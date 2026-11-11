#!/usr/bin/env python3
"""Build data/unihan.sqlite from the Chinese parts of the Unicode Han Database.

Unihan (Unicode Standard Annex #38) has one line per character and property. Only the
Chinese-language data is kept: Mandarin and Cantonese readings, Middle Chinese (Tang) and
fanqie readings, the English gloss, simplified/traditional and other variants, radical and
stroke counts, Chinese input and lookup codes, and numeric values. Japanese, Korean,
Vietnamese, Zhuang and Tay readings, character-set codes and dictionary page numbers are
left out (see KEEP below).

Tables:
  headwords(word)                    every character Unihan covers, with or without readings
  chars(word, mandarin, cantonese, definition, total_strokes, radical_stroke,
        simplified, traditional)     one convenient row per character
  fields(word, field, value)         every kept property exactly as Unihan gives it (lossless)
  senses(word, position, text)       kDefinition split into senses for the source chip
  classifiers(...)                   empty: Unihan has no measure words
  meta(key, value)                   updated = Unihan's own date, unicode_version, licence

Usage (from the project root):
    uv run python -m scripts.download_unihan
    uv run python -m scripts.import_unihan

Licence: Unicode License V3 (https://www.unicode.org/license.txt); keep the Unicode
copyright and permission notice with the data or its documentation.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ZIP = ROOT / "data" / "unihan" / "Unihan.zip"
DEFAULT_DB = ROOT / "data" / "unihan.sqlite"

# The Chinese-language properties that are kept. Everything else in the archive is skipped.
KEEP = {
    # Readings: Mandarin (several sources), Cantonese, Middle Chinese, and the English gloss.
    "kMandarin", "kHanyuPinyin", "kHanyuPinlu", "kXHC1983", "kTGHZ2013",
    "kCantonese", "kSMSZD2003Readings", "kTang", "kFanqie", "kDefinition",
    # Variants (the Japanese new/old forms are left out).
    "kSimplifiedVariant", "kTraditionalVariant", "kSemanticVariant",
    "kSpecializedSemanticVariant", "kZVariant", "kSpoofingVariant",
    # Radical-stroke index and stroke counts.
    "kRSUnicode", "kTotalStrokes", "kAlternateTotalStrokes",
    # Chinese input and lookup codes, school grade and core-set data.
    "kCangjie", "kFourCornerCode", "kPhonetic", "kGradeLevel", "kUnihanCore2020",
    "kHKGlyph", "kCheungBauer", "kFenn", "kHDZRadBreak",
    # Numeric meanings of Chinese numerals (not the Tay, Vietnamese or Zhuang ones).
    "kAccountingNumeric", "kPrimaryNumeric", "kOtherNumeric",
}

CODE_POINT = re.compile(r"U\+([0-9A-F]{4,6})")

SCHEMA = """
DROP TABLE IF EXISTS headwords;
DROP TABLE IF EXISTS chars;
DROP TABLE IF EXISTS fields;
DROP TABLE IF EXISTS senses;
DROP TABLE IF EXISTS classifiers;
DROP TABLE IF EXISTS meta;
CREATE TABLE headwords (word TEXT PRIMARY KEY);
CREATE TABLE chars (
    word TEXT PRIMARY KEY,
    mandarin TEXT,          -- kMandarin: the usual reading first, in pinyin with tone marks
    cantonese TEXT,         -- kCantonese: Jyutping
    definition TEXT,        -- kDefinition: short English gloss
    total_strokes TEXT,     -- kTotalStrokes
    radical_stroke TEXT,    -- kRSUnicode: radical number '.' residual strokes
    simplified TEXT,        -- kSimplifiedVariant as characters
    traditional TEXT        -- kTraditionalVariant as characters
);
CREATE TABLE fields (word TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL);
CREATE TABLE senses (word TEXT, position INTEGER, text TEXT);
CREATE TABLE classifiers (word TEXT, traditional TEXT, simplified TEXT, codes TEXT, note TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def char_of(code: str) -> str:
    """'U+4E00' -> '一'."""
    return chr(int(code[2:], 16))


def as_chars(value: str) -> str:
    """Turn a variant value such as 'U+4E7E<kMatthews U+5E72' into its characters ('乾干').

    The source tags after '<' are dropped here; the untouched value stays in `fields`.
    """
    return "".join(char_of("U+" + hexcode) for hexcode in CODE_POINT.findall(value))


def read_rows(zip_path: Path):
    """Yield (character, field, value) for the kept properties, and return header info."""
    info: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as archive:
        for name in sorted(archive.namelist()):
            for line in archive.read(name).decode("utf-8").splitlines():
                if line.startswith("#"):
                    # Header lines carry the release date and Unicode version.
                    if line.startswith("# Date:") and "date" not in info:
                        info["date"] = line.split(":", 1)[1].strip().split()[0]
                    elif line.startswith("# Unicode Version") and "version" not in info:
                        info["version"] = line.rsplit(" ", 1)[-1]
                    continue
                parts = line.split("\t")
                if len(parts) != 3 or parts[1] not in KEEP:
                    continue
                yield char_of(parts[0]), parts[1], parts[2]
    read_rows.info = info


def split_definition(text: str) -> list[str]:
    """kDefinition separates major senses with ';'. A ';' can also occur inside a sense,
    so this is an approximation, but it matches how most entries are written."""
    return [part.strip() for part in text.split(";") if part.strip()]


def build(zip_path: Path, db_path: Path) -> dict[str, int]:
    per_char: dict[str, dict[str, str]] = {}
    rows: list[tuple[str, str, str]] = []
    for char, field, value in read_rows(zip_path):
        rows.append((char, field, value))
        per_char.setdefault(char, {})[field] = value
    info = getattr(read_rows, "info", {})

    # Build beside the target and swap it in, so a running server never sees half a file.
    tmp = db_path.with_suffix(".sqlite.tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        # Every character Unihan covers is a headword, including the ~58k rare ones that only
        # have a radical and stroke count, so the list of Chinese characters is complete.
        shown = [(c,) for c in per_char]
        con.executemany("INSERT INTO headwords (word) VALUES (?)", shown)
        con.executemany(
            "INSERT INTO chars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(c, f.get("kMandarin"), f.get("kCantonese"), f.get("kDefinition"),
              f.get("kTotalStrokes"), f.get("kRSUnicode"),
              as_chars(f["kSimplifiedVariant"]) if "kSimplifiedVariant" in f else None,
              as_chars(f["kTraditionalVariant"]) if "kTraditionalVariant" in f else None)
             for c, f in per_char.items()])
        con.executemany("INSERT INTO fields VALUES (?, ?, ?)", rows)
        senses = [(c, i, text) for c, f in per_char.items() if "kDefinition" in f
                  for i, text in enumerate(split_definition(f["kDefinition"]), 1)]
        con.executemany("INSERT INTO senses VALUES (?, ?, ?)", senses)
        con.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("updated", info.get("date", "")),
            ("unicode_version", info.get("version", "")),
            ("license", "Unicode License V3, https://www.unicode.org/license.txt"),
        ])
        con.execute("CREATE INDEX fields_word ON fields (word)")
        con.execute("CREATE INDEX senses_word ON senses (word)")
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    tmp.replace(db_path)
    return {"characters": len(per_char), "headwords": len(shown), "fields": len(rows),
            "senses": len(senses)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=DEFAULT_ZIP)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    if not args.input.exists():
        raise SystemExit(f"{args.input} not found: run scripts/download_unihan.py first")
    counts = build(args.input, args.db)
    print(f"{args.db}: {counts['headwords']} headwords, {counts['fields']} fields, "
          f"{counts['senses']} senses")
    return 0


if __name__ == "__main__":
    sys.exit(main())
