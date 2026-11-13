#!/usr/bin/env python3
"""Build data/gram_words.sqlite: a Han unigram/bigram -> word inverted index.

For 1-2 character CJK queries the trigram index does not apply, so the app looks
the query up here instead of scanning whole tables. Each row is
(source, gram, word):

    wikt_head   Wiktionary headwords that have a gloss
    wikt_gloss  words whose Wiktionary gloss contains the gram
    moe_head    MOE headwords
    baidu_head  百度汉语 headwords
    moe_sense   words whose MOE sense contains the gram
    moe_revised_sense  the same for the revised MOE dictionary's visible definitions

A 2-character query is one bigram lookup, a 1-character query one unigram
lookup. The candidates are exact (they contain the query), and the app still
re-checks with LIKE before including them, so results do not change.

Usage (from the project root):
    uv run python scripts/generate-indexes-optimizations/import_gram_words.py
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Derived indexes live in db.GENERATED_DIR, not in data/ itself.
GENERATED = ROOT / "data" / "generated-indexes-optimizations"
DB_PATH = GENERATED / "gram_words.sqlite"
TEMP_PATH = GENERATED / "gram_words.sqlite.tmp"
FREQ_DB = ROOT / "data" / "dictionary-databases" / "corpus_freq.sqlite"
# Which dictionaries have a headword gram index comes from the registry in db.py (the "head_grams" trait); the
# frequency list is not a dictionary but is indexed too.
sys.path.insert(0, str(ROOT))
from yabai_search import db  # noqa: E402

SOURCE_FILES = {**db.source_databases("head_grams"), "freq": FREQ_DB}


def is_han(char: str) -> bool:
    """True for a Han ideograph, including the supplementary planes."""
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x323AF
    )


def grams_of(text: str) -> set[str]:
    """Every Han unigram and bigram inside the text's Han runs."""
    grams: set[str] = set()
    run: list[str] = []

    def flush() -> None:
        for index, char in enumerate(run):
            grams.add(char)
            if index + 1 < len(run):
                grams.add(char + run[index + 1])

    for char in text:
        if is_han(char):
            run.append(char)
        else:
            flush()
            run.clear()
    flush()
    return grams


def build() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TEMP_PATH.exists():
        TEMP_PATH.unlink()
    connection = sqlite3.connect(TEMP_PATH)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute(
        "CREATE TABLE gram_words ("
        "source TEXT NOT NULL, gram TEXT NOT NULL, word TEXT NOT NULL, "
        "PRIMARY KEY (source, gram, word)) WITHOUT ROWID"
    )
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    insert = "INSERT OR IGNORE INTO gram_words (source, gram, word) VALUES (?,?,?)"

    def add_word(source: str, word: str) -> None:
        connection.executemany(
            insert, [(source, gram, word) for gram in grams_of(word)]
        )

    def add_texts(source: str, rows) -> None:
        """Insert grams for (word, text) rows already ordered by word."""
        current = None
        grams: set[str] = set()
        for word, text in rows:
            if word != current:
                if current is not None:
                    connection.executemany(
                        insert, [(source, gram, current) for gram in grams]
                    )
                current = word
                grams = set()
            grams |= grams_of(text or "")
        if current is not None:
            connection.executemany(
                insert, [(source, gram, current) for gram in grams]
            )

    # Every dictionary with a headword gram index (the "head_grams" trait), read the same way: its headwords,
    # and, when it has a "gram_texts" index, its definition text. What each dictionary's tables look like is in
    # db._SOURCE_SCANS; nothing here names a dictionary.
    for dictionary in db.sources_with("head_grams"):
        for word in db.scan_headwords(dictionary):
            add_word(dictionary["head_grams"], word)
        texts = dictionary.get("gram_texts")
        if texts:
            add_texts(texts, db.scan_definitions(dictionary, visible=True, by_word=True))
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, '1')", (f"has:{texts}",)
            )
    if FREQ_DB.exists():
        source = sqlite3.connect(FREQ_DB)
        try:
            for (word,) in source.execute("SELECT word FROM freq ORDER BY rank"):
                add_word("freq_head", word)
        finally:
            source.close()

    for name, path in SOURCE_FILES.items():
        if path.exists():
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (f"source:{name}", hashlib.sha256(path.read_bytes()).hexdigest()),
            )
    connection.commit()
    count = connection.execute("SELECT count(*) FROM gram_words").fetchone()[0]
    connection.close()
    os.replace(TEMP_PATH, DB_PATH)
    print(
        f"done: {count} rows -> {DB_PATH} "
        f"({DB_PATH.stat().st_size / 1e6:.1f} MB)"
    )


if __name__ == "__main__":
    build()
