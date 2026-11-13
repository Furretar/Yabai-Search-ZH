#!/usr/bin/env python3
"""Build data/jpwiktionary.sqlite from Japanese Wiktionary's Chinese entries.

ja.wiktionary.org gives Chinese headwords (`=={{L|zh}}==`) Japanese-language
definitions. This is the Chinese-to-Japanese direction (the reverse of the
Japanese sections on zh.wiktionary). Data is CC BY-SA 4.0 and GFDL.

Usage (from the project root):
    uv run python scripts/create-dict-db/import_jpwiktionary.py
    uv run python scripts/create-dict-db/import_jpwiktionary.py --input data/jawiktionary.xml.bz2
"""

from __future__ import annotations

import argparse
import datetime
import re
import sqlite3
import sys
import time
from pathlib import Path


# Run as a file (python scripts/create-dict-db/NAME.py), so the project root is not
# on the import path by default.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# The zh.wiktionary importer sits next to this file, which is on the path when run as a file.
from import_zhwiktionary import (  # noqa: E402
    CJK_RE,
    HEADING_RE,
    clean_definition,
    download,
    read_pages,
)
from yabai_search.pinyin import split_pinyin  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = (
    "https://dumps.wikimedia.org/jawiktionary/latest/"
    "jawiktionary-latest-pages-articles.xml.bz2"
)
DEFAULT_CACHE = ROOT / "data" / "dl-base-dictionaries" / "jawiktionary-latest-pages-articles.xml.bz2"
DEFAULT_DB = ROOT / "data" / "jpwiktionary.sqlite"

# ja.wiktionary labels the Chinese language section with {{L|zh}}; older pages
# may use the plain heading instead.
CHINESE_MARKERS = ("{{L|zh}}", "中国語", "漢語", "汉语")
SENSE_RE = re.compile(r"^(#{1,3})\s*(.*)$")
WORD_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fffA-Za-z]")

PINYIN_RE = re.compile(r"\{\{cmn-pron\|([^|}]+)")
ZH_PRON_RE = re.compile(r"\{\{zh-pron\b(.*?)\}\}", re.S)
M_PINYIN_RE = re.compile(r"\|\s*m\s*=\s*([^|\n}]+)")
READING_RE = re.compile(r"^(?:ピンイン|拼音|Pinyin)\s*[:：]\s*(.+)$", re.I)
PLAIN_PINYIN_RE = re.compile(r"^[A-Za-züÜ:āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ\s]+$")
TONE_MARKS = "āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ"
CEDICT_DB = ROOT / "data" / "cedict.sqlite"


def cedict_syllables() -> set[str]:
    """Pinyin syllables used to space run-together reading headers."""
    if not CEDICT_DB.exists():
        return set()
    connection = sqlite3.connect(CEDICT_DB)
    try:
        rows = connection.execute("SELECT pinyin FROM entries").fetchall()
    finally:
        connection.close()
    return {
        token
        for (pinyin,) in rows
        for token in (pinyin or "").split()
    } - {""}


def section_pinyin(section: str) -> str:
    """The Mandarin pinyin from cmn-pron/zh-pron, when it is clean."""
    match = PINYIN_RE.search(section)
    value = match.group(1) if match else ""
    if not value:
        pron = ZH_PRON_RE.search(section)
        if pron:
            mandarin = M_PINYIN_RE.search(pron.group(1))
            value = mandarin.group(1) if mandarin else ""
    value = re.split(r"[,/]", value.strip())[0].strip()
    if not value or not re.fullmatch(r"[A-Za-züÜ:āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ\s]+", value):
        return ""
    return re.sub(r"\s+", " ", value)


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
    pinyin      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_headwords_traditional ON headwords(traditional);
CREATE INDEX idx_headwords_simplified ON headwords(simplified);

CREATE TABLE senses (
    word     TEXT NOT NULL,
    position INTEGER NOT NULL,
    text     TEXT NOT NULL,
    reading  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_senses_word ON senses(word);

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


def chinese_section(wikitext: str) -> str | None:
    """The Chinese-language section of a Japanese Wiktionary page."""
    lines = wikitext.splitlines()
    start = None
    for i, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match and any(marker in match.group(1) for marker in CHINESE_MARKERS):
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--force", action="store_true", help="re-download the dump")
    args = parser.parse_args()

    if not args.input.exists() or args.force:
        download(args.url, args.input)

    syllables = cedict_syllables()
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
            pinyin = section_pinyin(section)
            definitions: list[tuple[str, str]] = []
            current_reading = ""
            for line in section.splitlines():
                match = SENSE_RE.match(line)
                if not match:
                    continue
                body = match.group(2)
                if body.startswith((":", "*", ";")):
                    continue
                cleaned = clean_definition(body)
                if not cleaned or not WORD_RE.search(cleaned):
                    continue
                reading = READING_RE.match(cleaned)
                if reading:
                    value = reading.group(1).strip()
                    if re.fullmatch(
                        r"[A-Za-züÜ:āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ\s]+", value
                    ):
                        current_reading = split_pinyin(value, syllables)
                        continue
                # A bare pinyin line (e.g. "shèn") is a reading header, not a
                # definition; the actual definitions, if any, follow it.
                if PLAIN_PINYIN_RE.fullmatch(cleaned) and any(
                    mark in cleaned for mark in TONE_MARKS
                ):
                    continue
                item = (current_reading, cleaned)
                if item not in definitions:
                    definitions.append(item)
            if not definitions:
                continue
            connection.execute(
                "INSERT OR REPLACE INTO headwords(word, traditional, simplified, pinyin) "
                "VALUES (?, ?, ?, ?)",
                (word, word, word, pinyin),
            )
            connection.executemany(
                "INSERT INTO senses(word, position, text, reading) VALUES (?, ?, ?, ?)",
                [
                    (word, i + 1, text, reading)
                    for i, (reading, text) in enumerate(definitions)
                ],
            )
            connection.executemany(
                "INSERT INTO senses_fts(word, text) VALUES (?, ?)",
                [(word, text) for _reading, text in definitions],
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
        connection.execute(
            "INSERT INTO meta(key, value) VALUES ('updated', ?)",
            (datetime.date.today().isoformat(),),
        )
        connection.commit()
        connection.execute("VACUUM")
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
