#!/usr/bin/env python3
"""Generate the unified backbone: data/generated-indexes-optimizations/entries.sqlite.

The main (Chinese, pinyin, wildcard) search runs on a table of (word, reading) rows. Until now that table was
CEDICT's, so the other dictionaries' words only reached results through a separate side path, after the CEDICT
rows. This builds ONE table with the same shape for every dictionary, so a word is found, matched and ranked the
same way whichever dictionary it comes from:

* a copy of cedict.sqlite (its `entries`, `senses`, `forms`, `classifiers` and full-text tables, unchanged);
* one more `entries` row per (word, reading) of every other dictionary's words that CEDICT lacks, read through
  the scans in db._SOURCE_SCANS (a word with no reading gets one row with an empty reading), with the same
  columns CEDICT's rows have, the frequency rank from the frequency list, and english = '' (its definitions stay
  in its own dictionary and are attached from there);
* a `source` column saying which dictionary a row came from ('cedict' for CEDICT's own rows);
* the two full-text tables rebuilt over all rows.

db.connect() uses it when `db.entries_table_status()` is "ok" and falls back to CEDICT's file otherwise.
Rebuild whenever a dictionary, the frequency list, the scans or this script changes (regenerate_everything.py
does it).

    python scripts/generate-indexes-optimizations/build_entries.py [--output PATH]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from yabai_search import db  # noqa: E402
from yabai_search.pinyin import to_numeric, to_toneless  # noqa: E402


def frequency_ranks() -> dict[str, int]:
    """{word: rank} from the frequency list (empty when it is missing)."""
    if not db.FREQ_DB_PATH.exists():
        return {}
    connection = sqlite3.connect(f"file:{db.FREQ_DB_PATH}?mode=ro", uri=True)
    try:
        return dict(connection.execute("SELECT word, rank FROM freq"))
    finally:
        connection.close()


def build(output: Path) -> None:
    started = time.time()
    temporary = output.with_name(output.name + ".tmp")
    output.parent.mkdir(parents=True, exist_ok=True)
    for stale in (temporary, Path(str(temporary) + "-journal")):
        if stale.exists():
            stale.unlink()
    shutil.copyfile(db.DB_PATH, temporary)
    connection = sqlite3.connect(temporary)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("ALTER TABLE entries ADD COLUMN source TEXT NOT NULL DEFAULT 'cedict'")

    cedict_rows = connection.execute("SELECT count(*) FROM entries").fetchone()[0]
    # Words CEDICT already has, including the spellings it only knows as variants (a redirect leads them to the main
    # form, which the search follows), so no second row is made for them.
    known = {w for (w,) in connection.execute("SELECT traditional FROM entries UNION SELECT simplified FROM entries")}
    known |= {
        w
        for (w,) in connection.execute(
            "SELECT variant_trad FROM forms UNION SELECT variant_simp FROM forms WHERE variant_simp IS NOT NULL"
        )
    }
    ranks = frequency_ranks()
    next_id = connection.execute("SELECT max(id) FROM entries").fetchone()[0] + 1

    # One candidate per (word, reading): the dictionaries disagree about a word's simplified form (Wiktionary
    # lists 䦯 as 𫔵, Unihan lists it as itself), so the first form that differs from the word wins.
    candidates: dict[tuple[str, str], list] = {}  # (word, reading) -> [source, simplified]
    with_reading: set[str] = set()  # words that have at least one reading row

    def offer(source: str, word: str, simplified: str, marked: str) -> None:
        entry = candidates.get((word, marked))
        if entry is None:
            candidates[(word, marked)] = [source, simplified]
        elif entry[1] == word and simplified != word:
            entry[1] = simplified

    def is_cedict_word(word: str, simplified: str) -> bool:
        """True for a word CEDICT has, or a variant spelling of one (a different code point of the same character,
        or a traditional form of its simplified word): the page reaches those through CEDICT's row."""
        return (
            word in known
            or simplified in known
            or db.to_simplified(word) in known
            or unicodedata.normalize("NFKC", word) in known
        )

    # Pass 1: every reading every other dictionary lists, for words CEDICT does not have.
    sources = [s for s in db._SOURCE_QUERIES if s["anchor"] != db.BACKBONE_ANCHOR]
    for source in sources:
        for word, simplified, marked, _note in db.scan_readings(source, for_entries=True):
            if is_cedict_word(word, simplified):
                continue
            offer(source["anchor"], word, simplified, marked)
            with_reading.add(word)
    # Pass 2: words a dictionary lists with no reading at all still have to be findable by their characters.
    for source in sources:
        # Sorted: set order changes between runs (string hashing), and the row ids are the tie-break of equal results.
        for word in sorted({w for (w,) in db._scan(source, "listed_words") if w}):
            if word in with_reading or is_cedict_word(word, db.to_simplified(word)):
                continue
            offer(source["anchor"], word, db.to_simplified(word), "")
            with_reading.add(word)
    # A word that is just the simplified side of another row with the same reading is the same headword.
    simplified_side = {(entry[1], marked) for (word, marked), entry in candidates.items() if entry[1] != word}
    rows: list[tuple] = []
    per_source: dict[str, int] = {}
    kept = [
        (word, marked, source, simplified)
        for (word, marked), (source, simplified) in sorted(candidates.items())
        if not (simplified == word and (word, marked) in simplified_side)
    ]
    # A source that spells a headword in simplified characters only (Baidu, many Wikipedia titles) must still be found
    # by its traditional spelling: the traditional column of such a row holds the converted word, as CEDICT's does.
    # One OpenCC call over the whole batch is far faster than one per word.
    plain = [word for word, _marked, _source, simplified in kept if simplified == word]
    converted: dict[str, str] = {}
    if plain:
        batch = db.to_traditional("\n".join(plain)).split("\n")
        converted = dict(zip(plain, batch)) if len(batch) == len(plain) else {w: db.to_traditional(w) for w in plain}
    for word, marked, source, simplified in kept:
        traditional = converted.get(word, word) if simplified == word else word
        rank = ranks.get(simplified) or ranks.get(word)
        rows.append(
            (
                next_id, traditional, simplified, marked, to_numeric(marked) if marked else "",
                to_toneless(marked) if marked else "", "", "", None, rank, source,
            )
        )
        next_id += 1
        per_source[source] = per_source.get(source, 0) + 1

    connection.executemany("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    connection.execute("INSERT INTO entries_fts(entries_fts) VALUES('rebuild')")
    connection.execute("INSERT INTO entries_fts_trigram(entries_fts_trigram) VALUES('rebuild')")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source)")
    connection.execute("INSERT OR REPLACE INTO meta VALUES ('signature', ?)", (db.entries_signature(),))
    connection.execute("INSERT OR REPLACE INTO meta VALUES ('unified_rows', ?)", (str(len(rows)),))
    connection.commit()
    connection.execute("ANALYZE")
    connection.close()
    os.replace(temporary, output)
    print(f"CEDICT rows kept: {cedict_rows:,}; rows added: {len(rows):,}")
    for anchor, count in per_source.items():
        print(f"    {anchor:13} {count:>9,}")
    print(f"wrote {output} ({output.stat().st_size / 1e6:.0f} MB) in {time.time() - started:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=db.ENTRIES_DB_PATH)
    build(parser.parse_args().output)


if __name__ == "__main__":
    main()
