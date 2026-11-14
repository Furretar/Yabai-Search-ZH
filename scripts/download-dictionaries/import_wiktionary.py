#!/usr/bin/env python3
"""Build data/dictionary-databases/wiktionary.sqlite from kaikki.org's (Wiktextract) Chinese JSONL.

The source is en.wiktionary's Chinese entries, parsed into machine-readable JSON
by Wiktextract and published at kaikki.org. It is updated regularly and shares
Wiktionary's licence (CC BY-SA 4.0 and GFDL).

The database stores the headwords (for the source chips), their English glosses,
their Mandarin Pinyin readings, any non-Mandarin variety readings (Chengdu
Sichuanese, Wu/Shanghainese, Xiang, Gan, Cantonese, Hakka, Min, ...) and any
classifiers mentioned in the glosses. Each line of the JSONL is one
word/part-of-speech entry; we keep its CJK `word`.

Usage (from the project root):
    uv run python scripts/download-dictionaries/import_wiktionary.py
    uv run python scripts/download-dictionaries/import_wiktionary.py --input data/kaikki_zh.jsonl.gz
    uv run python scripts/download-dictionaries/import_wiktionary.py --force
"""

from __future__ import annotations

import _wikipedia_titles
import argparse
import gzip
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path


from opencc import OpenCC

# Run as a file (python scripts/download-dictionaries/NAME.py), so the project root is not
# on the import path by default.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from yabai_search.pinyin import split_pinyin, to_numeric, to_toneless  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = "https://kaikki.org/dictionary/Chinese/kaikki.org-dictionary-Chinese.jsonl.gz"
DEFAULT_CACHE = ROOT / "data" / "dl-base-dictionaries" / "kaikki_zh.jsonl.gz"
DEFAULT_DB = ROOT / "data" / "dictionary-databases" / "wiktionary.sqlite"
# The app splits run-together readings with the syllables CEDICT uses, so the
# importer precomputes the same spacing from the same source.
CEDICT_DB = ROOT / "data" / "dictionary-databases" / "cedict.sqlite"

# One row per headword, plus one row per (word, gloss). A word with no gloss is
# still recorded once (with a NULL gloss) so the source chips can find it.
SCHEMA = """
DROP TABLE IF EXISTS wiktionary;

CREATE TABLE wiktionary (
    id    INTEGER PRIMARY KEY,  -- auto row id
    word  TEXT NOT NULL,        -- a headword listed by Wiktionary
    gloss TEXT                  -- one English gloss (NULL when the word has none)
);

CREATE INDEX idx_wiktionary_word ON wiktionary(word);

-- Trigram full-text index over headwords and glosses. FTS5's trigram tokenizer
-- makes LIKE '%…%' use the index for queries of three or more characters, so
-- quoted definition searches do not scan the whole table. External content
-- keeps just the index, not a second copy of the text.
DROP TABLE IF EXISTS wiktionary_fts;
CREATE VIRTUAL TABLE wiktionary_fts USING fts5(
    word, gloss, content='wiktionary', content_rowid='id', tokenize='trigram'
);

-- Standardized classifiers, extracted from the "(Classifier: ...)" notes in the
-- glosses (same table shape as every other source database).
DROP TABLE IF EXISTS classifiers;

CREATE TABLE classifiers (
    word        TEXT NOT NULL,
    traditional TEXT,
    simplified  TEXT,
    codes       TEXT,
    note        TEXT
);
CREATE INDEX idx_classifiers_word ON classifiers(word);

-- Readings, one row per reading. kaikki publishes them in each entry's
-- "sounds" array, separately from the headword and glosses. Dialect readings
-- are stored only when the entry has no Mandarin Pinyin.
DROP TABLE IF EXISTS readings;

CREATE TABLE readings (
    id     INTEGER PRIMARY KEY,  -- auto row id (insertion order = source order)
    word   TEXT NOT NULL,        -- a headword listed by Wiktionary
    pinyin TEXT NOT NULL,        -- a reading (Mandarin Pinyin or a variety one)
    kind   TEXT NOT NULL DEFAULT 'mandarin',  -- "mandarin", "wu", "xiang", ...
    label  TEXT NOT NULL DEFAULT '',           -- e.g. "Wu", "Shanghainese", "Xiang"
    pinyin_toneless TEXT NOT NULL DEFAULT '',  -- pinyin without tones/spaces (search)
    -- Precomputed at import so a search never re-splits every reading:
    pinyin_spaced  TEXT NOT NULL DEFAULT '',   -- "líng cì yuán"
    pinyin_numeric TEXT NOT NULL DEFAULT '',   -- "ling2 ci4 yuan2"
    simplified     TEXT NOT NULL DEFAULT '',   -- word in simplified characters
    is_cedict      INTEGER NOT NULL DEFAULT 0  -- already covered by CEDICT
);
CREATE INDEX idx_readings_word ON readings(word);
CREATE INDEX idx_readings_toneless ON readings(pinyin_toneless);
CREATE INDEX idx_readings_simplified ON readings(simplified);

-- The subset a source-pinyin search can return: Mandarin readings that CEDICT
-- does not already cover. Keeping it separate makes the hot query table small.
DROP TABLE IF EXISTS readings_extra;

CREATE TABLE readings_extra (
    id     INTEGER PRIMARY KEY,
    word   TEXT NOT NULL,
    pinyin TEXT NOT NULL,
    pinyin_toneless TEXT NOT NULL,
    pinyin_spaced   TEXT NOT NULL,
    pinyin_numeric  TEXT NOT NULL,
    simplified      TEXT NOT NULL
);
CREATE INDEX idx_readings_extra_toneless ON readings_extra(pinyin_toneless);

-- Metadata (the About page shows when the data was last updated).
DROP TABLE IF EXISTS meta;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def has_cjk(text: str) -> bool:
    """True when `text` contains a CJK ideograph.

    kaikki's Chinese set also holds latin headwords (e.g. "book", "GDP"); those
    can't match a Chinese result, so we skip them.
    """
    return any("\u3400" <= char <= "\u9fff" for char in text)


def cedict_syllables(path: Path) -> set[str]:
    """Every toneless pinyin syllable CEDICT uses.

    The app splits run-together readings with this exact set, so the precomputed
    spacing in the readings table matches what a query-time split would produce.
    """
    if not path.exists():
        print(f"warning: {path} not found; readings split per character")
        return set()
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT pinyin FROM entries").fetchall()
    finally:
        connection.close()
    return {
        to_toneless(token)
        for (pinyin,) in rows
        for token in (pinyin or "").split()
    } - {""}


def cedict_heads(path: Path) -> set[str]:
    """Every traditional/simplified headword in CEDICT."""
    if not path.exists():
        return set()
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT traditional FROM entries UNION SELECT simplified FROM entries"
        ).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


# kaikki's "Chinese" set mixes every Chinese variety. We only want the
# Mandarin/standard data, so an entry that belongs *solely* to one of these four
# varieties (and nothing we keep) is dropped.
_EXCLUDED_VARIETIES = (
    "cantonese", "hakka", "puxian", "pu-xian", "xinghua", "henghua",
    "min-nan", "minnan", "southern-min", "hokkien", "taiwanese", "teochew",
    "chiuchow", "amoy", "xiamen", "quanzhou", "zhangzhou", "hoklo",
)
# Varieties we keep. Any of these on an entry overrides the exclusion above.
_INCLUDED_VARIETIES = (
    "mandarin", "putonghua", "guoyu", "standard-chinese", "sichuanese",
    "shanghainese", "hainanese", "fuzhounese", "waxiang", "pinghua",
    "dungan", "jianghuai", "min-dong", "eastern-min", "min-bei",
    "northern-min", "min-zhong", "central-min",
)
# Short variety names, matched only as whole hyphen-delimited tokens so "wu"
# doesn't match inside a longer word.
_SHORT_INCLUDED = re.compile(r"(?:^|-)(?:wu|gan|xiang|jin|hui)(?:-|$)")


def _entry_tags(obj: dict) -> set[str]:
    """Every dialect/register tag on an entry's sounds and senses, normalized."""
    found: set[str] = set()
    for sound in obj.get("sounds") or []:
        found.update(sound.get("tags") or [])
        found.update(sound.get("raw_tags") or [])
    for sense in obj.get("senses") or []:
        found.update(sense.get("tags") or [])
        found.update(sense.get("raw_tags") or [])
    return {tag.lower().replace(" ", "-") for tag in found}


def is_excluded_variety(obj: dict) -> bool:
    """True when an entry belongs only to an excluded variety.

    Excluded: Cantonese, Hakka, Puxian Min and Southern Min. An entry with any
    kept variety on it (Mandarin, Wu, ...), or with no variety tag at all, is
    not excluded.
    """
    tags = _entry_tags(obj)
    if not any(bad in tag for tag in tags for bad in _EXCLUDED_VARIETIES):
        return False
    if any(good in tag for tag in tags for good in _INCLUDED_VARIETIES):
        return False
    return not any(_SHORT_INCLUDED.search(tag) for tag in tags)


# A romanization suffix in a zh_pron, e.g. "shǐ (shi³)".
_PRON_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")

# Punctuation that splits a phrase's reading into one segment per clause.
_PHRASE_PUNCT_RE = re.compile(r"[,，;；:：、]")


def mandarin_pinyin(obj: dict) -> list[str]:
    """The standard Mandarin Pinyin reading(s) of an entry ([] when none).

    kaikki lists readings in `sounds`, each a `zh_pron` with tags. Keep only the
    Standard/Standard-Chinese ones (or a plain Mandarin fallback), skip dialect
    variants and the "phonetic" tone-sandhi respelling, and drop a romanization
    suffix like "shǐ (shi³)". A phrase split at punctuation has one reading per
    segment; those are joined back into a single reading.
    """
    standard: list[str] = []
    fallback: list[str] = []
    for sound in obj.get("sounds") or []:
        tags = {tag.lower() for tag in sound.get("tags") or []}
        if "pinyin" not in tags or "mandarin" not in tags or "phonetic" in tags:
            continue
        pron = (sound.get("zh_pron") or "").strip()
        if not pron:
            continue
        pron = _PRON_SUFFIX_RE.sub("", pron)
        # Some sounds append a phonetic respelling: "sìbǎi-mǐ [Phonetic: …]".
        pron = re.sub(r"\s*\[Phonetic:[^\]]*\]?", "", pron).strip()
        if "standard" in tags or "standard-chinese" in tags:
            standard.append(pron)
        elif tags <= {"mandarin", "pinyin"}:
            fallback.append(pron)
    standard_unique = list(dict.fromkeys(standard))
    fallback_unique = list(dict.fromkeys(fallback))
    # Prefer a reading that spells out digits ("liùjiǔshì" over "69-shì").
    unique = standard_unique or fallback_unique
    for group in (standard_unique, fallback_unique):
        spelled = [pron for pron in group if not re.search(r"\d", pron)]
        if spelled:
            unique = spelled
            break
    word = obj.get("word") or ""
    if len(unique) > 1 and _PHRASE_PUNCT_RE.search(word):
        return [" ".join(unique)]
    return unique


def simplified_forms(obj: dict) -> list[str]:
    """The simplified forms listed under an entry's `forms` ([] when none)."""
    forms: list[str] = []
    for form in obj.get("forms") or []:
        text = form.get("form")
        tags = " ".join(
            (form.get("raw_tags") or []) + (form.get("tags") or [])
        ).lower()
        if text and "simp" in tags:
            forms.append(text)
    return forms


# Variety tags, most specific first: (sound tag, kind, display label). A sound
# tagged with both a general and a specific name (Wu + Shanghai, Mandarin +
# Chengdu) takes the label from the first match.
_VARIETY_TAGS = (
    ("chengdu", "sichuanese", "Chengdu Sichuanese"),
    ("sichuanese", "sichuanese", "Sichuanese"),
    ("shanghai", "wu", "Shanghainese"),
    ("shanghainese", "wu", "Shanghainese"),
    ("wu", "wu", "Wu"),
    ("xiang", "xiang", "Xiang"),
    ("pingxiang", "gan", "Pingxiang Gan"),
    ("gan", "gan", "Gan"),
    ("jin", "jin", "Jin"),
    ("hui", "hui", "Hui"),
    ("pinghua", "pinghua", "Pinghua"),
    ("waxiang", "waxiang", "Waxiang"),
    ("dungan", "dungan", "Dungan"),
    ("jianghuai", "jianghuai", "Jianghuai Mandarin"),
    ("cantonese", "cantonese", "Cantonese"),
    ("hakka", "hakka", "Hakka"),
    ("teochew", "min-nan", "Teochew"),
    ("chiuchow", "min-nan", "Teochew"),
    ("taiwanese", "min-nan", "Taiwanese"),
    ("hokkien", "min-nan", "Hokkien"),
    ("min-nan", "min-nan", "Southern Min"),
    ("minnan", "min-nan", "Southern Min"),
    ("southern-min", "min-nan", "Southern Min"),
    ("amoy", "min-nan", "Amoy"),
    ("xiamen", "min-nan", "Xiamen"),
    ("quanzhou", "min-nan", "Quanzhou"),
    ("zhangzhou", "min-nan", "Zhangzhou"),
    ("hainanese", "hainanese", "Hainanese"),
    ("fuzhounese", "min-dong", "Fuzhounese"),
    ("min-dong", "min-dong", "Eastern Min"),
    ("eastern-min", "min-dong", "Eastern Min"),
    ("min-bei", "min-bei", "Northern Min"),
    ("northern-min", "min-bei", "Northern Min"),
    ("min-zhong", "min-zhong", "Central Min"),
    ("central-min", "min-zhong", "Central Min"),
)


def dialect_readings(obj: dict) -> list[tuple[str, str, str]]:
    """Non-Mandarin readings as (kind, label, pinyin) rows.

    Used only when the word has no Mandarin reading. Covers every variety the
    entry lists: Chengdu Sichuanese, Wu/Shanghainese, Xiang, Gan, Cantonese,
    Hakka, Min and so on. A dialect reading is shown as-is and is not
    tone-coloured: the app's tone colours are Hanyu Pinyin tones.
    """
    candidates: dict[str, list[tuple[str, str, bool]]] = {}
    for sound in obj.get("sounds") or []:
        tags = {tag.lower().replace(" ", "-") for tag in sound.get("tags") or []}
        if "sinological-ipa" in tags or "ipa" in tags:
            continue
        pron = (sound.get("zh_pron") or "").strip()
        if not pron:
            continue
        variety = next(
            ((kind, label) for tag, kind, label in _VARIETY_TAGS if tag in tags),
            None,
        )
        if variety is None:
            continue
        kind, label = variety
        pron = re.sub(r"\s*\[Phonetic:[^\]]*\]?", "", pron).strip()
        if not pron:
            continue
        specific = "wiktionary-specific" in tags
        candidates.setdefault(kind, []).append((label, pron, specific))

    result: list[tuple[str, str, str]] = []
    for kind, rows in candidates.items():
        # Prefer a non-Wiktionary-specific scheme, then one with tone digits.
        rows.sort(
            key=lambda row: (
                row[2],
                0 if any(char in row[1] for char in "⁰¹²³⁴⁵⁶⁷⁸⁹") else 1,
            )
        )
        label, pron, _ = rows[0]
        result.append((kind, label, pron))
    return result


# Wiktionary writes classifier notes like "(Classifier: 匹 m c; 隻／只 m c h mn)".
_CLASSIFIER_RE = re.compile(r"\s*\(Classifier:\s*([^)]+)\)")


def extract_classifiers(gloss: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Split a "(Classifier: ...)" note out of a gloss.

    Returns (clean_gloss, [(traditional, simplified, codes), ...]). A token like
    "隻／只 m c h mn" becomes ("隻", "只", "m c h mn"); "匹 m c" becomes
    ("匹", "匹", "m c").
    """
    found: list[tuple[str, str, str]] = []

    def collect(match: re.Match) -> str:
        for token in match.group(1).split(";"):
            parts = token.strip().split()
            if not parts:
                continue
            form = parts[0]
            codes = " ".join(parts[1:])
            if "／" in form:
                trad, simp = form.split("／", 1)
            elif "/" in form:
                trad, simp = form.split("/", 1)
            else:
                trad = simp = form
            found.append((trad, simp, codes))
        return ""

    return _CLASSIFIER_RE.sub(collect, gloss).strip(), found


def download(url: str, dest: Path) -> None:
    """Stream `url` to `dest` so we cache the file instead of re-fetching it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "yabai-search/0.1"})
    with urllib.request.urlopen(request) as response, dest.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    print(f"  saved {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


def build(source: Path, db_path: Path) -> None:
    """(Re)build the SQLite database from the gzipped JSONL `source`."""
    if not CEDICT_DB.exists():
        raise SystemExit(
            f"error: {CEDICT_DB} is required: the readings table precomputes "
            "CEDICT syllables and coverage. Run scripts/download-dictionaries/import_cedict.py first."
        )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Full wipe: delete the old DB file so every run starts from scratch.
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA)
    # Speed up a bulk load: disable crash-safe journaling while importing.
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    # kaikki's file has no date in it, so record when this snapshot was built.
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('updated', ?)",
        (time.strftime("%Y-%m-%d"),),
    )
    # Record which CEDICT build supplied the syllable set / coverage flags.
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('cedict_mtime', ?)",
        (str(CEDICT_DB.stat().st_mtime),),
    )

    insert = "INSERT INTO wiktionary (word, gloss) VALUES (?, ?)"
    insert_classifier = (
        "INSERT INTO classifiers (word, traditional, simplified, codes, note) "
        "VALUES (?,?,?,?,?)"
    )
    insert_reading = (
        "INSERT INTO readings (word, pinyin, kind, label, pinyin_toneless, "
        "pinyin_spaced, pinyin_numeric, simplified, is_cedict) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    seen_word: set[str] = set()  # headwords recorded (once each, even with no gloss)
    seen_gloss: set[tuple[str, str]] = set()  # (word, gloss) pairs, de-duplicated
    seen_cls: set[tuple[str, str, str, str]] = set()  # (word, trad, simp, codes)
    seen_reading: set[tuple[str, str, str]] = set()  # (word, pinyin, kind)
    rows: list[tuple[str, str | None]] = []
    cls_rows: list[tuple[str, str, str, str, str]] = []
    reading_rows: list[tuple[str, str, str, str, str, str, str, str, int]] = []
    kept = 0
    dropped = 0  # entries skipped because they are an excluded variety
    started = time.time()
    syllables = cedict_syllables(CEDICT_DB)
    heads = cedict_heads(CEDICT_DB)
    to_simplified = OpenCC("t2s")

    def flush() -> None:
        """Write the buffered rows and clear the buffers."""
        connection.executemany(insert, rows)
        connection.executemany(insert_classifier, cls_rows)
        connection.executemany(insert_reading, reading_rows)
        rows.clear()
        cls_rows.clear()
        reading_rows.clear()

    with gzip.open(source, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)  # one word entry per line
            except json.JSONDecodeError:
                continue  # skip any malformed line rather than abort
            # Drop entries that are only Cantonese / Hakka / Puxian / Southern Min.
            if is_excluded_variety(obj):
                dropped += 1
                continue
            word = obj.get("word")
            if not word or not has_cjk(word):
                continue
            # Record the headword once, even if it has no gloss (the chips only
            # need to know the word is present).
            if word not in seen_word:
                seen_word.add(word)
                rows.append((word, None))
                kept += 1
            # Mandarin readings, plus a non-Mandarin variety reading only when
            # the entry has no Mandarin Pinyin (the app shows it as a fallback).
            mandarin = mandarin_pinyin(obj)
            readings = [(p, "mandarin", "") for p in mandarin]
            if not mandarin:
                readings += [
                    (pinyin, kind, label) for kind, label, pinyin in dialect_readings(obj)
                ]
            if readings:
                for spelling in [word] + simplified_forms(obj):
                    for pinyin, kind, label in readings:
                        rkey = (spelling, pinyin, kind)
                        if rkey not in seen_reading:
                            seen_reading.add(rkey)
                            # Only Mandarin readings are searched by the pinyin
                            # query, so only those need the split/numeric forms.
                            if kind == "mandarin":
                                spaced = split_pinyin(pinyin, syllables)
                                numeric = to_numeric(spaced)
                            else:
                                spaced = ""
                                numeric = ""
                            simplified = to_simplified.convert(spelling)
                            reading_rows.append(
                                (
                                    spelling,
                                    pinyin,
                                    kind,
                                    label,
                                    to_toneless(pinyin),
                                    spaced,
                                    numeric,
                                    simplified,
                                    1 if simplified in heads else 0,
                                )
                            )
            # ...then one row per distinct gloss on any of its senses, pulling any
            # "(Classifier: ...)" note out into the standardized classifiers table.
            # Prefer `raw_glosses`, which keep register/dialect labels such as
            # "(Wu)" that the cleaned `glosses` list strips.
            for sense in obj.get("senses") or []:
                for gloss in sense.get("raw_glosses") or sense.get("glosses") or []:
                    clean, classifiers = extract_classifiers(gloss)
                    for trad, simp, codes in classifiers:
                        ckey = (word, trad, simp, codes)
                        if ckey not in seen_cls:
                            seen_cls.add(ckey)
                            cls_rows.append((word, trad, simp, codes, ""))
                    key = (word, clean)
                    if clean and key not in seen_gloss:
                        seen_gloss.add(key)
                        rows.append((word, clean))
            # Flush every 5000 rows: fewer, larger transactions is faster.
            if len(rows) + len(cls_rows) + len(reading_rows) >= 5000:
                flush()
    if rows or cls_rows or reading_rows:
        flush()  # flush the last partial batch

    # Materialize the hot subset: Mandarin readings CEDICT does not cover.
    connection.execute(
        "INSERT INTO readings_extra ("
        "id, word, pinyin, pinyin_toneless, pinyin_spaced, pinyin_numeric, simplified"
        ") SELECT id, word, pinyin, pinyin_toneless, pinyin_spaced, "
        "pinyin_numeric, simplified FROM ("
        "  SELECT id, word, pinyin, pinyin_toneless, pinyin_spaced, "
        "         pinyin_numeric, simplified, ROW_NUMBER() OVER ("
        "    PARTITION BY simplified, pinyin_spaced ORDER BY word, id"
        "  ) AS rn FROM readings "
        "  WHERE kind = 'mandarin' AND is_cedict = 0"
        ") WHERE rn = 1 ORDER BY id"
    )
    # Chinese Wikipedia titles that have an English title (_wikipedia_titles.py), once, in their own table; the view
    # wiktionary_all adds them as glosses "English title (Wikipedia)" for the app (db.py). `wiktionary` itself stays
    # Wiktionary-only, so word lists, the segmenter and the full-text index never see them.
    connection.executescript(
        "DROP VIEW IF EXISTS wiktionary_all; DROP TABLE IF EXISTS wikipedia;"
        "CREATE TABLE wikipedia (title TEXT PRIMARY KEY COLLATE NOCASE, en TEXT NOT NULL) WITHOUT ROWID;"
    )
    connection.executemany("INSERT OR IGNORE INTO wikipedia(title, en) VALUES (?, ?)", _wikipedia_titles.rows("en"))
    connection.execute(
        "CREATE VIEW wiktionary_all AS SELECT word, gloss FROM wiktionary "
        "UNION ALL SELECT title, en || ' (Wikipedia)' FROM wikipedia"
    )
    print(f"  Wikipedia: {connection.execute('SELECT COUNT(*) FROM wikipedia').fetchone()[0]:,} titles with an English title added")
    connection.execute("INSERT INTO wiktionary_fts (wiktionary_fts) VALUES ('rebuild')")
    connection.commit()
    connection.execute("VACUUM")  # repack the file compactly
    words = connection.execute("SELECT count(DISTINCT word) FROM wiktionary").fetchone()[0]
    total = connection.execute("SELECT count(*) FROM wiktionary").fetchone()[0]
    readings = connection.execute("SELECT count(*) FROM readings").fetchone()[0]
    connection.close()

    print(
        f"done: {words} headwords, {total} rows, {readings} readings "
        f"in {time.time() - started:.1f}s ({dropped} dialect entries skipped)"
    )
    print(f"database: {db_path} ({db_path.stat().st_size / 1e6:.1f} MB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--input", type=Path, help="use a local .jsonl.gz file")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--force", action="store_true", help="re-download")
    args = parser.parse_args()

    if args.input:
        source = args.input  # the user pointed us at a local file
    else:
        source = DEFAULT_CACHE
        # Download only if there's no cached copy, or the user forced a refresh.
        if args.force or not source.exists():
            download(args.url, source)

    if args.force:
        _wikipedia_titles.build(force=True)  # read newer Wikipedia dumps too
    build(source, args.db)
    return 0


if __name__ == "__main__":
    import _indexes  # rebuilds the derived indexes when the import is done (--no-indexes skips it)

    sys.exit(_indexes.run(main))
