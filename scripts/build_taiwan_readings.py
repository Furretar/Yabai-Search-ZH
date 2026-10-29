#!/usr/bin/env python3
"""Prefill the Taiwan reading of every dictionary row: data/taiwan_readings.sqlite.

The Taiwan toggle used to derive a Taiwan reading per result at request time, so
search could neither find a word by its Taiwan reading nor rank it by it. This
script runs the SAME derivation (db._entry_taiwan_reading, which honours the
manual TAIWAN_READINGS in overrides.py) once, over every headword of every
dictionary, and stores the result. Search, ranking and display all read it.

The source databases are only read, never modified. The output is written to a
temporary file and renamed into place, so a failed run leaves the old table.

Rebuild whenever a source database, overrides.py or the Taiwan-reading logic in
db.py changes (see AGENTS.md). The stored signature makes the server report a
stale table at startup.

    python scripts/build_taiwan_readings.py [--output PATH] [--samples N]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yabai_search import db  # noqa: E402
from yabai_search.overrides import TAIWAN_READINGS  # noqa: E402
from yabai_search.pinyin import to_numeric, to_toneless  # noqa: E402

SCHEMA = """
CREATE TABLE readings (
    tw_source     TEXT NOT NULL,     -- dictionary anchor (cedict, moe, ...)
    tw_word       TEXT NOT NULL,     -- headword exactly as the source stores it
    tw_simplified TEXT NOT NULL,
    tw_mainland   TEXT NOT NULL,     -- the source's own marked reading (join key)
    tw_reading    TEXT NOT NULL,     -- Taiwan reading, marked pinyin
    tw_numeric    TEXT NOT NULL,     -- the same, numeric ("hai2 xia4")
    tw_toneless   TEXT NOT NULL,     -- the same, no tones, no spaces (search key)
    tw_changed    INTEGER NOT NULL   -- 1 when the Taiwan reading differs
);
CREATE INDEX readings_toneless ON readings (tw_toneless);
CREATE INDEX readings_key ON readings (tw_source, tw_word, tw_mainland);
CREATE INDEX readings_word ON readings (tw_word);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def moe_readings() -> dict[str, list[str]]:
    """{word: [Taiwan readings]} from both MOE databases, in one pass."""
    by_word: dict[str, list[str]] = {}
    for path in db._moe_db_paths():
        connection = db._connect(path)
        try:
            for word, pinyin in connection.execute(
                "SELECT DISTINCT word, pinyin FROM moe WHERE pinyin <> ''"
            ):
                options = by_word.setdefault(word, [])
                if pinyin not in options:
                    options.append(pinyin)
        finally:
            connection.close()
    return by_word


def source_rows(anchor: str):
    """Yield (word, simplified, marked reading, english_normalized) for a source."""
    source = next(item for item in db._SOURCE_QUERIES if item["anchor"] == anchor)
    path = source["database"]
    if not path.exists():
        return
    connection = db._connect(path)
    try:
        if anchor == "cedict":
            sql = (
                "SELECT traditional, simplified, pinyin, english_normalized "
                "FROM entries WHERE pinyin <> ''"
            )
            for word, simplified, pinyin, english in connection.execute(sql):
                yield word, simplified, pinyin, english or ""
            return
        if anchor == "wiktionary":
            # Mandarin readings only; the dialect readings have no Taiwan form.
            sql = (
                "SELECT word, pinyin FROM readings "
                "WHERE kind = 'mandarin' AND pinyin <> ''"
            )
            rows = ((word, None, pinyin) for word, pinyin in connection.execute(sql))
        else:
            rows = (
                (record[0], record[1] if len(record) > 2 else None, record[-1])
                for record in connection.execute(source["pinyin_rows_sql"])
            )
        for word, simplified, pinyin in rows:
            if not word or not pinyin:
                continue
            marked = db._split_pinyin(pinyin) if source.get("split_pinyin") else pinyin
            yield word, simplified or db.to_simplified(word), marked, ""
    finally:
        connection.close()


def build(output: Path, samples: int = 8) -> None:
    started = time.time()
    counts = db._reading_counts()
    char_map = db._moe_char_map()
    options_by_word = moe_readings()
    moe_anchors = {"moe", "moe_revised"}

    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    out = sqlite3.connect(temporary)
    out.executescript(SCHEMA)

    for source in db._SOURCE_QUERIES:
        anchor = source["anchor"]
        seen: set[tuple[str, str]] = set()
        batch: list[tuple] = []
        changed = total = 0
        examples: list[str] = []
        for word, simplified, marked, english in source_rows(anchor):
            if (word, marked) in seen:
                continue
            seen.add((word, marked))
            if anchor in moe_anchors:
                # MOE readings ARE Taiwan readings; only an override changes them.
                reading = TAIWAN_READINGS.get(word) or TAIWAN_READINGS.get(simplified)
            else:
                reading = db._entry_taiwan_reading(
                    word,
                    simplified,
                    marked,
                    english,
                    options_by_word.get(word, []),
                    counts,
                    char_map,
                )
            reading = reading or marked
            differs = int(to_numeric(reading) != to_numeric(marked))
            total += 1
            changed += differs
            if differs and len(examples) < samples:
                examples.append(f"{word}: {marked} -> {reading}")
            batch.append(
                (
                    anchor,
                    word,
                    simplified,
                    marked,
                    reading,
                    to_numeric(reading),
                    to_toneless(reading),
                    differs,
                )
            )
            if len(batch) >= 50_000:
                out.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?)", batch)
                batch.clear()
        out.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?)", batch)
        print(f"{anchor:12} rows={total:8} changed={changed:7}")
        for example in examples:
            print(f"    {example}")

    # Trigram index so a "contains" search stays fast (needs SQLite >= 3.34).
    try:
        out.execute(
            "CREATE VIRTUAL TABLE readings_fts USING fts5("
            "tw_toneless, content='readings', content_rowid='rowid', "
            "tokenize='trigram')"
        )
        out.execute(
            "INSERT INTO readings_fts(rowid, tw_toneless) "
            "SELECT rowid, tw_toneless FROM readings"
        )
    except sqlite3.OperationalError as error:
        print(f"no trigram index ({error}); contains-search will scan")

    out.execute(
        "INSERT INTO meta VALUES ('signature', ?)", (db.taiwan_signature(),)
    )
    out.execute("INSERT INTO meta VALUES ('built_at', ?)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
    out.commit()
    out.execute("VACUUM")
    out.close()
    os.replace(temporary, output)
    print(f"wrote {output} in {time.time() - started:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=db.TAIWAN_DB_PATH)
    parser.add_argument("--samples", type=int, default=8, help="changed rows to print per source")
    args = parser.parse_args()
    build(args.output, args.samples)


if __name__ == "__main__":
    main()
