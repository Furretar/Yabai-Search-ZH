#!/usr/bin/env python3
"""Import the two Taiwan MOE Mandarin dictionaries into ONE SQLite database.

  concised - 國語辭典簡編本     tables moe, senses, examples, classifiers, senses_fts
  revised  - 重編國語辭典修訂本  the same tables with an "rev_" prefix (rev_moe, ...)

Both are written to data/moe-taiwan/moe.sqlite. The raw downloads (zips, see
scripts/download-dictionaries/download_moe.py) stay untouched in data/dl-base-dictionaries/.

Each download is a zip containing a single .xlsx. This reads that sheet with the
standard library (no openpyxl) and builds the same small schema for both.

Usage (from the project root):
    uv run python scripts/create-dict-db/import_moe.py                  # both dictionaries
    uv run python scripts/create-dict-db/import_moe.py --kind revised   # only the revised tables
    uv run python scripts/create-dict-db/import_moe.py --kind concised --input path/to/file.zip
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


ROOT = Path(__file__).resolve().parents[2]
# Raw downloads (the zips) live here; the merged database lives in moe-taiwan/.
DL_DIR = ROOT / "data" / "dl-base-dictionaries"
MOE_DIR = ROOT / "data" / "moe-taiwan"
MOE_DB = MOE_DIR / "moe.sqlite"

# Table-name prefix of each dictionary inside the merged database. The concise
# dictionary keeps the plain names, so the old single-file queries still work.
PREFIX = {"concised": "", "revised": "rev_"}

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Column positions in each sheet (0-based). The revised dictionary has a few
# extra columns before the main reading and definition.
COLUMNS = {
    "concised": {
        "word": 0,  # 字詞名
        "radical": 2,  # 部首字
        "strokes": 3,  # 總筆畫數
        "zhuyin": 6,  # 注音一式
        "pinyin": 9,  # 漢語拼音
        "definition": 13,  # 釋義
    },
    "revised": {
        "word": 0,  # 字詞名
        "radical": 4,  # 部首字
        "strokes": 5,  # 總筆畫數
        "zhuyin": 8,  # 注音一式
        "pinyin": 11,  # 漢語拼音
        "definition": 15,  # 釋義
    },
}


def latest_zip(kind: str) -> Path:
    """Return the newest downloaded text zip of a dictionary.

    The name must be dict_<kind>_<edition>_<date>.zip. A plain glob on
    "dict_revised_*" would also match the glyph-picture archive
    (dict_revised_pic_...), which sorts after it and is not the text data.
    """
    pattern = re.compile(rf"dict_{kind}_\d+_\d{{8}}\.zip$")
    matches = sorted(p for p in DL_DIR.glob(f"dict_{kind}_*.zip") if pattern.match(p.name))
    if not matches:
        raise FileNotFoundError(
            f"no dict_{kind}_*.zip in {DL_DIR} - run: uv run python scripts/download-dictionaries/download_moe.py {kind}"
        )
    return matches[-1]


# Where the extracted glyph images live, served by GET /moe-glyphs/<name>.
GLYPH_DIR = MOE_DIR / "moe_glyphs"
# The 字圖 archive's inner folder ends with this (the glyph pictures for
# characters UTF-8 cannot represent, referenced as "&8ff0._104_0.gif;").
_GLYPH_FOLDER_RE = re.compile(r"utf8.*字圖/$")


def extract_glyphs(kind: str) -> int:
    """Unpack the 字圖 (glyph image) zip into data/moe-taiwan/moe_glyphs/.

    Definitions reference these as "&8ff0._104_0.gif;"; the app swaps them for
    the picture, coloured like text. No picture zip (or none downloaded yet)
    is not an error - the definitions just keep showing the raw reference.
    Returns the number of files written (0 when there is no picture zip).
    """
    # e.g. dict_revised_pic_2015_20260929.zip, downloaded alongside the text zip.
    pic_zips = sorted(DL_DIR.glob(f"dict_{kind}_pic_*.zip"))
    if not pic_zips:
        return 0
    GLYPH_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    with zipfile.ZipFile(pic_zips[-1]) as archive:
        for name in archive.namelist():
            # Keep only the images inside the glyph folder (skip the mapping
            # xlsx, the font, the PDF and Thumbs.db).
            leaf = name.rsplit("/", 1)[-1]
            if not leaf.lower().endswith((".gif", ".png")):
                continue
            folder = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
            if not _GLYPH_FOLDER_RE.search(folder):
                continue
            (GLYPH_DIR / leaf).write_bytes(archive.read(name))
            written += 1
    return written


# Tables of one dictionary; `{p}` is its prefix ("" or "rev_").
TABLES = ("moe", "senses", "examples", "classifiers", "senses_fts")

SCHEMA = """
CREATE TABLE {p}moe (
    id         INTEGER PRIMARY KEY,
    word       TEXT NOT NULL,   -- 字詞名
    zhuyin     TEXT,            -- 注音一式
    pinyin     TEXT,            -- 漢語拼音
    radical    TEXT,            -- 部首字
    strokes    INTEGER,         -- 總筆畫數
    definition TEXT             -- 釋義
);
CREATE INDEX idx_{p}moe_word ON {p}moe(word);

-- Standardized senses and classifiers (same shape as every other source DB).
-- The 釋義 is split on newlines and its leading "1." "2." numbers are dropped;
-- "量詞..." senses are stored as classifier notes instead.
CREATE TABLE {p}senses (
    word     TEXT NOT NULL,
    position INTEGER NOT NULL,
    text     TEXT NOT NULL
);
CREATE INDEX idx_{p}senses_word ON {p}senses(word);

-- Trigram full-text index over the sense text. FTS5's trigram tokenizer makes
-- LIKE '%…%' use the index for queries of three or more characters, so quoted
-- definition searches do not scan the whole table.
CREATE VIRTUAL TABLE {p}senses_fts USING fts5(word, text, tokenize='trigram');

-- Examples ("[例]…") split out of the senses, kept separate for now.
CREATE TABLE {p}examples (
    word     TEXT NOT NULL,
    position INTEGER NOT NULL,
    text     TEXT NOT NULL
);
CREATE INDEX idx_{p}examples_word ON {p}examples(word);

CREATE TABLE {p}classifiers (
    word        TEXT NOT NULL,
    traditional TEXT,
    simplified  TEXT,
    codes       TEXT,
    note        TEXT
);
CREATE INDEX idx_{p}classifiers_word ON {p}classifiers(word);
"""

# Metadata shared by both (the About page shows when each was last updated):
# key "updated" is the concise dictionary, "rev_updated" the revised one.
META_SCHEMA = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"


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
    # The revised zip also contains a column-description workbook.
    xlsx_name = next(
        name
        for name in outer.namelist()
        if name.endswith(".xlsx") and "欄位" not in name
    )
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
    return text.replace("_x000D_", "").replace("　", " ").strip()


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
# The revised dictionary prefixes some lines with a part-of-speech tag, e.g.
# "[名]" or "[動]".
_POS_RE = re.compile(r"^\s*\[[^\]]+\]\s*")


# "(1)", "(2)" ... (half or full width) number the uses inside one 量詞 sense.
_SUBSENSE_RE = re.compile(r"[(（]\s*\d+\s*[)）]")


def split_classifier_sense(item: str) -> list[str]:
    """Split "量詞：(1)計算A。[例]x (2)計算B。" into "量詞：計算A。[例]x" and "量詞：計算B。".

    The text before "(1)" (the revised dictionary's "量詞：同「隻」。") is its own sense. A sense
    without numbered parts is returned unchanged.
    """
    parts = _SUBSENSE_RE.split(item)
    if len(parts) < 2:
        return [item]
    head = parts[0].strip()
    prefix = re.match(r"量詞\s*[：:。]?", head).group(0).rstrip("。").strip()
    prefix = prefix if prefix.endswith(("：", ":")) else "量詞："
    out = []
    # Text between "量詞：" and "(1)" (e.g. "同「隻」。") stands alone.
    lead = head[len(re.match(r"量詞\s*[：:。]?", head).group(0)):].strip()
    if lead:
        out.append(f"{prefix}{lead}")
    out.extend(f"{prefix}{part.strip()}" for part in parts[1:] if part.strip())
    return out


def split_senses(
    definition: str, revised: bool = False
) -> tuple[list[str], list[str]]:
    """Return (senses, classifier_notes) from an MOE 釋義.

    Senses are split on newlines with the leading "1." numbering removed.
    Senses that describe the word as a 量詞 are returned separately as classifier
    notes (so they aren't repeated in the numbered list). In the revised
    dictionary, a line without its own number is a continuation of the previous
    sense, and a bare part-of-speech tag is skipped.
    """
    senses: list[str] = []
    notes: list[str] = []
    for line in definition.split("\n"):
        numbered = bool(_SENSE_NUM_RE.match(line))
        item = _SENSE_NUM_RE.sub("", line).strip()
        if revised:
            item = _POS_RE.sub("", item).strip()
        if not item:
            continue
        if item.startswith("量詞"):
            # A 量詞 sense says what the word IS ("計算船隻的單位"), so it stays a numbered sense; the
            # Classifier line is for the classifiers a word takes. A "(1)…(2)…" run becomes one sense each.
            senses.extend(split_classifier_sense(item))
        elif revised and not numbered and senses:
            senses[-1] = f"{senses[-1]} {item}".strip()
        else:
            senses.append(item)
    return senses, notes


def build_kind(connection: sqlite3.Connection, zip_path: Path, kind: str) -> int:
    """(Re)build one dictionary's tables inside the open merged database."""
    columns = COLUMNS[kind]
    prefix = PREFIX[kind]
    # Only this dictionary's tables are dropped, so the other one is left alone.
    for table in TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {prefix}{table}")
    connection.executescript(SCHEMA.format(p=prefix))
    connection.execute(META_SCHEMA)

    # The MOE download's filename carries the data date, e.g.
    # "dict_concised_2014_20260929.zip" -> 2026-09-29.
    match = re.search(r"(\d{4})(\d{2})(\d{2})", zip_path.name)
    updated = (
        f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        if match
        else time.strftime("%Y-%m-%d")
    )
    connection.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (f"{prefix}updated", updated),
    )

    rows = []
    sense_rows = []  # (word, position, text)
    example_rows = []  # (word, position, text)
    cls_rows = []  # (word, traditional, simplified, codes, note)
    for values in read_rows(zip_path):
        word = values.get(columns["word"], "").strip()
        if not word or word == "字詞名":  # skip the header row
            continue
        strokes = values.get(columns["strokes"], "")
        definition = clean_definition(values.get(columns["definition"], ""))
        rows.append(
            (
                word,
                values.get(columns["zhuyin"], "").strip(),
                values.get(columns["pinyin"], "").strip(),
                values.get(columns["radical"], "").strip(),
                int(strokes) if strokes.isdigit() else None,
                definition,
            )
        )
        # Store the numbered senses (without their "1." prefixes) and the 量詞
        # notes as standardized classifier rows.
        senses, notes = split_senses(definition, revised=(kind == "revised"))
        for position, sense in enumerate(senses, start=1):
            definition_text, example = split_example(sense)
            sense_rows.append((word, position, definition_text))
            if example:
                example_rows.append((word, position, example))
        for note in notes:
            cls_rows.append((word, None, None, "", note))

    connection.executemany(
        f"INSERT INTO {prefix}moe (word, zhuyin, pinyin, radical, strokes, definition) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    connection.executemany(
        f"INSERT INTO {prefix}senses (word, position, text) VALUES (?,?,?)", sense_rows
    )
    connection.executemany(
        f"INSERT INTO {prefix}examples (word, position, text) VALUES (?,?,?)",
        example_rows,
    )
    connection.executemany(
        f"INSERT INTO {prefix}classifiers (word, traditional, simplified, codes, note) "
        "VALUES (?,?,?,?,?)",
        cls_rows,
    )
    connection.execute(
        f"INSERT INTO {prefix}senses_fts (word, text) SELECT word, text FROM {prefix}senses"
    )
    connection.commit()
    return connection.execute(f"SELECT count(*) FROM {prefix}moe").fetchone()[0]


def build(db_path: Path, jobs: list[tuple[str, Path]], fresh: bool) -> None:
    """Write the given (kind, zip) jobs into the merged database at db_path.

    fresh=True starts from an empty file (a full rebuild of both dictionaries);
    otherwise the existing file is opened and only the named kinds are replaced.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if fresh and db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        for kind, zip_path in jobs:
            total = build_kind(connection, zip_path, kind)
            print(f"imported {total} {kind} entries from {zip_path.name}")
        connection.execute("VACUUM")
    finally:
        connection.close()
    print(f"wrote {db_path} ({db_path.stat().st_size / 1e6:.1f} MB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=[*COLUMNS, "all"], default="all")
    parser.add_argument("--input", type=Path, help="zip to read (one --kind only)")
    parser.add_argument("--db", type=Path, default=MOE_DB)
    args = parser.parse_args()
    kinds = list(COLUMNS) if args.kind == "all" else [args.kind]
    if args.input and len(kinds) != 1:
        parser.error("--input needs --kind concised or --kind revised")
    try:
        jobs = [(kind, args.input or latest_zip(kind)) for kind in kinds]
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1
    build(args.db, jobs, fresh=(args.kind == "all"))
    # The revised dictionary's definitions embed glyph pictures the text zip
    # cannot carry ("&8ff0._104_0.gif;"); unpack them next to the database.
    if "revised" in kinds:
        glyphs = extract_glyphs("revised")
        if glyphs:
            print(f"extracted {glyphs} glyph images into {GLYPH_DIR}")
    return 0


if __name__ == "__main__":
    import _indexes  # rebuilds the derived indexes when the import is done (--no-indexes skips it)

    sys.exit(_indexes.run(main))
