#!/usr/bin/env python3
"""Build the source-only Wiktionary gloss index.

The live Wiktionary database has every gloss, including words CEDICT already
covers. The search code only needs glosses whose simplified headword is *not*
in CEDICT. This script precomputes exactly that subset into a separate SQLite
FTS table so a phrase search does not scan the large live Wiktionary FTS table
and then throw CEDICT rows away.

Usage:
    nix develop ~/Documents/Yabai-Search -c .venv/bin/python \
        scripts/generate-indexes-optimizations/build_source_gloss.py
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from yabai_search.db import to_simplified  # noqa: E402

CEDICT = ROOT / "data" / "dictionary-databases" / "cedict.sqlite"
WIKTIONARY = ROOT / "data" / "dictionary-databases" / "wiktionary.sqlite"
# Derived indexes live in db.GENERATED_DIR, not in data/ itself.
GENERATED = ROOT / "data" / "generated-indexes-optimizations"
OUT = GENERATED / "source_gloss.sqlite"
TEMP_OUT = GENERATED / "source_gloss.sqlite.tmp"


def main() -> None:
    cedict = sqlite3.connect(CEDICT)
    known = {
        row[0]
        for row in cedict.execute(
            "SELECT traditional FROM entries UNION SELECT simplified FROM entries"
        )
    }
    cedict.close()

    source = sqlite3.connect(WIKTIONARY)
    rows = source.execute(
        "SELECT word, gloss FROM wiktionary WHERE gloss IS NOT NULL"
    )
    if TEMP_OUT.exists():
        TEMP_OUT.unlink()
    TEMP_OUT.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(TEMP_OUT)
    out.executescript(
        """
        DROP TABLE IF EXISTS source_gloss;
        CREATE TABLE source_gloss (word TEXT NOT NULL, gloss TEXT NOT NULL);
        DROP TABLE IF EXISTS source_gloss_fts;
        CREATE VIRTUAL TABLE source_gloss_fts USING fts5(
            word, gloss, tokenize='trigram'
        );
        DROP TABLE IF EXISTS meta;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    digest = hashlib.sha256(CEDICT.read_bytes()).hexdigest()
    out.execute("INSERT INTO meta(key, value) VALUES ('cedict_sha256', ?)", (digest,))
    batch: list[tuple[str, str]] = []
    total = 0
    for word, gloss in rows:
        if not word or not gloss:
            continue
        simplified = to_simplified(word)
        if simplified in known:
            continue
        batch.append((simplified, gloss))
        if len(batch) >= 5000:
            out.executemany("INSERT INTO source_gloss VALUES (?, ?)", batch)
            total += len(batch)
            batch.clear()
    if batch:
        out.executemany("INSERT INTO source_gloss VALUES (?, ?)", batch)
        total += len(batch)
    source.close()
    out.execute(
        "INSERT INTO source_gloss_fts(rowid, word, gloss) "
        "SELECT rowid, word, gloss FROM source_gloss"
    )
    out.execute("ANALYZE")
    out.commit()
    out.close()
    os.replace(TEMP_OUT, OUT)
    size = OUT.stat().st_size if OUT.exists() else 0
    print(f"source-only gloss rows: {total:,}")
    print(f"wrote {OUT} ({size / 1_000_000:.1f} MB)")


if __name__ == "__main__":
    main()
