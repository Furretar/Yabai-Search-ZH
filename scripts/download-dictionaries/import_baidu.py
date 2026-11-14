#!/usr/bin/env python3
"""Convert the Baidu base database into the clean one the website reads.

    python3 scripts/download-dictionaries/import_baidu.py

Reads  data/dl-base-dictionaries/baidu.sqlite   the base database: every Baidu answer exactly as scraped (table `raw`,
                                                zlib-compressed JSON) and every character page (`zici_raw`). It is built
                                                by ~/Desktop/ai/baidu-scraper/scripts/import_baidu.py, which gets the
                                                answers from the scrape; the scrape and this conversion are separate.
Writes data/dictionary-databases/baidu/baidu_clean.sqlite   plain-text tables, no empty results, no compressed blobs
       data/dictionary-databases/baidu/baidu_clean.csv      one row per word, flattened for checking by eye
and then rebuilds the derived indexes (scripts/regenerate_everything.py), like every importer.

The website lists every word in `words` (also the headwords with no definition, layout "headword") and reads `senses_text` (one display text per
sense with its reading, built here: `(part of speech)` in front of a character's definitions; the site files each text
under its reading's header). The other tables (readings, relations, citations, examples, texts, idiom_parts) are the rest of what
Baidu says, kept for later and for reading by eye.

Cleaning: HTML tags and entities removed, whitespace collapsed, empty strings dropped, repeats within a word
dropped; images, audio links, ids and tracking fields are not carried over. Answers whose own `name` is not the
word that was asked are left out (a stray answer for another word). Baidu's generated example sentences are not
kept: the top-level `liju` of idioms and `zaoJu`; the dictionary examples under basicDefinition[].liju are kept, in
their own table, as the project rule says. Baidu's "related" lists (relationInfo, "recommended for you") are not
kept either: they are random recommendations (for 一刹: 前进, 回去, 家乡), not synonyms.

What counts as empty: the answer has no `name` (Baidu has no entry), or after cleaning it holds no definition,
no text (encyclopedia, story, notes), no citation or example and no synonym, antonym, combination or variant.
A word with an encyclopedia text but no definition is kept, with has_definition = 0 (it is not a website headword), unless the
older VPN scrape defined it: that definition is added (layer `vpn`) only for a word whose own answer gave none.
Headwords without a Chinese character are not dictionary words and are left out. A pinyin value without a Latin
letter (Baidu once sent the word itself) is dropped.

Character pages (single Han characters, scraped from hanyu-page/zici): their readings, definitions (with part of
speech), citations, compounds and facts (radical, strokes, structure, wubi) are layers `zici_basic` / `zici_detail`
of `senses` and texts of kind radical, stroke_count, structure, traditional, wubi, wuxing, writing_skill. A character
that also has a word-page answer is one word with both.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sqlite3
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SRC = ROOT / "data" / "dl-base-dictionaries" / "baidu.sqlite"
DEFAULT_DB = ROOT / "data" / "dictionary-databases" / "baidu" / "baidu_clean.sqlite"
DEFAULT_CSV = ROOT / "data" / "dictionary-databases" / "baidu" / "baidu_clean.csv"

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_ODD = re.compile("[​‌‍⁠﻿­]")


def clean(value) -> str:
    return _SPACE.sub(" ", _ODD.sub("", html.unescape(_TAG.sub("", str(value if value is not None else ""))))).strip()


_LATIN = re.compile("[a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜüńňǹḿ]")
_CJK = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff]")


def pinyin_of(value) -> str:
    """A reading, or "" when it has no Latin letter (Baidu once sent the word itself as its pinyin)."""
    text = clean(first(value) if isinstance(value, list) else value)
    return text if _LATIN.search(text) else ""


def first(value) -> str:
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def names(value) -> list[str]:
    """Words from a list of {name: ...} or plain strings, cleaned, without repeats, in order."""
    out: list[str] = []
    for item in value or []:
        text = clean(item.get("name") if isinstance(item, dict) else item)
        if text and text not in out:
            out.append(text)
    return out


def texts_of(value) -> list[str]:
    """A field that may be a string, a list of strings or a list of {name|text: ...}."""
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    for item in value or []:
        if isinstance(item, dict):
            item = item.get("name") or item.get("text") or item.get("content") or ""
        text = clean(item)
        if text and text not in out:
            out.append(text)
    return out


def joined(values, sep=", ") -> str:
    out: list[str] = []
    for v in values or []:
        v = clean(v)
        if v and v not in out:
            out.append(v)
    return sep.join(out)


SCHEMA = """
CREATE TABLE words (
  word TEXT PRIMARY KEY, type TEXT, layout TEXT, pinyin TEXT, structure TEXT, connotation TEXT,
  word_formation TEXT, has_definition INTEGER, n_senses INTEGER, fetched TEXT);
CREATE TABLE readings (word TEXT, idx INTEGER, pinyin TEXT);
-- layer: basic = the short definition with part of speech and grammar roles; detail = the fuller one;
-- idiom = an idiom's definition; old = the older answer layout; vpn = a definition from the older VPN scrape, used only when the
-- word's own answer had none; zici_basic / zici_detail = the same two for a character page
CREATE TABLE senses (word TEXT, reading INTEGER, pinyin TEXT, layer TEXT, position INTEGER, definition TEXT,
  part_of_speech TEXT, grammar TEXT, connotation TEXT, usage TEXT);
-- kind: synonym, antonym, combination, modifier, quantifier, variant, related_idiom
CREATE TABLE relations (word TEXT, kind TEXT, target TEXT, reading INTEGER, sense INTEGER);
-- kind: quote (detail layer or idiom quotation), origin (idiom source), example (old layout quotation)
CREATE TABLE citations (word TEXT, kind TEXT, reading INTEGER, sense INTEGER, source TEXT, chapter TEXT,
  dynasty TEXT, author TEXT, quote TEXT);
CREATE TABLE examples (word TEXT, reading INTEGER, sense INTEGER, text TEXT);
-- kind: baike, baike_url, story, note, usage_tip, common_mistake, similar, ancient, modern, textbook, source, riddle;
-- character pages (layout zici): radical, stroke_count, structure, traditional, wubi, wuxing, writing_skill
CREATE TABLE texts (word TEXT, kind TEXT, text TEXT);
CREATE TABLE idiom_parts (word TEXT, part TEXT, definition TEXT);
-- What the website reads: one display text per sense (see senses_display below).
CREATE TABLE senses_text (word TEXT, position INTEGER, pinyin TEXT, text TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
INDEXES = """
CREATE INDEX i_readings ON readings(word); CREATE INDEX i_senses ON senses(word);
CREATE INDEX i_relations_word ON relations(word); CREATE INDEX i_relations_kind ON relations(kind, target);
CREATE INDEX i_citations ON citations(word); CREATE INDEX i_examples ON examples(word);
CREATE INDEX i_texts ON texts(word); CREATE INDEX i_parts ON idiom_parts(word);
CREATE INDEX i_senses_text ON senses_text(word, position);
CREATE INDEX i_words_def ON words(has_definition, word);
"""
CSV_COLUMNS = ["word", "type", "layout", "pinyin", "part_of_speech", "grammar", "connotation", "structure",
               "definitions", "detailed_definitions", "synonyms", "antonyms", "combinations",
               "citations", "examples", "baike", "fetched"]


class Entry:
    """Everything cleaned out of one answer."""

    def __init__(self) -> None:
        self.readings: list[str] = []
        self.senses: list[tuple] = []        # reading, pinyin, layer, position, definition, pos, grammar, conn, usage
        self.relations: list[tuple] = []     # kind, target, reading, sense
        self.citations: list[tuple] = []     # kind, reading, sense, source, chapter, dynasty, author, quote
        self.examples: list[tuple] = []      # reading, sense, text
        self.texts: list[tuple] = []         # kind, text
        self.parts: list[tuple] = []         # part, definition

    def add_relations(self, kind: str, values, reading=None, sense=None) -> None:
        for target in names(values):
            row = (kind, target, reading, sense)
            if row not in self.relations:
                self.relations.append(row)

    def add_text(self, kind: str, value) -> None:
        for text in texts_of(value):
            if (kind, text) not in self.texts:
                self.texts.append((kind, text))

    def add_cite(self, kind, reading, sense, item) -> None:
        if not isinstance(item, dict):
            return
        row = (kind, reading, sense, clean(item.get("source")), clean(item.get("sourceChapter")),
               clean(item.get("dynasty")), clean(item.get("author")), clean(item.get("citeOriginalText")))
        if row[3] or row[7]:
            if row not in self.citations:
                self.citations.append(row)

    def has_definition(self) -> bool:
        return any(s[4] for s in self.senses)

    def has_content(self) -> bool:
        return bool(self.senses or self.texts or self.citations or self.examples
                    or any(r[0] in ("synonym", "antonym", "combination", "variant") for r in self.relations))


def parse_term(d: dict, e: Entry) -> None:
    for gi, group in enumerate(d.get("comprehensiveDefinition") or [], 1):
        if not isinstance(group, dict):
            continue
        pinyin = pinyin_of(group.get("pinyin"))
        e.readings.append(pinyin)
        seen: set[tuple] = set()
        for pi, item in enumerate(group.get("basicDefinition") or [], 1):
            if not isinstance(item, dict):
                continue
            text = clean(item.get("definition"))
            row = (gi, pinyin, "basic", pi, text, joined(item.get("cixing")), joined(item.get("grammarStruct")),
                   clean(item.get("baobian")), clean(item.get("shiyongchangjing")))
            if text and row not in seen:
                seen.add(row)
                e.senses.append(row)
            e.add_relations("synonym", item.get("synonyms"), gi, pi)
            e.add_relations("antonym", item.get("antonym"), gi, pi)
            e.add_relations("combination", item.get("zuci"), gi, pi)
            for ex in names(item.get("liju")):
                e.examples.append((gi, pi, ex))
        position = 0
        for item in group.get("detailDefinition") or []:
            if not isinstance(item, dict):
                continue
            text = clean(item.get("definition"))
            if not text:
                continue
            position += 1
            row = (gi, pinyin, "detail", position, text, joined(item.get("cixing")), "", clean(item.get("baobian")), "")
            if row not in seen:
                seen.add(row)
                e.senses.append(row)
            for cite in item.get("citeList") or []:
                e.add_cite("quote", gi, position, cite)
    e.add_relations("modifier", d.get("modifier"))
    e.add_relations("quantifier", d.get("quantifier"))
    e.add_relations("related_idiom", d.get("relationIdiom"))
    baike = d.get("baikeInfo") or {}
    if isinstance(baike, dict):
        e.add_text("baike", baike.get("baikeMean") or baike.get("baike_mean"))
        e.add_text("baike_url", baike.get("baikeUrl") or baike.get("baike_url"))
    lesson = d.get("lessonInfo")
    if isinstance(lesson, dict) and lesson:
        e.add_text("textbook", " ".join(clean(lesson.get(k)) for k in ("name", "edition", "grade", "volumes")).strip())


def parse_idiom(d: dict, e: Entry) -> None:
    e.readings.append(pinyin_of(d.get("pinyin")))
    info = d.get("definitionInfo") or {}
    if isinstance(info, dict):
        text = clean(info.get("definition"))
        if text:
            e.senses.append((1, e.readings[0], "idiom", 1, text, "", "", clean(info.get("baobian")), ""))
        for part in info.get("detailMeans") or []:
            if isinstance(part, dict) and clean(part.get("word")) and clean(part.get("definition")):
                row = (clean(part["word"]), clean(part["definition"]))
                if row not in e.parts:
                    e.parts.append(row)
        e.add_text("similar", info.get("similarDefinition"))
        e.add_text("ancient", info.get("ancientDefinition"))
        e.add_text("modern", info.get("modernDefinition"))
        e.add_text("usage_tip", info.get("usageTips"))
        e.add_text("common_mistake", info.get("yicuodian"))
    e.add_relations("synonym", d.get("synonyms"))
    e.add_relations("antonym", d.get("antonym"))
    e.add_relations("variant", d.get("tongyiyixing"))
    for item in d.get("chuChu") or []:
        e.add_cite("origin", None, None, item)
    for item in d.get("yinZheng") or []:
        e.add_cite("quote", None, None, item)
    e.add_text("story", d.get("story"))
    lesson = d.get("lessonInfo")
    if isinstance(lesson, dict) and lesson:
        e.add_text("textbook", " ".join(clean(lesson.get(k)) for k in ("name", "edition", "grade", "volumes")).strip())


def parse_old(d: dict, e: Entry) -> None:
    for gi, group in enumerate(d.get("definition") or [], 1):
        if not isinstance(group, dict):
            continue
        pinyin = pinyin_of(group.get("pinyin"))
        e.readings.append(pinyin)
        for pi, text in enumerate(texts_of(group.get("definition")), 1):
            e.senses.append((gi, pinyin, "old", pi, text, "", "", "", ""))
        position = 0
        for block in group.get("detailDefinition") or []:
            for sense in (block.get("list") or []) if isinstance(block, dict) else []:
                if not isinstance(sense, dict):
                    continue
                position += 1
                for text in texts_of(sense.get("definition")):
                    e.senses.append((gi, pinyin, "detail", position, text, "", "", "", ""))
                for quote in texts_of(sense.get("examples")):
                    e.citations.append(("example", gi, position, "", "", "", "", quote))
    e.add_relations("synonym", d.get("synonym"))
    e.add_relations("antonym", d.get("antonym"))
    e.add_text("source", d.get("source"))
    e.add_text("story", d.get("story"))
    e.add_text("riddle", d.get("jieLong"))
    baike = d.get("baikeInfo") or {}
    if isinstance(baike, dict):
        e.add_text("baike", baike.get("baikeMean") or baike.get("baike_mean"))
        e.add_text("baike_url", baike.get("baikeUrl") or baike.get("baike_url"))


def parse_zici(d: dict, e: Entry) -> None:
    """A character page (zici): readings with their definitions, citations and compounds, and the character's
    own facts (radical, strokes, structure, wubi). `d` is the answer's data.detail. Readings and senses are
    numbered after any the word page already gave."""
    base = len(e.readings)
    for gi, group in enumerate(d.get("comprehensiveDefinition") or [], base + 1):
        if not isinstance(group, dict):
            continue
        pinyin = pinyin_of(group.get("pinyin"))
        e.readings.append(pinyin)
        seen: set[tuple] = set()
        for pi, item in enumerate(group.get("basicDefinition") or [], 1):
            if not isinstance(item, dict):
                continue
            text = clean(item.get("definition"))
            row = (gi, pinyin, "zici_basic", pi, text, joined(item.get("cixing")), joined(item.get("grammarStruct")),
                   clean(item.get("baobian")), clean(item.get("shiyongchangjing")))
            if text and row not in seen:
                seen.add(row)
                e.senses.append(row)
            e.add_relations("synonym", item.get("synonyms"), gi, pi)
            e.add_relations("antonym", item.get("antonym"), gi, pi)
            e.add_relations("combination", item.get("zuci"), gi, pi)
            for src in item.get("source") or []:
                e.add_text("source", src)
        position = 0
        for item in group.get("detailDefinition") or []:
            if not isinstance(item, dict):
                continue
            text = clean(item.get("definition"))
            if not text:
                continue
            position += 1
            row = (gi, pinyin, "zici_detail", position, text, joined(item.get("cixing")), "", clean(item.get("baobian")), "")
            if row not in seen:
                seen.add(row)
                e.senses.append(row)
            for cite in item.get("citeList") or []:
                e.add_cite("quote", gi, position, cite)
    e.add_relations("combination", d.get("commonZuci"))
    for key, kind in (("radical", "radical"), ("wordStrokeCount", "stroke_count"), ("structType", "structure"),
                      ("traditional", "traditional"), ("wubi", "wubi"), ("lineType", "wuxing"),
                      ("writingSkill", "writing_skill")):
        e.add_text(kind, d.get(key))
    lesson = d.get("lessonInfo")
    if isinstance(lesson, dict) and lesson:
        e.add_text("textbook", " ".join(clean(lesson.get(k)) for k in ("name", "edition", "grade", "volumes")).strip())



def parse(word: str, kind: str, d: dict) -> Entry:
    e = Entry()
    (parse_idiom if kind == "idiom" else parse_old if kind == "old" else parse_term)(d, e)
    return e


def csv_row(word, kind, d, e: Entry, fetched) -> list[str]:
    basic = [s for s in e.senses if s[2] in ("basic", "idiom", "old", "zici_basic")]
    detail = [s for s in e.senses if s[2] in ("detail", "zici_detail")]
    many = len({s[1] for s in e.senses if s[1]}) > 1

    def line(s):
        text = (f"〔{s[1]}〕" if many and s[1] else "") + s[4]
        extra = "; ".join(x for x in (s[5], s[6]) if x)
        return text + (f" ({extra})" if extra else "")

    def rel(k):
        return " | ".join(dict.fromkeys(r[1] for r in e.relations if r[0] == k))

    baike = next((t[1] for t in e.texts if t[0] == "baike"), "")
    return [word, clean(d.get("type")), kind, " / ".join(dict.fromkeys(p for p in e.readings if p)),
            joined([s[5] for s in e.senses]), joined([g for s in e.senses for g in s[6].split(", ") if g]),
            joined([s[7] for s in e.senses]) or clean(d.get("baobian")), clean(d.get("structure")),
            " ‖ ".join(line(s) for s in basic), " ‖ ".join(line(s) for s in detail),
            rel("synonym"), rel("antonym"), rel("combination"),
            str(len(e.citations)), str(len(e.examples)), baike, fetched]



def export_csv_tables(db: sqlite3.Connection, out_dir: Path) -> None:
    """One CSV per table, every column, so the CSV files are a complete copy of the database."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.csv"):
        old.unlink()
    for table in ("words", "readings", "senses", "senses_text", "relations", "citations", "examples", "texts", "idiom_parts"):
        cursor = db.execute(f"SELECT * FROM {table}")
        with (out_dir / f"{table}.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow([c[0] for c in cursor.description])
            writer.writerows(cursor)



# A classical character definition may open with its part of speech and a full stop ("代词。这；此。", "副词。同“祗”。仅；仅仅。");
# the dictionary-style ones carry it as "(副词) ". The site shows both as one subheader, so both are written "(副词) text".
_LEADING_POS = re.compile(
    "^(名词|动词|形容词|副词|代词|数词|量词|数量词|拟声词|象声词|连词|助词|语气词|语气助词|介词|叹词|感叹词|疑问词|前缀|后缀)[。：]\\s*(.*)$",
    re.S,
)


def with_part_of_speech_prefix(text: str) -> str:
    """"代词。这；此。" -> "(代词) 这；此。" ; anything else unchanged."""
    match = _LEADING_POS.match(text)
    return f"({match.group(1)}) {match.group(2)}".rstrip() if match else text


def display_senses(e: Entry, kind: str) -> list[tuple[str, str]]:
    """The website's text for each sense of one word.

    Per reading: a word page's detailed definitions when it has them (the same text as the basic ones with the
    grammar and citations; the basic ones are often only a category label), else its basic ones; an idiom's or an
    old-layout answer's own definitions; for a character page the modern dictionary definitions, with their part of
    speech in front (the classical ones are kept in `senses` but not shown). Each text comes with the reading of its group (the site files it under that
    reading's header); nothing is added to the text itself.
    """
    readings: dict[int, tuple[str, dict[str, list[tuple]]]] = {}
    for s in e.senses:
        _g, pinyin, layer = s[0], s[1], s[2]
        readings.setdefault(s[0], (pinyin, {}))[1].setdefault(layer, []).append(s)
    groups: list[tuple[str, list[str]]] = []
    for gi in sorted(readings):
        pinyin, layers = readings[gi]
        if "zici_basic" in layers or "zici_detail" in layers:
            # Only the modern dictionary definitions are shown. The classical ones (layer zici_detail: historical
            # meanings with quoted sources) stay in `senses` and `citations` but are not shown for now.
            texts = []
            for s in layers.get("zici_basic", []):
                pos = s[5]
                texts.append(f"({pos.replace(', ', '、')}) {s[4]}" if pos else with_part_of_speech_prefix(s[4]))
        elif kind == "old":
            texts = [s[4] for s in layers.get("old", [])]
        elif kind == "idiom":
            texts = [s[4] for s in layers.get("idiom", [])]
        else:
            texts = [s[4] for s in (layers.get("detail") or layers.get("basic") or [])]
        groups.append((pinyin, texts))
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pinyin, texts in groups:
        for text in texts:
            text = with_part_of_speech_prefix(text) if text else text
            if text and (pinyin, text) not in seen:
                seen.add((pinyin, text))
                out.append((pinyin, text))
    return out


def add_old_definitions(src: sqlite3.Connection, out: sqlite3.Connection, out_csv: Path, have_text: set[str],
                        empties: list[tuple], stats: dict[str, int]) -> int:
    """Give a word the definition the older (VPN) scrape found, but only when its Baidu answer gave none.

    The base database's `senses` table still holds those older definitions (they were only kept for words the
    browser answers did not define). A word with a definition from its own answer is never touched. A word the browser
    scrape called empty ("no entry", "no content") but the older scrape defined becomes a word again.
    """
    if src.execute("SELECT 1 FROM sqlite_master WHERE name = 'senses'").fetchone() is None:
        return 0
    old: dict[str, list[str]] = {}
    # A character with a page of its own gets its definitions from that page, never from the older text: the base
    # database's `senses` hold that page's classical definitions too, which are not shown.
    paged = {w for (w,) in src.execute("SELECT word FROM zici_raw")} if src.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'zici_raw'").fetchone() else set()
    for word, _position, text in src.execute("SELECT word, position, text FROM senses ORDER BY word, position"):
        if word not in have_text and word not in paged and _CJK.search(word) and text and text.strip():
            old.setdefault(word, []).append(text.strip())
    if not old:
        return 0
    known = {w for (w,) in out.execute("SELECT word FROM words")}
    revived = {word for word, _reason, _layout, _checked in empties} & set(old)
    empties[:] = [row for row in empties if row[0] not in old]
    rows_csv = []
    for word, texts in old.items():
        if word in known:
            out.execute("UPDATE words SET has_definition = 1, n_senses = ? WHERE word = ?", (len(texts), word))
        else:
            out.execute("INSERT INTO words VALUES (?,?,?,?,?,?,?,?,?,?)", (word, "", "vpn", "", "", "", "", 1, len(texts), ""))
            rows_csv.append([word, "", "vpn", "", "", "", "", "", " ‖ ".join(texts), "", "", "", "", "0", "0", "", ""])
        out.executemany("INSERT INTO senses VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(word, 1, "", "vpn", i, text, "", "", "", "") for i, text in enumerate(texts, 1)])
        out.executemany("INSERT INTO senses_text VALUES (?,?,?,?)", [(word, i, "", text) for i, text in enumerate(texts, 1)])
    out.commit()
    if rows_csv:
        with out_csv.open("a", newline="", encoding="utf-8-sig") as handle:
            csv.writer(handle).writerows(rows_csv)
    stats["old definitions added"] = len(old)
    stats["old definitions for words the browser called empty"] = len(revived) + len([w for w in old if w not in known and w not in revived])
    return len(old)


def add_empty_headwords(src: sqlite3.Connection, out: sqlite3.Connection, stats: dict[str, int]) -> None:
    """List every Baidu headword the website knows, also the ones Baidu has no definition for.

    The base database's `headwords` holds every word that was searched (the original list, the related words and
    the scraped words) that has a Chinese character. A word with nothing to show is stored as a word with
    has_definition = 0 and layout "headword": the site lists it (a Baidu chip) but it has no senses. Pinyin-only
    strings are not in `headwords`, so none come in.
    """
    if src.execute("SELECT 1 FROM sqlite_master WHERE name = 'headwords'").fetchone() is None:
        return
    known = {w for (w,) in out.execute("SELECT word FROM words")}
    rows = [
        (word, "", "headword", "", "", "", "", 0, 0, "")
        for (word,) in src.execute("SELECT word FROM headwords")
        if word not in known and _CJK.search(word)
    ]
    out.executemany("INSERT INTO words VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    out.commit()
    stats["headwords without a definition added"] = len(rows)


def build(src_path: Path, out_db: Path, out_csv: Path, quiet: bool = False) -> tuple[dict[str, int], list[tuple]]:
    """Clean the base database. Returns (statistics, empty words as (word, reason, layout, checked))."""
    t0 = time.time()
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=120)
    total = src.execute("SELECT count(*) FROM raw").fetchone()[0]
    has_zici = src.execute("SELECT 1 FROM sqlite_master WHERE name = 'zici_raw'").fetchone() is not None
    tmp = out_db.with_suffix(".sqlite.tmp")
    for path in (tmp, out_csv):
        path.unlink(missing_ok=True)
    out_db.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(tmp)
    out.executescript(SCHEMA)
    empties: list[tuple] = []
    stats: dict[str, int] = {}
    batch = {k: [] for k in ("words", "readings", "senses", "relations", "citations", "examples", "texts", "parts", "text")}

    def flush() -> None:
        out.executemany("INSERT INTO words VALUES (?,?,?,?,?,?,?,?,?,?)", batch["words"])
        out.executemany("INSERT INTO readings VALUES (?,?,?)", batch["readings"])
        out.executemany("INSERT INTO senses VALUES (?,?,?,?,?,?,?,?,?,?)", batch["senses"])
        out.executemany("INSERT INTO relations VALUES (?,?,?,?,?)", batch["relations"])
        out.executemany("INSERT INTO citations VALUES (?,?,?,?,?,?,?,?,?)", batch["citations"])
        out.executemany("INSERT INTO examples VALUES (?,?,?,?)", batch["examples"])
        out.executemany("INSERT INTO texts VALUES (?,?,?)", batch["texts"])
        out.executemany("INSERT INTO idiom_parts VALUES (?,?,?)", batch["parts"])
        out.executemany("INSERT INTO senses_text VALUES (?,?,?,?)", batch["text"])
        out.commit()
        for rows in batch.values():
            rows.clear()

    have_text: set[str] = set()      # words that got a definition from their Baidu answer
    zici: dict[str, tuple[str, str, bytes]] = {}
    if has_zici:
        for word, kind, fetched, body in src.execute("SELECT word, kind, fetched, body FROM zici_raw"):
            zici[word] = (kind, fetched, body)
    if not quiet:
        print(f"{total:,} answers, {len(zici):,} character pages", flush=True)

    def zici_detail(word, record_empty=False):
        """The character page's detail dict and fetched time, or None (no page, or Baidu has nothing for it)."""
        page = zici.pop(word, None)
        if page is None:
            return None
        _kind, fetched, body = page
        try:
            detail = ((json.loads(zlib.decompress(body)) or {}).get("data") or {}).get("detail") or {}
        except (ValueError, zlib.error):
            stats["unreadable"] = stats.get("unreadable", 0) + 1
            return None
        if not clean(detail.get("name")) or clean(detail.get("name")) != word:
            if record_empty:      # only a character the word pages did not keep: nobody should ask again
                empties.append((word, "no entry (character page)", "zici", fetched))
                stats["no entry (character page)"] = stats.get("no entry (character page)", 0) + 1
            return None
        return detail, fetched

    with out_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        done = 0

        def emit(word, kind, d, e, fetched) -> None:
            """Store one cleaned word (the same for word pages and character pages)."""
            stats["kept"] = stats.get("kept", 0) + 1
            stats["kept " + kind] = stats.get("kept " + kind, 0) + 1
            display = display_senses(e, kind)
            has_def = int(bool(display))      # a definition the website can show (not only a hidden layer)
            if display:
                have_text.add(word)
            stats["with definition"] = stats.get("with definition", 0) + has_def
            pinyin = " / ".join(dict.fromkeys(p for p in e.readings if p))
            info = d.get("definitionInfo") if isinstance(d.get("definitionInfo"), dict) else {}
            batch["words"].append((word, clean(d.get("type")), kind, pinyin, clean(d.get("structure")),
                                   clean(d.get("baobian") or info.get("baobian")), clean(info.get("wordFormation")),
                                   has_def, len([s for s in e.senses if s[2] not in ("detail", "zici_detail")]), fetched))
            batch["readings"].extend((word, i, p) for i, p in enumerate(e.readings, 1) if p)
            batch["senses"].extend((word,) + s for s in e.senses)
            batch["relations"].extend((word,) + r for r in e.relations)
            batch["citations"].extend((word,) + c for c in e.citations)
            batch["examples"].extend((word,) + x for x in e.examples)
            batch["texts"].extend((word,) + t for t in e.texts)
            batch["parts"].extend((word,) + p for p in e.parts)
            batch["text"].extend((word, i, pinyin, text) for i, (pinyin, text) in enumerate(display, 1))
            writer.writerow(csv_row(word, kind, d, e, fetched))
            if len(batch["words"]) >= 20000:
                flush()
                if not quiet:
                    print(f"{done:,} of {total:,} answers, {time.time() - t0:.0f} s", flush=True)

        for word, status, fetched, body in src.execute("SELECT word, status, fetched, body FROM raw ORDER BY word"):
            done += 1
            if status == "miss":     # the older (VPN) scraper found nothing: an empty result too
                empties.append((word, "no entry (old cache)", "", fetched))
                stats["no entry (old cache)"] = stats.get("no entry (old cache)", 0) + 1
                continue
            if status not in ("term-v2", "idiom", "old") or body is None:
                stats["old-cache answers not used"] = stats.get("old-cache answers not used", 0) + 1
                continue
            kind = status
            try:
                answer = json.loads(zlib.decompress(body))
            except (ValueError, zlib.error):
                stats["unreadable"] = stats.get("unreadable", 0) + 1
                continue
            d = answer.get("data") or {}
            z = zici_detail(word) if word in zici else None
            if not clean(d.get("name")):
                if not z:
                    empties.append((word, "no entry", kind, fetched))
                    stats["no entry"] = stats.get("no entry", 0) + 1
                    continue
                d = {}
            elif clean(d.get("name")) != word:
                if not z:
                    empties.append((word, "answer for another word", kind, fetched))
                    stats["answer for another word"] = stats.get("answer for another word", 0) + 1
                    continue
                d = {}
            e = parse(word, kind, d)
            if z:
                parse_zici(z[0], e)
                if not d or not e.has_content():
                    d = {"type": "character", "structure": z[0].get("structType")}
                    kind = "zici"
                else:
                    d = {**d, "structure": d.get("structure") or z[0].get("structType")}
            if not e.has_content():
                empties.append((word, "no content", kind, fetched))
                stats["no content"] = stats.get("no content", 0) + 1
                continue
            if not _CJK.search(word):
                stats["pinyin-only headwords left out"] = stats.get("pinyin-only headwords left out", 0) + 1
                continue
            emit(word, kind, d, e, z[1] if z and kind == "zici" else fetched)
        # characters with a page but no word-page answer
        for word in sorted(zici):
            z = zici_detail(word, record_empty=True)
            if not z:
                continue
            e = Entry()
            parse_zici(z[0], e)
            if not e.has_content():
                empties.append((word, "no content (character page)", "zici", z[1]))
                stats["no content (character page)"] = stats.get("no content (character page)", 0) + 1
                continue
            if not _CJK.search(word):
                continue
            emit(word, "zici", {"type": "character", "structure": z[0].get("structType")}, e, z[1])
    flush()
    added = add_old_definitions(src, out, out_csv, have_text, empties, stats)
    add_empty_headwords(src, out, stats)
    out.executescript(INDEXES)
    out.execute("CREATE VIEW words_with_definition AS SELECT * FROM words WHERE has_definition = 1")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    out.executemany("INSERT INTO meta VALUES (?, ?)", [("built", now), ("source", str(src_path)), ("answers_read", str(total))]
                    + [("n_" + k.replace(" ", "_"), str(v)) for k, v in stats.items()])
    out.commit()
    out.execute("VACUUM")
    out.close()
    src.close()
    tmp.replace(out_db)
    return stats, empties


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args()
    if not args.src.exists():
        raise SystemExit(f"{args.src} not found: build it with ~/Desktop/ai/baidu-scraper/scripts/import_baidu.py")
    t0 = time.time()
    stats, empties = build(args.src, args.db, args.csv)
    print(json.dumps(stats, ensure_ascii=False), f"{time.time() - t0:.0f} s")
    print(f"{args.db}: {stats.get('kept', 0):,} words, {stats.get('with definition', 0):,} with a definition; {len(empties):,} empty results")
    return 0


if __name__ == "__main__":
    import _indexes  # rebuilds the derived indexes when the import is done (--no-indexes skips it)

    sys.exit(_indexes.run(main))
