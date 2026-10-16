#!/usr/bin/env python3
"""Build data/wiktionary.sqlite from kaikki.org's (Wiktextract) Chinese JSONL.

The source is en.wiktionary's Chinese entries, parsed into machine-readable JSON
by Wiktextract and published at kaikki.org. It is updated regularly and shares
Wiktionary's licence (CC BY-SA 4.0 and GFDL).

The database stores only the headwords (enough for the source chips). Each line
of the JSONL is one word/part-of-speech entry; we keep its CJK `word`.

Usage (from the project root):
    uv run python -m scripts.import_wiktionary
    uv run python -m scripts.import_wiktionary --input data/kaikki_zh.jsonl.gz
    uv run python -m scripts.import_wiktionary --force
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "https://kaikki.org/dictionary/Chinese/kaikki.org-dictionary-Chinese.jsonl.gz"
DEFAULT_CACHE = ROOT / "data" / "kaikki_zh.jsonl.gz"
DEFAULT_DB = ROOT / "data" / "wiktionary.sqlite"

# One row per headword, plus one row per (word, gloss). A word with no gloss is
# still recorded once (with a NULL gloss) so the source chips can find it.
SCHEMA = """
DROP TABLE IF EXISTS wiktionary;

CREATE TABLE wiktionary (
    id    INTEGER PRIMARY KEY,  -- auto row id
    word  TEXT NOT NULL,        -- a headword listed by Wiktionary
    gloss TEXT                  -- one English gloss (NULL when the word has none)
);

CREATE INDEX idx_wiktionary_word ON wiktionary(word);

-- Standardized classifiers, extracted from the "(Classifier: ...)" notes in the
-- glosses (same table shape as every other source database).
DROP TABLE IF EXISTS classifiers;

CREATE TABLE classifiers (
    word        TEXT NOT NULL,
    traditional TEXT,
    simplified  TEXT,
    codes       TEXT,
    note        TEXT
);
CREATE INDEX idx_classifiers_word ON classifiers(word);

-- Metadata (the About page shows when the data was last updated).
DROP TABLE IF EXISTS meta;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def has_cjk(text: str) -> bool:
    """True when `text` contains a CJK ideograph.

    kaikki's Chinese set also holds latin headwords (e.g. "book", "GDP"); those
    can't match a Chinese result, so we skip them.
    """
    return any("\u3400" <= char <= "\u9fff" for char in text)


# kaikki's "Chinese" set mixes every Chinese variety. We only want the
# Mandarin/standard data, so an entry that belongs *solely* to one of these four
# varieties (and nothing we keep) is dropped.
_EXCLUDED_VARIETIES = (
    "cantonese", "hakka", "puxian", "pu-xian", "xinghua", "henghua",
    "min-nan", "minnan", "southern-min", "hokkien", "taiwanese", "teochew",
    "chiuchow", "amoy", "xiamen", "quanzhou", "zhangzhou", "hoklo",
)
# Varieties we keep. Any of these on an entry overrides the exclusion above.
_INCLUDED_VARIETIES = (
    "mandarin", "putonghua", "guoyu", "standard-chinese", "sichuanese",
    "shanghainese", "hainanese", "fuzhounese", "waxiang", "pinghua",
    "dungan", "jianghuai", "min-dong", "eastern-min", "min-bei",
    "northern-min", "min-zhong", "central-min",
)
# Short variety names, matched only as whole hyphen-delimited tokens so "wu"
# doesn't match inside a longer word.
_SHORT_INCLUDED = re.compile(r"(?:^|-)(?:wu|gan|xiang|jin|hui)(?:-|$)")


def _entry_tags(obj: dict) -> set[str]:
    """Every dialect/register tag on an entry's sounds and senses, normalized."""
    found: set[str] = set()
    for sound in obj.get("sounds") or []:
        found.update(sound.get("tags") or [])
        found.update(sound.get("raw_tags") or [])
    for sense in obj.get("senses") or []:
        found.update(sense.get("tags") or [])
        found.update(sense.get("raw_tags") or [])
    return {tag.lower().replace(" ", "-") for tag in found}


def is_excluded_variety(obj: dict) -> bool:
    """True when an entry belongs only to an excluded variety.

    Excluded: Cantonese, Hakka, Puxian Min and Southern Min. An entry with any
    kept variety on it (Mandarin, Wu, ...), or with no variety tag at all, is
    not excluded.
    """
    tags = _entry_tags(obj)
    if not any(bad in tag for tag in tags for bad in _EXCLUDED_VARIETIES):
        return False
    if any(good in tag for tag in tags for good in _INCLUDED_VARIETIES):
        return False
    return not any(_SHORT_INCLUDED.search(tag) for tag in tags)


# Wiktionary writes classifier notes like "(Classifier: 匹 m c; 隻／只 m c h mn)".
_CLASSIFIER_RE = re.compile(r"\s*\(Classifier:\s*([^)]+)\)")


def extract_classifiers(gloss: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Split a "(Classifier: ...)" note out of a gloss.

    Returns (clean_gloss, [(traditional, simplified, codes), ...]). A token like
    "隻／只 m c h mn" becomes ("隻", "只", "m c h mn"); "匹 m c" becomes
    ("匹", "匹", "m c").
    """
    found: list[tuple[str, str, str]] = []

    def collect(match: re.Match) -> str:
        for token in match.group(1).split(";"):
            parts = token.strip().split()
            if not parts:
                continue
            form = parts[0]
            codes = " ".join(parts[1:])
            if "／" in form:
                trad, simp = form.split("／", 1)
            elif "/" in form:
                trad, simp = form.split("/", 1)
            else:
                trad = simp = form
            found.append((trad, simp, codes))
        return ""

    return _CLASSIFIER_RE.sub(collect, gloss).strip(), found


def download(url: str, dest: Path) -> None:
    """Stream `url` to `dest` so we cache the file instead of re-fetching it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "yabai-chaci/0.1"})
    with urllib.request.urlopen(request) as response, dest.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    print(f"  saved {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


def build(source: Path, db_path: Path) -> None:
    """(Re)build the SQLite database from the gzipped JSONL `source`."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Full wipe: delete the old DB file so every run starts from scratch.
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA)
    # Speed up a bulk load: disable crash-safe journaling while importing.
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    # kaikki's file has no date in it, so record when this snapshot was built.
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('updated', ?)",
        (time.strftime("%Y-%m-%d"),),
    )

    insert = "INSERT INTO wiktionary (word, gloss) VALUES (?, ?)"
    insert_classifier = (
        "INSERT INTO classifiers (word, traditional, simplified, codes, note) "
        "VALUES (?,?,?,?,?)"
    )
    seen_word: set[str] = set()  # headwords recorded (once each, even with no gloss)
    seen_gloss: set[tuple[str, str]] = set()  # (word, gloss) pairs, de-duplicated
    seen_cls: set[tuple[str, str, str, str]] = set()  # (word, trad, simp, codes)
    rows: list[tuple[str, str | None]] = []
    cls_rows: list[tuple[str, str, str, str, str]] = []
    kept = 0
    dropped = 0  # entries skipped because they are an excluded variety
    started = time.time()

    def flush() -> None:
        """Write the buffered rows and clear the buffers."""
        connection.executemany(insert, rows)
        connection.executemany(insert_classifier, cls_rows)
        rows.clear()
        cls_rows.clear()

    with gzip.open(source, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)  # one word entry per line
            except json.JSONDecodeError:
                continue  # skip any malformed line rather than abort
            # Drop entries that are only Cantonese / Hakka / Puxian / Southern Min.
            if is_excluded_variety(obj):
                dropped += 1
                continue
            word = obj.get("word")
            if not word or not has_cjk(word):
                continue
            # Record the headword once, even if it has no gloss (the chips only
            # need to know the word is present).
            if word not in seen_word:
                seen_word.add(word)
                rows.append((word, None))
                kept += 1
            # ...then one row per distinct gloss on any of its senses, pulling any
            # "(Classifier: ...)" note out into the standardized classifiers table.
            for sense in obj.get("senses") or []:
                for gloss in sense.get("glosses") or []:
                    clean, classifiers = extract_classifiers(gloss)
                    for trad, simp, codes in classifiers:
                        ckey = (word, trad, simp, codes)
                        if ckey not in seen_cls:
                            seen_cls.add(ckey)
                            cls_rows.append((word, trad, simp, codes, ""))
                    key = (word, clean)
                    if clean and key not in seen_gloss:
                        seen_gloss.add(key)
                        rows.append((word, clean))
            # Flush every 5000 rows: fewer, larger transactions is faster.
            if len(rows) + len(cls_rows) >= 5000:
                flush()
    if rows or cls_rows:
        flush()  # flush the last partial batch

    connection.commit()
    connection.execute("VACUUM")  # repack the file compactly
    words = connection.execute("SELECT count(DISTINCT word) FROM wiktionary").fetchone()[0]
    total = connection.execute("SELECT count(*) FROM wiktionary").fetchone()[0]
    connection.close()

    print(
        f"done: {words} headwords, {total} rows in {time.time() - started:.1f}s "
        f"({dropped} dialect entries skipped)"
    )
    print(f"database: {db_path} ({db_path.stat().st_size / 1e6:.1f} MB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--input", type=Path, help="use a local .jsonl.gz file")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--force", action="store_true", help="re-download")
    args = parser.parse_args()

    if args.input:
        source = args.input  # the user pointed us at a local file
    else:
        source = DEFAULT_CACHE
        # Download only if there's no cached copy, or the user forced a refresh.
        if args.force or not source.exists():
            download(args.url, source)

    build(source, args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
