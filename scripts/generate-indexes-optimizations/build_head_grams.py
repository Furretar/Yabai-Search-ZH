#!/usr/bin/env python3
"""Build the headword gram candidate table (and the definition-text gram set).

The normal Chinese headword query uses leading-wildcard LIKE on simplified and
traditional, which cannot use the B-tree indexes. For a 1-2 character query,
every possible matching headword must contain one of its 1-grams or 2-grams.
This table maps those grams to entry ids so the query can narrow candidates
before applying the exact LIKE test.

Usage:
    nix develop ~/Documents/Yabai-Search -c .venv/bin/python \
        scripts/generate-indexes-optimizations/build_head_grams.py
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from yabai_search import db  # noqa: E402

# The index maps a character gram to the ids of the rows of the table the main search runs on: the unified
# entries table when it is current, else CEDICT's (db.backbone_path()). Build it after build_entries.py.
CEDICT = db.backbone_path()
# Derived indexes live here (db.GENERATED_DIR), not in data/ itself.
GENERATED = ROOT / "data" / "generated-indexes-optimizations"
OUT = GENERATED / "head_grams.sqlite"
TEMP_OUT = GENERATED / "head_grams.sqlite.tmp"


def main() -> None:
    source = sqlite3.connect(CEDICT)
    rows = source.execute("SELECT id, simplified, traditional FROM entries").fetchall()
    # Every character and adjacent pair that occurs in any definition text (only CEDICT's rows have one). A
    # long query that is not a headword is also rejected when it cannot occur in a definition, which is the
    # other half of the "contains" scan (db._english_han_possible).
    english_grams: set[str] = set()
    for (text,) in source.execute("SELECT english_normalized FROM entries WHERE english_normalized <> ''"):
        for start in range(len(text)):
            english_grams.add(text[start])
            if start + 1 < len(text):
                english_grams.add(text[start : start + 2])
    source.close()
    if TEMP_OUT.exists():
        TEMP_OUT.unlink()
    TEMP_OUT.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(TEMP_OUT)
    out.executescript(
        """
        DROP TABLE IF EXISTS head_grams;
        CREATE TABLE head_grams (
            gram TEXT NOT NULL,
            entry_id INTEGER NOT NULL,
            PRIMARY KEY (gram, entry_id)
        ) WITHOUT ROWID;
        DROP TABLE IF EXISTS english_grams;
        CREATE TABLE english_grams (gram TEXT PRIMARY KEY) WITHOUT ROWID;
        DROP TABLE IF EXISTS meta;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    batch: list[tuple[str, int]] = []
    total = 0
    for entry_id, simplified, traditional in rows:
        grams: set[str] = set()
        for word in (simplified or "", traditional or ""):
            for size in (1, 2):
                for start in range(0, max(0, len(word) - size + 1)):
                    gram = word[start : start + size]
                    if gram:
                        grams.add(gram)
        if not grams:
            continue
        batch.extend((gram, entry_id) for gram in grams)
        if len(batch) >= 5000:
            out.executemany("INSERT OR IGNORE INTO head_grams VALUES (?, ?)", batch)
            total += len(batch)
            batch.clear()
    if batch:
        out.executemany("INSERT OR IGNORE INTO head_grams VALUES (?, ?)", batch)
        total += len(batch)
    out.executemany("INSERT OR IGNORE INTO english_grams VALUES (?)", ((gram,) for gram in english_grams))
    digest = hashlib.sha256(CEDICT.read_bytes()).hexdigest()
    out.execute(
        "INSERT INTO meta(key, value) VALUES ('backbone_sha256', ?)", (digest,)
    )
    out.execute("ANALYZE")
    out.commit()
    out.close()
    os.replace(TEMP_OUT, OUT)
    print(f"headword gram rows: {total:,}")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1_000_000:.1f} MB)")


if __name__ == "__main__":
    main()
