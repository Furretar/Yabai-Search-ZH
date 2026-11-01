#!/usr/bin/env python3
"""Build a SQLite dictionary database from the CC-CEDICT text file.

Usage (from the project root):
    uv run python -m scripts.import_cedict
    uv run python -m scripts.import_cedict --limit 5000
    uv run python -m scripts.import_cedict --force
"""

from __future__ import annotations

import argparse
import gzip
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

# Expand CEDICT abbreviations for the normalized column.
from yabai_search.overrides import normalize_english

# Reuse the pinyin helpers so the database gets the same marked/toneless forms
# that the search layer computes at query time.
from yabai_search.pinyin import numeric_to_marked, to_toneless

# Project root is one directory above scripts/.
ROOT = Path(__file__).resolve().parents[1]
# Source file to fetch. The filename is fixed, so this URL always serves MDBG's
# current CC-CEDICT build (no version/date in the name).
DEFAULT_URL = (
    "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"
)
# Where we cache the download, and the database we generate from it.
DEFAULT_CACHE = ROOT / "data" / "dl" / "cedict.txt.gz"
DEFAULT_DB = ROOT / "data" / "cedict.sqlite"

# Matches one CEDICT entry line: "trad simp [pin1 yin1] /def/def/"
#   1 = traditional  2 = simplified  3 = numeric pinyin  4 = slash-joined defs
LINE_RE = re.compile(r"^(\S+)\s+(\S+)\s+\[([^\]]*)\]\s+/(.*)/$")

# SQL that creates the schema. The DROPs make a rebuild clean (the DB file is
# also deleted before this runs, so they mainly guard against custom --db paths).
SCHEMA = """
DROP TABLE IF EXISTS entries;
DROP TABLE IF EXISTS entries_fts;
DROP TABLE IF EXISTS entries_fts_trigram;

CREATE TABLE entries (
    id                 INTEGER PRIMARY KEY,  -- auto row id, also used as FTS rowid
    traditional        TEXT NOT NULL,        -- CEDICT field 1
    simplified         TEXT NOT NULL,        -- CEDICT field 2
    pinyin             TEXT NOT NULL,        -- field 3, tone marks (e.g. shàng yī)
    pinyin_numeric     TEXT NOT NULL,        -- field 3, raw (e.g. shang4 yi1)
    pinyin_toneless    TEXT NOT NULL,        -- field 3, search key (e.g. shangyi)
    english            TEXT NOT NULL,        -- field 4, raw ("sb." kept verbatim)
    english_normalized TEXT NOT NULL,        -- field 4, expanded (sb. -> somebody)
    freq_rank          INTEGER               -- reserved; not filled (CEDICT has none)
);

-- Indexes make lookups on these columns fast.
CREATE INDEX idx_entries_simplified      ON entries(simplified);
CREATE INDEX idx_entries_traditional     ON entries(traditional);
CREATE INDEX idx_entries_pinyin_toneless ON entries(pinyin_toneless);

-- Full-text index over the normalized English definitions. It is "external
-- content": it stores only the index and reads text from `entries`, saving space.
CREATE VIRTUAL TABLE entries_fts USING fts5(
    english_normalized,
    content='entries',
    content_rowid='id',
    tokenize='unicode61'
);

-- Trigram index for substring searches ("reel" inside "reeling"). Unlike the
-- word-token index above, a trigram index can accelerate LIKE '%...%' queries.
-- It covers the definition text and the toneless pinyin reading.
CREATE VIRTUAL TABLE entries_fts_trigram USING fts5(
    english_normalized,
    pinyin_toneless,
    content='entries',
    content_rowid='id',
    tokenize='trigram'
);

-- Pronunciation-note prefixes ("pr. [x]"), extracted at import so pinyin
-- searches can match alternate readings without scanning every definition.
DROP TABLE IF EXISTS alt_prons;
CREATE TABLE alt_prons (
    entry_id INTEGER NOT NULL,
    text     TEXT NOT NULL
);
CREATE INDEX idx_alt_prons_entry ON alt_prons(entry_id);

-- Standardized senses and classifiers. Every source database has tables with
-- these exact shapes, so the app reads definitions and classifiers the same way
-- regardless of the dictionary. `senses` holds the definition split into
-- numbered items; `classifiers` holds the measure words, with traditional and
-- simplified forms kept separately so the Traditional toggle can switch them.
DROP TABLE IF EXISTS senses;
DROP TABLE IF EXISTS classifiers;

CREATE TABLE senses (
    word     TEXT NOT NULL,   -- headword the sense belongs to (simplified)
    reading  TEXT,            -- the tone-marked reading this sense belongs to (e.g. lào)
    position INTEGER NOT NULL,-- 1-based order within the reading
    text     TEXT NOT NULL    -- the sense itself
);
CREATE INDEX idx_senses_word ON senses(word);

CREATE TABLE classifiers (
    word        TEXT NOT NULL, -- headword the classifier applies to
    traditional TEXT,          -- classifier, traditional form
    simplified  TEXT,          -- classifier, simplified form
    codes       TEXT,          -- variety codes (e.g. "m c" from Wiktionary)
    note        TEXT           -- free-text classifier note (e.g. from MOE), else ''
);
CREATE INDEX idx_classifiers_word ON classifiers(word);

-- Variant forms. CEDICT often carries a "see X[reading]" entry for a character
-- that is only an alternative form/reading of another one. Those are recorded
-- here (keyed by the main form) so they show as "other forms", not definitions.
DROP TABLE IF EXISTS forms;
CREATE TABLE forms (
    variant_trad TEXT NOT NULL,  -- the variant's traditional form (distinct key)
    variant_simp TEXT,           -- the variant's simplified form
    main_trad    TEXT NOT NULL,  -- the main form's traditional form
    main_simp    TEXT,           -- the main form's simplified form
    pinyin       TEXT,           -- the variant's own reading
    qualifier    TEXT,           -- "old", "erhua", ... ; empty for a plain variant
    redirect     INTEGER NOT NULL DEFAULT 1
                                 -- 1 = "see X" variant that redirects;
                                 -- 0 = display-only "also written X" form
);
CREATE INDEX idx_forms_variant ON forms(variant_trad);
CREATE INDEX idx_forms_main ON forms(main_trad);

-- Small key/value metadata table (lets the About page show when the data was
-- last updated). Every source database has one.
DROP TABLE IF EXISTS meta;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def download(url: str, dest: Path) -> None:
    """Stream `url` to `dest` so we cache the file instead of re-fetching it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    # Identify ourselves to the server (harmless, but good etiquette).
    request = urllib.request.Request(url, headers={"User-Agent": "yabai-search/0.1"})
    # Read in 1 MiB chunks and write straight through (low memory use).
    with urllib.request.urlopen(request) as response, dest.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    print(f"  saved {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


def open_source(path: Path):
    """Open a .gz or plain-text CEDICT file as a text stream."""
    if path.suffix == ".gz":
        # errors="replace" so a stray bad byte can't crash the whole import.
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


def parse_line(line: str):
    """Parse one CEDICT line into a 7-tuple, or None if it doesn't match."""
    match = LINE_RE.match(line)
    if not match:
        return None  # not a valid entry line (blank/comment/garbage)
    traditional, simplified, pinyin_numeric, raw_defs = match.groups()
    # Definitions are slash-separated; drop empties and join them with "; ".
    english = "; ".join(d for d in raw_defs.split("/") if d)
    # Return values in the exact column order used by the INSERT statement.
    return (
        traditional,
        simplified,
        numeric_to_marked(pinyin_numeric),  # pinyin (tone marks)
        pinyin_numeric,  # pinyin_numeric (raw)
        to_toneless(pinyin_numeric),  # pinyin_toneless (search key)
        english,  # english (raw)
        normalize_english(english),  # english_normalized (expanded)
    )


# A CEDICT classifier token: "個|个[ge4]" or "滴[di1]".
_CL_TOKEN_RE = re.compile(r"^([^|\[]+?)(?:\|([^|\[]+?))?\[[^\]]+\]$")
# A definition item that is really a note (pronunciation/register), not a sense.
_NOTE_RE = re.compile(
    r"^(?:Taiwan|Beijing|also|old|ancient|colloquial|coll\.|commonly|Mandarin)?\s*"
    r"(?:pr\.|pronounced)"
)


# Qualifier words that can precede "see" / "variant of" in a cross-reference,
# e.g. "old variant of 柿[shi4]" or "(literary) see 倘[tang3]". Kept explicit
# so an English sense that merely contains the word "see" is not misread.
_VARIANT_QUALIFIERS = (
    "old|ancient|archaic|erhua|taiwan|euphemistic|popular|"
    "nonstandard|simplified|classical|obscure|colloquial|literary|"
    "less common|contracted|emphatic|japanese|korean|vietnamese|"
    "cantonese|hakka|dialect|tw|hk|slang|idiom"
)
# A cross-reference-only definition: "see X[reading]" or "variant of X[reading]",
# with optional qualifiers. CEDICT uses these for forms that are only an
# alternative of another character.
_SEE_RE = re.compile(
    rf"^((?:\([^)]*\)\s*)*(?:(?:{_VARIANT_QUALIFIERS})\s+)*)"
    r"(?:see(?:\s+also)?|variant of)\s+"
    r"([^\[\];()\s]+?)(?:\[[^\]]+\])?$",
    re.IGNORECASE,
)


def see_target(english: str) -> tuple[str, str, str] | None:
    """The main form from a "see X" / "variant of X" cross-reference.

    Returns `(traditional, simplified, qualifier)`, where the qualifier is the
    text before "variant of" ("old", "erhua", "(literary)", ...) or an empty
    string for a plain variant. Such entries are only an alternative
    form/reading of another; we record them as "other forms", not definitions.
    """
    match = _SEE_RE.match(english.strip())
    if not match:
        return None
    qualifier = " ".join(match.group(1).split()).replace("(", "").replace(")", "")
    target = match.group(2)
    if "|" in target:  # written "trad|simp"
        traditional, simplified = target.split("|", 1)
        return traditional, simplified, qualifier
    return target, target, qualifier


# A variant note like "also written 沒精打彩|没精打彩[mei2 jing1 da3 cai3]".
# These are not senses: the variant belongs under "Other forms" instead.
_ALSO_WRITTEN_RE = re.compile(r"^also written\s+(.+)$", re.IGNORECASE)


def also_written_target(item: str) -> tuple[str, str, str] | None:
    """The (traditional, simplified, pinyin) from an "also written X" note.

    Returns None for the ordinary sense "also written as" (the definition of
    亦作) and for targets we cannot parse as a single headword. The pinyin is
    tone-marked when the note supplies it, otherwise it is empty.
    """
    if item.strip().lower() == "also written as":
        return None
    match = _ALSO_WRITTEN_RE.match(item.strip())
    if not match:
        return None
    body = match.group(1).strip()
    # Drop a trailing ", see also ..." cross-reference for this purpose.
    cut = body.lower().find(", see also")
    if cut != -1:
        body = body[:cut].strip()
    pinyin = ""
    pm = re.search(r"\[([^\]]+)\]", body)
    if pm:
        pinyin = numeric_to_marked(pm.group(1))
        body = body[: pm.start()].strip()
    if not body:
        return None
    if "|" in body:
        traditional, simplified = body.split("|", 1)
    else:
        traditional = simplified = body
    # A variant is one token; reject prose such as "Tuoketuo 托克托".
    for value in (traditional, simplified):
        if not value or re.search(r"[\s\[\];()]", value):
            return None
    return traditional, simplified, pinyin


def split_senses(text: str) -> list[str]:
    """Split a joined definition into clean, numbered senses.

    CEDICT joins senses with "; ". Classifier lists ("CL:...") and pronunciation
    notes ("also pr. [...]") are not senses, so they are dropped here (the
    classifiers are stored separately).
    """
    senses = []
    for item in text.split(";"):
        item = item.strip()
        if not item or item.startswith("CL:") or _NOTE_RE.match(item):
            continue
        senses.append(item)
    return senses


def extract_classifiers(text: str) -> list[tuple[str, str]]:
    """(traditional, simplified) pairs for every CEDICT "CL:" classifier."""
    found = []
    for match in re.finditer(r"CL:\s*([^;]+)", text):
        for token in match.group(1).split(","):
            token = token.strip()
            tm = _CL_TOKEN_RE.match(token)
            if tm:
                found.append((tm.group(1), tm.group(2) or tm.group(1)))
    return found


def build(source: Path, db_path: Path, limit: int | None) -> None:
    """(Re)build the SQLite database from `source`."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Full wipe: delete the old DB file so every run starts from scratch.
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA)  # Create the tables and indexes
    # Speed up a bulk load: disable crash-safe journaling while importing.
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    # CEDICT has no date in the file, so record when this snapshot was built.
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('updated', ?)",
        (time.strftime("%Y-%m-%d"),),
    )

    insert = (
        "INSERT INTO entries "
        "(traditional, simplified, pinyin, pinyin_numeric, pinyin_toneless, "
        " english, english_normalized) "
        "VALUES (?,?,?,?,?,?,?)"
    )
    insert_sense = "INSERT INTO senses (word, reading, position, text) VALUES (?,?,?,?)"
    insert_classifier = (
        "INSERT INTO classifiers (word, traditional, simplified, codes, note) "
        "VALUES (?,?,?,?,?)"
    )
    insert_form = (
        "INSERT INTO forms "
        "(variant_trad, variant_simp, main_trad, main_simp, pinyin, qualifier, "
        " redirect) "
        "VALUES (?,?,?,?,?,?,?)"
    )
    insert_alt = "INSERT INTO alt_prons (entry_id, text) VALUES (?,?)"
    rows: list[tuple] = []  # buffer of entry rows waiting to be written
    sense_rows: list[tuple] = []  # (word, position, text)
    cls_rows: list[tuple] = []  # (word, traditional, simplified, codes, note)
    form_rows: list[tuple] = []  # (variant, main, pinyin, qualifier, redirect)
    alt_rows: list[tuple] = []  # (entry_id, "pr. [content")
    kept = 0  # entries accepted so far
    skipped = 0  # lines that didn't match the expected format
    started = time.time()

    # A pronunciation note, e.g. "also pr. [shui2]" or "Taiwan pr. [xi1]". The
    # bracket content is stored lowercased with its "pr. [" prefix, matching the
    # app's "pr. [<query>" search pattern (SQLite LIKE is ASCII-case-insensitive).
    _PRON_RE = re.compile(r"pr\. \[([^\]]*)\]", re.IGNORECASE)

    def flush() -> None:
        """Write every buffered batch and clear the buffers."""
        connection.executemany(insert, rows)
        # The batch's entry ids are consecutive: derive them from the last id.
        last_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        first_id = last_id - len(rows) + 1
        for offset, row in enumerate(rows):
            for match in _PRON_RE.finditer(row[6]):
                alt_rows.append((first_id + offset, "pr. [" + match.group(1).lower()))
        connection.executemany(insert_alt, alt_rows)
        connection.executemany(insert_sense, sense_rows)
        connection.executemany(insert_classifier, cls_rows)
        connection.executemany(insert_form, form_rows)
        rows.clear()
        sense_rows.clear()
        cls_rows.clear()
        form_rows.clear()
        alt_rows.clear()

    with open_source(source) as handle:
        for line in handle:
            # Skip blanks and comment/header lines (they start with "#").
            if not line or line.startswith("#"):
                continue
            row = parse_line(line.rstrip("\n"))
            if row is None:
                skipped += 1
                continue
            traditional, simplified, pinyin, _numeric, _toneless, english, normalized = row
            rows.append(row)
            target = see_target(english)
            if target:
                # A "see X" / "variant of X" entry is a variant of the main form X:
                # record it (so X lists the variant under "Other forms", and the
                # variant itself shows X's result). Keyed by the traditional forms,
                # which are distinct (e.g. 軟 vs 輭) even when simplified collide.
                main_trad, main_simp, qualifier = target
                form_rows.append(
                    (
                        traditional,
                        simplified,
                        main_trad,
                        main_simp,
                        pinyin,
                        qualifier,
                        1,
                    )
                )
            else:
                # Standardized senses (1., 2., ...) and classifiers, keyed by the
                # simplified headword and tagged with their reading, so polyphonic
                # words can be shown per reading. An "also written X" note is a
                # form, not a sense, so it becomes a display-only forms row.
                senses = []
                for item in split_senses(normalized):
                    note = also_written_target(item)
                    if note:
                        note_trad, note_simp, note_pinyin = note
                        form_rows.append(
                            (
                                note_trad,
                                note_simp,
                                traditional,
                                simplified,
                                note_pinyin,
                                "",
                                0,
                            )
                        )
                    else:
                        senses.append(item)
                for position, sense in enumerate(senses, start=1):
                    sense_rows.append((simplified, pinyin, position, sense))
                for trad, simp in extract_classifiers(english):
                    cls_rows.append((simplified, trad, simp, "", ""))
            kept += 1
            # Flush every 5000 rows: fewer, larger transactions is faster.
            if len(rows) >= 5000:
                flush()
            # Stop early when the caller asked for a limited test import.
            if limit and kept >= limit:
                break
    if rows:
        flush()  # flush the last partial batch

    # Populate the full-text indexes from the normalized English column and the
    # pinyin search key.
    print("building full-text indexes...")
    connection.execute(
        "INSERT INTO entries_fts(rowid, english_normalized) "
        "SELECT id, english_normalized FROM entries"
    )
    connection.execute(
        "INSERT INTO entries_fts_trigram(rowid, english_normalized, pinyin_toneless) "
        "SELECT id, english_normalized, pinyin_toneless FROM entries"
    )
    # Fill freq_rank from SUBTLEX once, so searches order by frequency without a
    # correlated subquery per row. The meta flag tells the app the column is
    # usable; without it the app falls back to the live subquery.
    subtlex = ROOT / "data" / "subtlex.sqlite"
    if subtlex.exists():
        source = sqlite3.connect(subtlex)
        try:
            mapping = dict(source.execute("SELECT word, rank FROM freq").fetchall())
        finally:
            source.close()
        connection.executemany(
            "UPDATE entries SET freq_rank = ? WHERE id = ?",
            [
                (mapping.get(simplified), entry_id)
                for entry_id, simplified in connection.execute(
                    "SELECT id, simplified FROM entries"
                )
            ],
        )
        connection.execute("INSERT INTO meta (key, value) VALUES ('freq_rank', '1')")
        print(f"filled freq_rank from {subtlex}")
    else:
        print("note: subtlex.sqlite not found; freq_rank left empty")
    connection.commit()
    count = connection.execute("SELECT count(*) FROM entries").fetchone()[0]
    # VACUUM repacks the file compactly and reclaims unused space.
    connection.execute("VACUUM")
    connection.close()

    print(
        f"done: {count} entries in {time.time() - started:.1f}s "
        f"({skipped} lines skipped)"
    )
    print(f"database: {db_path} ({db_path.stat().st_size / 1e6:.1f} MB)")


def main() -> int:
    """Parse command-line arguments and run the import."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--input", type=Path, help="use a local CEDICT file")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, help="only import the first N entries")
    parser.add_argument("--force", action="store_true", help="re-download")
    args = parser.parse_args()

    if args.input:
        source = args.input  # the user pointed us at a local file
    else:
        source = DEFAULT_CACHE
        # Download only if there's no cached copy, or the user forced a refresh.
        if args.force or not source.exists():
            download(args.url, source)

    build(source, args.db, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
