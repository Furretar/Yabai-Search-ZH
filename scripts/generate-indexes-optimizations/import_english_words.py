#!/usr/bin/env python3
"""Build data/english_words.sqlite: every space-delimited word in the definitions.

The UI's "search the English word" hint is shown only when the query is a whole
word in this list, instead of any substring of any definition. One row per
normalized word (surrounding punctuation stripped, lowercased), collected from
the CEDICT, MOE and Wiktionary definition texts. Tokens with no ASCII letter or
digit are skipped: Chinese prose has no word breaks, so it would otherwise land
as one huge row.

Usage (from the project root):
    uv run python scripts/generate-indexes-optimizations/import_english_words.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from yabai_search import db  # noqa: E402
# Derived indexes live in db.GENERATED_DIR, not in data/ itself.
DB_PATH = ROOT / "data" / "generated-indexes-optimizations" / "english_words.sqlite"

# Characters trimmed from both ends of a token before lowercasing. Must match
# the strip set in yabai_search/db.py (_ENGLISH_WORD_STRIP).
_STRIP = " \t\r\n!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~“”‘’「」【】《》…·—–~"



def normalize(token: str) -> str:
    return token.strip(_STRIP).lower()


def build() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("CREATE TABLE words (word TEXT PRIMARY KEY) WITHOUT ROWID")

    def add(text: str | None) -> None:
        if not text:
            return
        for token in text.split():
            word = normalize(token)
            if not word or not any(char.isascii() and char.isalnum() for char in word):
                continue
            connection.execute("INSERT OR IGNORE INTO words (word) VALUES (?)", (word,))

    # Every dictionary whose definitions are English text (the "english_text" trait). What its tables look like
    # is in db._SOURCE_SCANS.
    for dictionary in db.sources_with("english_text"):
        for _word, text in db.scan_definitions(dictionary):
            add(text)
    connection.commit()
    words = connection.execute("SELECT count(*) FROM words").fetchone()[0]
    connection.close()
    print(f"done: {words} words -> {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    build()
