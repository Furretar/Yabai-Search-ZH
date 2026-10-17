#!/usr/bin/env python3
"""Import the MOE 國語辭典簡編本 (concised) spreadsheet into data/moe.sqlite.

The download is a zip containing a single .xlsx. This reads that sheet with the
standard library (no openpyxl) and builds a small SQLite table.

Usage (from the project root):
    uv run python -m scripts.import_moe
    uv run python -m scripts.import_moe --input data/dict_concised_2014_20260929.zip
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ZIP = ROOT / "data" / "dict_concised_2014_20260929.zip"
DEFAULT_DB = ROOT / "data" / "moe.sqlite"

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Column positions in the sheet (0-based).
COL_WORD = 0  # 字詞名
COL_RADICAL = 2  # 部首字
COL_STROKES = 3  # 總筆畫數
COL_ZHUYIN = 6  # 注音一式
COL_PINYIN = 9  # 漢語拼音
COL_DEFINITION = 13  # 釋義

SCHEMA = """
DROP TABLE IF EXISTS moe;

CREATE TABLE moe (
    id         INTEGER PRIMARY KEY,
    word       TEXT NOT NULL,   -- 字詞名
    zhuyin     TEXT,            -- 注音一式
    pinyin     TEXT,            -- 漢語拼音
    radical    TEXT,            -- 部首字
    strokes    INTEGER,         -- 總筆畫數
    definition TEXT             -- 釋義
);

CREATE INDEX idx_moe_word ON moe(word);

-- Standardized senses and classifiers (same shape as every other source DB).
-- The 釋義 is split on newlines and its leading "1." "2." numbers are dropped;
-- "量詞..." senses are stored as classifier notes instead.
DROP TABLE IF EXISTS senses;
DROP TABLE IF EXISTS classifiers;

CREATE TABLE senses (
    word     TEXT NOT NULL,
    position INTEGER NOT NULL,
    text     TEXT NOT NULL
);
CREATE INDEX idx_senses_word ON senses(word);

-- Trigram full-text index over the sense text. FTS5's trigram tokenizer makes
-- LIKE '%…%' use the index for queries of three or more characters, so quoted
-- definition searches do not scan the whole table.
DROP TABLE IF EXISTS senses_fts;
CREATE VIRTUAL TABLE senses_fts USING fts5(word, text, tokenize='trigram');

-- Examples ("[例]…") split out of the senses, kept separate for now.
CREATE TABLE examples (
    word     TEXT NOT NULL,
    position INTEGER NOT NULL,
    text     TEXT NOT NULL
);
CREATE INDEX idx_examples_word ON examples(word);

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


def _column_index(ref: str) -> int:
    """Turn an xlsx cell reference's letters into a 0-based column index."""
    letters = re.match(r"[A-Z]+", ref).group()
    number = 0
    for char in letters:
        number = number * 26 + (ord(char) - 64)
    return number - 1


def read_rows(zip_path: Path):
    """Yield {column_index: value} dicts for every row of the sheet."""
    outer = zipfile.ZipFile(zip_path)
    xlsx_name = next(name for name in outer.namelist() if name.endswith(".xlsx"))
    workbook = zipfile.ZipFile(outer.open(xlsx_name))

    shared: list[str] = []
    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    for si in root.findall(f"{NS}si"):
        shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))

    sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    for row in sheet.findall(f".//{NS}row"):
        values: dict[int, str] = {}
        for cell in row.findall(f"{NS}c"):
            value = cell.find(f"{NS}v")
            if value is None:
                continue
            if cell.get("t") == "s":
                values[_column_index(cell.get("r"))] = shared[int(value.text)]
            else:
                values[_column_index(cell.get("r"))] = value.text
        yield values


def clean_definition(text: str) -> str:
    """Tidy the 釋義 cell (strip the _x000D_ markers and full-width padding)."""
    return text.replace("_x000D_", "").replace("\u3000", " ").strip()


# MOE appends examples to a sense as "[例]…".
_EXAMPLE_RE = re.compile(r"\s*[\[【﹝]例[\]】﹞]\s*(.*)$")


def split_example(sense: str) -> tuple[str, str | None]:
    """Split a sense into (definition, example); example is None when absent."""
    match = _EXAMPLE_RE.search(sense)
    if not match:
        return sense, None
    example = match.group(1).strip().rstrip("◎").strip()
    return sense[: match.start()].strip(), (example or None)


# MOE numbers each sense "1. 2. 3." on its own line.
_SENSE_NUM_RE = re.compile(r"^\s*\d+\s*[.、]\s*")


def split_senses(definition: str) -> tuple[list[str], list[str]]:
    """Return (senses, classifier_notes) from an MOE 釋義.

    Senses are split on newlines with the leading "1." numbering removed.
    Senses that describe the word as a 量詞 are returned separately as classifier
    notes (so they aren't repeated in the numbered list).
    """
    senses: list[str] = []
    notes: list[str] = []
    for line in definition.split("\n"):
        item = _SENSE_NUM_RE.sub("", line).strip()
        if not item:
            continue
        (notes if item.startswith("量詞") else senses).append(item)
    return senses, notes


def build(zip_path: Path, db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA)
    # The MOE download's filename carries the data date, e.g.
    # "dict_concised_2014_20260929.zip" -> 2026-09-29.
    match = re.search(r"(\d{4})(\d{2})(\d{2})", zip_path.name)
    updated = (
        f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        if match
        else time.strftime("%Y-%m-%d")
    )
    connection.execute("INSERT INTO meta (key, value) VALUES ('updated', ?)", (updated,))

    rows = []
    sense_rows = []  # (word, position, text)
    example_rows = []  # (word, position, text)
    cls_rows = []  # (word, traditional, simplified, codes, note)
    count = 0
    for values in read_rows(zip_path):
        word = values.get(COL_WORD, "").strip()
        if not word or word == "字詞名":  # skip the header row
            continue
        strokes = values.get(COL_STROKES, "")
        definition = clean_definition(values.get(COL_DEFINITION, ""))
        rows.append(
            (
                word,
                values.get(COL_ZHUYIN, "").strip(),
                values.get(COL_PINYIN, "").strip(),
                values.get(COL_RADICAL, "").strip(),
                int(strokes) if strokes.isdigit() else None,
                definition,
            )
        )
        # Store the numbered senses (without their "1." prefixes) and the 量詞
        # notes as standardized classifier rows.
        senses, notes = split_senses(definition)
        for position, sense in enumerate(senses, start=1):
            definition_text, example = split_example(sense)
            sense_rows.append((word, position, definition_text))
            if example:
                example_rows.append((word, position, example))
        for note in notes:
            cls_rows.append((word, None, None, "", note))
        count += 1

    connection.executemany(
        "INSERT INTO moe (word, zhuyin, pinyin, radical, strokes, definition) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    connection.executemany(
        "INSERT INTO senses (word, position, text) VALUES (?,?,?)", sense_rows
    )
    connection.executemany(
        "INSERT INTO examples (word, position, text) VALUES (?,?,?)", example_rows
    )
    connection.executemany(
        "INSERT INTO classifiers (word, traditional, simplified, codes, note) "
        "VALUES (?,?,?,?,?)",
        cls_rows,
    )
    connection.execute(
        "INSERT INTO senses_fts (word, text) SELECT word, text FROM senses"
    )
    connection.commit()
    connection.execute("VACUUM")
    total = connection.execute("SELECT count(*) FROM moe").fetchone()[0]
    connection.close()
    print(f"imported {total} entries into {db_path} ({db_path.stat().st_size / 1e6:.1f} MB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_ZIP)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    build(args.input, args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
