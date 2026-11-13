#!/usr/bin/env python3
"""Generate the headwords index: data/generated-indexes-optimizations/headwords.sqlite.

One row per word form (simplified or traditional) that has a frequency or an HSK level, so a page of results
reads these numbers with one primary-key lookup in one small file, with no work per request:

    word          the form a headword is looked up by
    freq_rank, freq_count, per_million     from the configured frequency list (NULL when the word is not in it)
    hsk_new, hsk_newest                    the HSK 3.0 level (7 means 7-9) and the 2026 syllabus level
                                           (NULL when the word has no current level; HSK 2.0-only words get none)

It reproduces exactly what db._attach_freq and db._attach_hsk computed per request: a headword is matched by
its simplified form first, then its traditional form, separately for frequency and for HSK.

Rebuild whenever the frequency list, hsk.sqlite or this script changes (regenerate_everything.py does it;
`db.headwords_table_status()` reports a stale file).

    python scripts/generate-indexes-optimizations/build_headwords.py [--output PATH]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from yabai_search import db  # noqa: E402

SCHEMA = """
CREATE TABLE headwords (
    word        TEXT NOT NULL PRIMARY KEY,
    freq_rank   INTEGER,
    freq_count  INTEGER,
    per_million REAL,
    hsk_new     INTEGER,
    hsk_newest  INTEGER
) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def read_frequency() -> dict[str, tuple[int, int, float]]:
    """{word: (rank, count, per_million)} from the configured frequency list."""
    if not db.FREQ_DB_PATH.exists():
        print(f"no frequency list at {db.FREQ_DB_PATH}: frequencies left empty")
        return {}
    connection = sqlite3.connect(f"file:{db.FREQ_DB_PATH}?mode=ro", uri=True)
    try:
        return {w: (r, c, p) for w, r, c, p in connection.execute("SELECT word, rank, count, per_million FROM freq")}
    finally:
        connection.close()


def read_hsk() -> dict[str, tuple[int | None, int | None]]:
    """{word form: (new level, newest level)}, first row wins, like the per-request lookup."""
    if not db.HSK_DB_PATH.exists():
        print(f"no HSK list at {db.HSK_DB_PATH}: HSK levels left empty")
        return {}
    connection = sqlite3.connect(f"file:{db.HSK_DB_PATH}?mode=ro", uri=True)
    by_word: dict[str, tuple[int | None, int | None]] = {}
    try:
        for simplified, traditional, new, newest in connection.execute(
            "SELECT simplified, traditional, new_level, newest_level FROM hsk"
        ):
            if new is None and newest is None:
                continue  # an HSK 2.0-only word has no current level to show
            value = (new, newest)
            by_word.setdefault(simplified, value)
            if traditional:
                by_word.setdefault(traditional, value)
    finally:
        connection.close()
    return by_word


def build(output: Path) -> None:
    started = time.time()
    frequency = read_frequency()
    hsk = read_hsk()
    words = sorted(set(frequency) | set(hsk))
    rows = []
    for word in words:
        rank, count, per_million = frequency.get(word, (None, None, None))
        new, newest = hsk.get(word, (None, None))
        rows.append((word, rank, count, per_million, new, newest))

    temporary = output.with_name(output.name + ".tmp")
    output.parent.mkdir(parents=True, exist_ok=True)
    if temporary.exists():
        temporary.unlink()
    out = sqlite3.connect(temporary)
    out.executescript(SCHEMA)
    out.executemany("INSERT INTO headwords VALUES (?,?,?,?,?,?)", rows)
    out.execute("INSERT INTO meta VALUES ('signature', ?)", (db.headwords_signature(),))
    out.execute("INSERT INTO meta VALUES ('built_at', ?)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
    out.commit()
    out.execute("VACUUM")
    out.close()
    os.replace(temporary, output)
    print(f"frequency list: {len(frequency)} words; HSK: {len(hsk)} word forms; rows written: {len(rows)}")
    print(f"wrote {output} in {time.time() - started:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=db.HEADWORDS_DB_PATH)
    build(parser.parse_args().output)


if __name__ == "__main__":
    main()
