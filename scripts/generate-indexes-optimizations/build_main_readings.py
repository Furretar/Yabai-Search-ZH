#!/usr/bin/env python3
"""Generate the fixed headword reading lists: data/generated-indexes-optimizations/main_readings.sqlite.

A word can have several readings (都 dōu / dū / Dū), so one "main" reading is chosen once, here, and the
website only displays it (README, "Headword Readings"). The list never depends on the dictionaries a
visitor has selected. It is used when a search has Chinese characters, for `*` and for the word page; a
pinyin search shows the reading that was searched instead.

Built in this order:

1. MAIN LIST. For a word CC-CEDICT has, the reading with the most senses (the same count the app has
   always ranked readings by: sense_counts.sqlite, cross-references and blank senses excluded; ties go to
   the lowest CEDICT id). For a word CEDICT lacks, the first reading the other dictionaries list, trying
   them in the site's dictionary order (those sources do not count senses per reading).
2. OVERRIDES TO THE MAIN LIST. `PREFERRED_READINGS` (overrides.py, e.g. 了 -> le) replaces the chosen
   reading when the word really has that reading.
3. TAIWAN LIST. The reading of every main reading in Taiwan, by the rules in yabai_search/gen_taiwan_readings.py
   (nothing is generated anywhere else). It starts from the FINAL main reading, so 了 gets the Taiwan
   reading of le and not of liǎo.
4. OVERRIDES TO THE TAIWAN LIST. `TAIWAN_READINGS` (overrides.py, e.g. 血 -> xiě); gen_taiwan_readings.py applies it
   as its first rule.

Two tables, because the page keys a row two ways: `pairs` is one row per (traditional, simplified)
spelling pair (traditional mode); `simplified` is one row per simplified headword (simplified mode,
where the spellings are merged, e.g. 只 and 隻).

Rebuild whenever a source database, sense_counts, overrides.py, gen_taiwan_readings.py or this script changes
(regenerate_everything.py does it; `db.main_readings_table_status()` reports a stale file).

    python scripts/generate-indexes-optimizations/build_main_readings.py [--output PATH] [--samples N]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

from yabai_search import db, gen_taiwan_readings  # noqa: E402
from yabai_search.overrides import PREFERRED_READINGS, TAIWAN_READINGS  # noqa: E402
from yabai_search.pinyin import to_numeric  # noqa: E402

import build_taiwan_readings as taiwan_sources  # noqa: E402  (reads each dictionary's readings)

SCHEMA = """
CREATE TABLE pairs (
    traditional       TEXT NOT NULL,
    simplified        TEXT NOT NULL,
    source            TEXT NOT NULL,     -- dictionary anchor the main reading came from
    reading           TEXT NOT NULL,     -- main reading, marked pinyin
    senses            INTEGER NOT NULL,  -- CEDICT senses of that reading (0 for other sources)
    overridden        INTEGER NOT NULL,  -- 1 when PREFERRED_READINGS chose it
    taiwan            TEXT NOT NULL,     -- main Taiwan reading (the main reading when it has none)
    taiwan_changed    INTEGER NOT NULL,  -- 1 when the Taiwan reading differs from the main one
    taiwan_overridden INTEGER NOT NULL,  -- 1 when TAIWAN_READINGS chose it
    PRIMARY KEY (traditional, simplified)
) WITHOUT ROWID;
CREATE TABLE simplified (
    simplified        TEXT NOT NULL PRIMARY KEY,
    traditional       TEXT NOT NULL,     -- the spelling whose entry gave the reading
    source            TEXT NOT NULL,
    reading           TEXT NOT NULL,
    senses            INTEGER NOT NULL,
    overridden        INTEGER NOT NULL,
    taiwan            TEXT NOT NULL,
    taiwan_changed    INTEGER NOT NULL,
    taiwan_overridden INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def sense_counts() -> dict[tuple[str, str], int]:
    """{(simplified word, marked reading): senses} from sense_counts.sqlite."""
    if not db.SENSE_COUNTS_DB_PATH.exists():
        sys.exit("sense_counts.sqlite is missing: run build_sense_counts.py first")
    connection = sqlite3.connect(f"file:{db.SENSE_COUNTS_DB_PATH}?mode=ro", uri=True)
    try:
        return {
            (word, reading): count
            for word, reading, count in connection.execute(
                "SELECT word, reading, count FROM senses"
            )
        }
    finally:
        connection.close()


def preferred_for(traditional: str, simplified: str) -> str | None:
    """The PREFERRED_READINGS entry for a word, matched on either spelling."""
    return PREFERRED_READINGS.get(traditional) or PREFERRED_READINGS.get(simplified)


def choose(candidates: list[dict], traditional: str, simplified: str) -> tuple[dict, bool]:
    """The main reading among one word's CEDICT rows, and whether an override chose it.

    `candidates` are in CEDICT id order. Most senses wins; a tie keeps the lowest id (max() keeps the
    first of equal keys). The manual override then wins when the word has that reading.
    """
    best = max(candidates, key=lambda row: row["senses"])
    preferred = preferred_for(traditional, simplified)
    if preferred:
        wanted = db.reading_key(preferred)
        for row in candidates:
            if db.reading_key(row["pinyin"]) == wanted:
                return row, row is not best
    return best, False


def cedict_rows(counts: dict[tuple[str, str], int]) -> list[dict]:
    """Every CEDICT row in id order, with its sense count."""
    connection = db._connect(db.DB_PATH)
    try:
        rows = connection.execute(
            "SELECT id, traditional, simplified, pinyin, english_normalized "
            "FROM entries WHERE pinyin <> '' ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "id": row[0],
            "traditional": row[1],
            "simplified": row[2],
            "pinyin": row[3],
            "english": row[4] or "",
            "senses": counts.get((row[2], row[3]), 0),
        }
        for row in rows
    ]


def fallback_rows(skip: set[str]) -> dict[tuple[str, str], tuple[str, str, str]]:
    """{(word, simplified): (anchor, reading, english)} for words CEDICT does not have.

    Dictionaries are tried in the site's order and the first reading listed for a word is kept.
    """
    found: dict[tuple[str, str], tuple[str, str, str]] = {}
    for source in db._SOURCE_QUERIES:
        anchor = source["anchor"]
        if anchor == "cedict":
            continue
        for word, simplified, marked, english in taiwan_sources.source_rows(anchor):
            simplified = simplified or db.to_simplified(word)
            if word in skip or simplified in skip:
                continue
            found.setdefault((word, simplified), (anchor, marked, english))
    return found


def taiwan_for(
    traditional: str,
    simplified: str,
    reading: str,
    english: str,
    anchor: str,
    options: dict[str, list[str]],
    counts: dict[str, int],
    char_map: dict,
) -> tuple[str, int, int]:
    """(Taiwan reading, changed, overridden) of a main reading, by the rules in gen_taiwan_readings.py."""
    overridden = int(bool(TAIWAN_READINGS.get(traditional) or TAIWAN_READINGS.get(simplified)))
    registry = next((item for item in db._SOURCE_QUERIES if item["anchor"] == anchor), {})
    if registry.get("readings_are_taiwan"):
        # Such a dictionary's readings ARE Taiwan readings (the trait in db.py); only an override changes them.
        derived = TAIWAN_READINGS.get(traditional) or TAIWAN_READINGS.get(simplified)
    else:
        derived = gen_taiwan_readings.entry_taiwan_reading(
            traditional, simplified, reading, english, options.get(traditional, []), counts, char_map
        )
    result = derived or reading
    return result, int(to_numeric(result) != to_numeric(reading)), overridden


def build(output: Path, samples: int = 12) -> None:
    started = time.time()
    counts = sense_counts()
    options = taiwan_sources.moe_readings()
    reading_counts = db._reading_counts()
    char_map = db._moe_char_map()

    rows = cedict_rows(counts)
    by_pair: dict[tuple[str, str], list[dict]] = {}
    by_simplified: dict[str, list[dict]] = {}
    for row in rows:
        by_pair.setdefault((row["traditional"], row["simplified"]), []).append(row)
        by_simplified.setdefault(row["simplified"], []).append(row)

    # 1 + 2: the main list (with the main overrides), then 3 + 4: the Taiwan list from it.
    pair_rows: list[tuple] = []
    overridden_samples: list[str] = []
    for (traditional, simplified), candidates in by_pair.items():
        row, forced = choose(candidates, traditional, simplified)
        tw, changed, tw_forced = taiwan_for(
            traditional, simplified, row["pinyin"], row["english"], "cedict", options, reading_counts, char_map
        )
        pair_rows.append(
            (traditional, simplified, "cedict", row["pinyin"], row["senses"], int(forced), tw, changed, tw_forced)
        )
        if forced and len(overridden_samples) < samples:
            overridden_samples.append(f"{traditional}/{simplified}: {row['pinyin']}")

    simplified_rows: list[tuple] = []
    for simplified, candidates in by_simplified.items():
        # The merged simplified row can take its reading from any spelling that has the word.
        traditional = candidates[0]["traditional"]
        row, forced = choose(candidates, traditional, simplified)
        traditional = row["traditional"]
        tw, changed, tw_forced = taiwan_for(
            traditional, simplified, row["pinyin"], row["english"], "cedict", options, reading_counts, char_map
        )
        simplified_rows.append(
            (simplified, traditional, "cedict", row["pinyin"], row["senses"], int(forced), tw, changed, tw_forced)
        )

    # Words CEDICT lacks: the first reading another dictionary lists.
    cedict_words = {row["traditional"] for row in rows} | {row["simplified"] for row in rows}
    seen_simplified = {row[0] for row in simplified_rows}
    extra = fallback_rows(cedict_words)
    for (word, simplified), (anchor, marked, english) in extra.items():
        # These sources list one reading per word, so there is nothing for a manual reading to choose.
        reading, forced = marked, 0
        tw, changed, tw_forced = taiwan_for(
            word, simplified, reading, english, anchor, options, reading_counts, char_map
        )
        pair_rows.append((word, simplified, anchor, reading, 0, forced, tw, changed, tw_forced))
        if simplified not in seen_simplified:
            seen_simplified.add(simplified)
            simplified_rows.append((simplified, word, anchor, reading, 0, forced, tw, changed, tw_forced))

    temporary = output.with_name(output.name + ".tmp")
    output.parent.mkdir(parents=True, exist_ok=True)
    if temporary.exists():
        temporary.unlink()
    out = sqlite3.connect(temporary)
    out.executescript(SCHEMA)
    marks = "?,?,?,?,?,?,?,?,?"
    out.executemany(f"INSERT OR IGNORE INTO pairs VALUES ({marks})", pair_rows)
    out.executemany(f"INSERT OR IGNORE INTO simplified VALUES ({marks})", simplified_rows)
    out.execute("INSERT INTO meta VALUES ('signature', ?)", (db.main_readings_signature(),))
    out.execute("INSERT INTO meta VALUES ('built_at', ?)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
    out.commit()

    print(f"pairs      rows={len(pair_rows):8}  (CEDICT {len(by_pair)}, other dictionaries {len(extra)})")
    print(f"simplified rows={len(simplified_rows):8}  (CEDICT {len(by_simplified)})")
    print(f"main overrides that changed a reading: {sum(r[5] for r in pair_rows)}")
    for example in overridden_samples:
        print(f"    {example}")
    print(f"Taiwan reading differs from the main reading: {sum(r[7] for r in pair_rows)}")
    out.execute("VACUUM")
    out.close()
    os.replace(temporary, output)
    print(f"wrote {output} in {time.time() - started:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=db.MAIN_READINGS_DB_PATH)
    parser.add_argument("--samples", type=int, default=12)
    args = parser.parse_args()
    build(args.output, args.samples)


if __name__ == "__main__":
    main()
