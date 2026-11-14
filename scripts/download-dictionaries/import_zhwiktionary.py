#!/usr/bin/env python3
"""Build data/dictionary-databases/zhwiktionary.sqlite from the Chinese Wiktionary dump.

The source is zh.wiktionary.org's Chinese-language entries (CC BY-SA 4.0 and
GFDL). This reads the official pages-articles XML dump, keeps pages with a
Chinese (漢語) section, and extracts their `#` definition lines as plain text.

Usage (from the project root):
    uv run python scripts/download-dictionaries/import_zhwiktionary.py
    uv run python scripts/download-dictionaries/import_zhwiktionary.py --input data/zhwiktionary.xml.bz2
"""

from __future__ import annotations

import _wikipedia_titles
import argparse
import bz2
import datetime
import gzip
import html
import re
import sqlite3
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


from opencc import OpenCC

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = (
    "https://dumps.wikimedia.org/zhwiktionary/latest/"
    "zhwiktionary-latest-pages-articles.xml.bz2"
)
DEFAULT_CACHE = ROOT / "data" / "dl-base-dictionaries" / "zhwiktionary-latest-pages-articles.xml.bz2"
DEFAULT_DB = ROOT / "data" / "dictionary-databases" / "zhwiktionary.sqlite"
CEDICT_DB = ROOT / "data" / "dictionary-databases" / "cedict.sqlite"

# Top-level language headings that hold the Chinese entries.
CHINESE_HEADINGS = ("漢語", "汉语", "中文", "官話", "官话")
CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
HEADING_RE = re.compile(r"^==\s*([^=\s][^=]*?)\s*==\s*$")
SENSE_RE = re.compile(r"^(#{1,3})\s*(.*)$")
PINYIN_RE = re.compile(r"\|[ \t]*m[ \t]*=[ \t]*([^|\n}]+)")

SCHEMA = """
DROP TABLE IF EXISTS headwords;
DROP TABLE IF EXISTS senses;
DROP TABLE IF EXISTS senses_fts;
DROP TABLE IF EXISTS classifiers;
DROP TABLE IF EXISTS meta;

CREATE TABLE headwords (
    word        TEXT PRIMARY KEY,
    traditional TEXT NOT NULL,
    simplified  TEXT NOT NULL,
    pinyin      TEXT NOT NULL DEFAULT '',
    wikipedia   INTEGER NOT NULL DEFAULT 0  -- 1 = only a Chinese Wikipedia title, no Wiktionary entry
);
-- LIKE 'x%' (the headword prefix search) can only use an index whose collation is NOCASE; without this it
-- scanned all 2.7 million Wikipedia titles, 260 ms per search.
CREATE INDEX idx_headwords_word_nocase ON headwords(word COLLATE NOCASE);
CREATE INDEX idx_headwords_traditional ON headwords(traditional);
CREATE INDEX idx_headwords_simplified ON headwords(simplified);

CREATE TABLE senses (
    word     TEXT NOT NULL,
    position INTEGER NOT NULL,
    text     TEXT NOT NULL
);
CREATE INDEX idx_senses_word ON senses(word);

-- Trigram index for quoted definition searches.
CREATE VIRTUAL TABLE senses_fts USING fts5(word, text, tokenize='trigram');

CREATE TABLE classifiers (
    word        TEXT NOT NULL,
    traditional TEXT,
    simplified  TEXT,
    codes       TEXT,
    note        TEXT
);
CREATE INDEX idx_classifiers_word ON classifiers(word);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def clean_definition(text: str) -> str:
    """Strip wiki markup from one definition line."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref\b[^>]*>.*?</ref>", "", text, flags=re.S | re.I)
    text = re.sub(r"<ref\b[^>]*/>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    # Keep the base text of Japanese ruby templates (e.g. 心 from
    # {{ふりがな|心|こころ}}) before generic templates are stripped.
    text = re.sub(
        r"\{\{(?:ふりがな|furigana|ruby)\|([^|}]+)\|[^}]*\}\}",
        r"\1",
        text,
        flags=re.I,
    )
    # Okurigana templates: {{おくりがな2|何|なに|か|なにか}} -> 何か,
    # {{おくりがな|健|やか|すこやか}} -> 健やか.
    text = re.sub(
        r"\{\{おくりがな2\|([^|}]+)\|[^|}]*\|([^|}]*)\|[^}]*\}\}",
        r"\1\2",
        text,
    )
    text = re.sub(
        r"\{\{おくりがな\|([^|}]+)\|([^|}]*)\|[^}]*\}\}",
        r"\1\2",
        text,
    )
    # Remove innermost templates, repeated for nested ones.
    while True:
        stripped = re.sub(r"\{\{[^{}]*\}\}", "", text)
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[https?://[^\]]+\]", "", text)
    text = text.replace("'''", "").replace("''", "")
    text = re.sub(r"^[\s:*#]+", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def chinese_section(wikitext: str) -> str | None:
    """The text of the Chinese (漢語) language section, if present."""
    lines = wikitext.splitlines()
    start = None
    for i, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match and any(name in match.group(1) for name in CHINESE_HEADINGS):
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start, len(lines)):
        if HEADING_RE.match(lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def cedict_pinyin() -> dict[str, str]:
    """Word -> tone-marked pinyin from CEDICT, when it is available."""
    if not CEDICT_DB.exists():
        return {}
    connection = sqlite3.connect(CEDICT_DB)
    try:
        rows = connection.execute(
            "SELECT traditional, simplified, pinyin FROM entries"
        ).fetchall()
    finally:
        connection.close()
    readings: dict[str, str] = {}
    for traditional, simplified, pinyin in rows:
        for word in (traditional, simplified):
            if word and word not in readings:
                readings[word] = pinyin or ""
    return readings


def template_pinyin(section: str) -> str:
    """The Mandarin reading from {{zh-pron|m=...}} when it is clean pinyin."""
    pronunciation = re.search(r"\{\{zh-pron\b(.*?)\}\}", section, re.S)
    if not pronunciation:
        return ""
    match = PINYIN_RE.search(pronunciation.group(1))
    if not match:
        return ""
    value = match.group(1).strip()
    value = re.split(r"[,/]", value)[0]
    # Some entries leave a Han character inside the parameter (a partial
    # reading); skip those rather than storing a truncated pinyin.
    if CJK_RE.search(value):
        return ""
    value = re.sub(r"[^\sA-Za-züÜ:āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]", "", value).strip()
    if not value or not re.fullmatch(r"[A-Za-züÜ:āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ\s]+", value):
        return ""
    return re.sub(r"\s+", " ", value)


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}", file=sys.stderr)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Yabai-Search/1.0 "
                "(https://github.com/Furretar/Yabai-Search-ZH)"
            )
        },
    )
    with urllib.request.urlopen(request) as response, destination.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


def read_pages(path: Path):
    """Yield (title, wikitext) for article pages in the dump."""
    if path.suffix == ".bz2":
        stream = bz2.open(path, "rb")
    elif path.suffix == ".gz":
        stream = gzip.open(path, "rb")
    else:
        stream = path.open("rb")
    try:
        for _event, element in ET.iterparse(stream, events=("end",)):
            if not element.tag.rsplit("}", 1)[-1] == "page":
                continue
            title = ns = None
            redirect = False
            text = None
            for child in element:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "title":
                    title = child.text or ""
                elif tag == "ns":
                    ns = child.text or ""
                elif tag == "redirect":
                    redirect = True
                elif tag == "revision":
                    for node in child.iter():
                        if node.tag.rsplit("}", 1)[-1] == "text":
                            text = node.text or ""
            element.clear()
            if ns != "0" or redirect or not title or text is None:
                continue
            yield title, text
    finally:
        stream.close()


def add_wikipedia_titles(connection, readings: dict[str, str]) -> int:
    """Every Chinese Wikipedia title as a headword (see _wikipedia_titles.py), with no sense.

    A title the Wiktionary already has is left alone. A new one is a headword only: the result shows the
    source chip and no definition, which is how a word the source merely lists is already displayed. They
    are flagged wikipedia = 1 so the segmenter and word lists skip them, and they are not in senses_fts.
    Both script columns hold the title as written (a search matches either script through its variants).
    """
    known = {row[0] for row in connection.execute("SELECT word FROM headwords")}
    batch: list[tuple[str, str, str, str]] = []
    added = 0
    for title, _translation in _wikipedia_titles.rows():
        if title in known:
            continue
        batch.append((title, title, title, readings.get(title, "")))
        if len(batch) == 100000:
            connection.executemany(
                "INSERT OR IGNORE INTO headwords(word, traditional, simplified, pinyin, wikipedia) VALUES (?, ?, ?, ?, 1)",
                batch,
            )
            added += len(batch)
            batch = []
    connection.executemany(
        "INSERT OR IGNORE INTO headwords(word, traditional, simplified, pinyin, wikipedia) VALUES (?, ?, ?, ?, 1)",
        batch,
    )
    return added + len(batch)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--force", action="store_true", help="re-download the dump")
    args = parser.parse_args()

    if not args.input.exists() or args.force:
        download(args.url, args.input)
    if args.force:
        _wikipedia_titles.build(force=True)  # newer Wikipedia dumps too

    to_simplified = OpenCC("t2s").convert
    to_traditional = OpenCC("s2t").convert
    readings = cedict_pinyin()

    connection = sqlite3.connect(args.database)
    try:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        seen_pages = 0
        headwords = 0
        senses = 0
        started = time.time()
        for title, wikitext in read_pages(args.input):
            seen_pages += 1
            if not CJK_RE.search(title) or ":" in title or "/" in title:
                continue
            section = chinese_section(wikitext)
            if section is None:
                continue
            word = title.strip()
            if not word:
                continue
            definitions: list[str] = []
            for line in section.splitlines():
                match = SENSE_RE.match(line)
                if not match:
                    continue
                body = match.group(2)
                if body.startswith((":", "*", ";")):
                    continue
                cleaned = clean_definition(body)
                if not cleaned or not CJK_RE.search(cleaned):
                    continue
                if cleaned not in definitions:
                    definitions.append(cleaned)
            if not definitions:
                continue
            pinyin = readings.get(word, "") or template_pinyin(section)
            simplified = to_simplified(word)
            traditional = to_traditional(word)
            connection.execute(
                "INSERT OR REPLACE INTO headwords(word, traditional, simplified, pinyin) "
                "VALUES (?, ?, ?, ?)",
                (word, traditional, simplified, pinyin),
            )
            connection.executemany(
                "INSERT INTO senses(word, position, text) VALUES (?, ?, ?)",
                [(word, i + 1, text) for i, text in enumerate(definitions)],
            )
            connection.executemany(
                "INSERT INTO senses_fts(word, text) VALUES (?, ?)",
                [(word, text) for text in definitions],
            )
            headwords += 1
            senses += len(definitions)
            if seen_pages % 1000 == 0:
                connection.commit()
                print(
                    f"{seen_pages:,} pages, {headwords:,} headwords, "
                    f"{senses:,} senses ({time.time() - started:.0f}s)",
                    file=sys.stderr,
                )
        wiki = add_wikipedia_titles(connection, readings)
        connection.execute(
            "INSERT INTO meta(key, value) VALUES ('updated', ?)",
            (datetime.date.today().isoformat(),),
        )
        connection.commit()
        connection.execute("VACUUM")
        print(f"  Wikipedia: {wiki:,} titles added as headwords", file=sys.stderr)
        print(
            f"done: {headwords:,} headwords, {senses:,} senses "
            f"from {seen_pages:,} pages",
            file=sys.stderr,
        )
    finally:
        connection.close()


if __name__ == "__main__":
    import _indexes  # rebuilds the derived indexes when the import is done (--no-indexes skips it)

    sys.exit(_indexes.run(main))
