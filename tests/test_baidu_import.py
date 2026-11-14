"""The Baidu base database is converted into the clean one the website reads (scripts/download-dictionaries/import_baidu.py)."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import zlib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download-dictionaries" / "import_baidu.py"
spec = importlib.util.spec_from_file_location("import_baidu_under_test", SCRIPT)
conv = importlib.util.module_from_spec(spec)
sys.modules["import_baidu_under_test"] = conv
spec.loader.exec_module(conv)


def z(answer: dict) -> bytes:
    return zlib.compress(json.dumps(answer, ensure_ascii=False).encode())


def term(name, groups):
    """A term-v2 answer: groups of (pinyin, basic definitions, detailed definitions)."""
    return {"errno": 0, "data": {"name": name, "termVersion": 2, "comprehensiveDefinition": [
        {"pinyin": p, "basicDefinition": [{"definition": d, "cixing": ["名词"]} for d in basic],
         "detailDefinition": [{"definition": d} for d in detail]} for p, basic, detail in groups]}}


def character(name, pinyin_defs):
    return {"errno": 0, "data": {"detail": {"name": name, "radical": "木", "wordStrokeCount": "4", "comprehensiveDefinition": [
        {"pinyin": p, "basicDefinition": [{"definition": d, "cixing": ["动词"], "liju": [{"name": "generated"}]}],
         "detailDefinition": [{"definition": "古义：" + d}]} for p, d in pinyin_defs]}}}


@pytest.fixture()
def base(tmp_path):
    path = tmp_path / "baidu.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE raw (word TEXT PRIMARY KEY, status TEXT NOT NULL, fetched TEXT NOT NULL, body BLOB)")
    db.execute("CREATE TABLE zici_raw (word TEXT PRIMARY KEY, kind TEXT NOT NULL, fetched TEXT NOT NULL, body BLOB)")
    rows = [
        ("学习", "term-v2", "t", z(term("学习", [("xué xí", ["基本"], ["详细一", "详细二"])]))),
        ("行", "term-v2", "t", z(term("行", [("xíng", ["走"], []), ("háng", ["行业"], [])]))),
        ("空", "term-v2", "t", z({"errno": 0, "data": {"name": ""}})),
        ("a", "term-v2", "t", z(term("a", [("a", ["x"], [])]))),            # no Chinese character: left out
        ("旧", "miss", "t", None),
        ("木", "old", "t", z({"errno": 0, "data": {"name": ""}})),          # the word page has nothing, the character page does
    ]
    db.executemany("INSERT INTO raw VALUES (?,?,?,?)", rows)
    db.executemany("INSERT INTO zici_raw VALUES (?,?,?,?)", [
        ("木", "ok", "t", z(character("木", [("mù", "树木")]))),
        ("䷀", "empty", "t", z({"errno": 0, "data": {"detail": {}}})),
        ("朩", "ok", "t", z(character("朩", [("mù", "同木"), ("yù", "别义")]))),
    ])
    db.commit()
    db.close()
    return path


def test_conversion_keeps_what_has_content_and_lists_the_empty(base, tmp_path):
    stats, empties = conv.build(base, tmp_path / "clean.sqlite", tmp_path / "clean.csv", quiet=True)
    db = sqlite3.connect(tmp_path / "clean.sqlite")
    assert {r[0] for r in db.execute("SELECT word FROM words")} == {"学习", "行", "木", "朩"}
    reasons = {word: reason for word, reason, _layout, _checked in empties}
    assert reasons["空"] == "no entry" and reasons["旧"] == "no entry (old cache)" and reasons["䷀"] == "no entry (character page)"
    assert "a" not in reasons                       # a Latin headword is not a dictionary word, nor an empty search


def test_the_website_text_of_a_sense(base, tmp_path):
    conv.build(base, tmp_path / "clean.sqlite", tmp_path / "clean.csv", quiet=True)
    db = sqlite3.connect(tmp_path / "clean.sqlite")
    text = lambda word: [r for r in db.execute("SELECT pinyin, text FROM senses_text WHERE word = ? ORDER BY position", (word,))]
    assert text("学习") == [("xué xí", "详细一"), ("xué xí", "详细二")]    # the detailed definitions replace the basic one
    assert text("行") == [("xíng", "走"), ("háng", "行业")]                # several readings: each text keeps its own reading, no prefix
    assert text("木") == [("mù", "(动词) 树木")]                          # a character: the modern definition with its part of speech; the classical one is not shown
    assert text("朩") == [("mù", "(动词) 同木"), ("yù", "(动词) 别义")]
    # ... but it is kept in the clean database
    assert db.execute("SELECT definition FROM senses WHERE word = '木' AND layer = 'zici_detail'").fetchone() == ("古义：树木",)
    assert [r[0] for r in db.execute("SELECT word FROM words_with_definition ORDER BY word")] == sorted(["学习", "朩", "木", "行"])


def test_character_facts_are_kept_and_generated_examples_are_not(base, tmp_path):
    conv.build(base, tmp_path / "clean.sqlite", tmp_path / "clean.csv", quiet=True)
    db = sqlite3.connect(tmp_path / "clean.sqlite")
    facts = dict(db.execute("SELECT kind, text FROM texts WHERE word = '木'"))
    assert facts["radical"] == "木" and facts["stroke_count"] == "4"
    assert db.execute("SELECT count(*) FROM examples WHERE text = 'generated'").fetchone()[0] == 0
    assert db.execute("SELECT layout FROM words WHERE word = '木'").fetchone()[0] == "zici"
    assert (tmp_path / "clean.csv").read_text(encoding="utf-8-sig").count("\n") >= 5


def test_old_definition_is_added_only_when_the_answer_gave_none(base, tmp_path):
    """The older (VPN) scrape's text fills a word whose own answer had no definition, and never replaces one."""
    db = sqlite3.connect(base)
    db.execute("CREATE TABLE senses (word TEXT, position INTEGER, text TEXT)")
    db.executemany("INSERT INTO senses VALUES (?,?,?)", [
        ("学习", 1, "旧的定义"),                 # has its own definition: the old text must be ignored
        ("空", 1, "旧的意思"),                    # the browser answer was empty: the old text revives the word
        ("新", 1, "只有旧抓取知道"),              # not in the raw answers at all
    ])
    db.execute("INSERT INTO raw VALUES ('无', 'term-v2', 't', ?)", (z({"errno": 0, "data": {"name": "无", "baike": ""}}),))
    db.commit()
    db.close()
    stats, empties = conv.build(base, tmp_path / "clean.sqlite", tmp_path / "clean.csv", quiet=True)
    out = sqlite3.connect(tmp_path / "clean.sqlite")
    text = lambda word: [r[0] for r in out.execute("SELECT text FROM senses_text WHERE word = ? ORDER BY position", (word,))]
    assert text("学习") == ["详细一", "详细二"]
    assert text("空") == ["旧的意思"] and text("新") == ["只有旧抓取知道"]
    assert out.execute("SELECT layout, has_definition FROM words WHERE word = '新'").fetchone() == ("vpn", 1)
    assert "空" not in {row[0] for row in empties}
    assert stats["old definitions added"] == 2


def test_headwords_without_a_definition_are_listed_and_a_word_has_its_reading(base, tmp_path):
    """Every base headword that has a Chinese character is a word (has_definition 0 when there is nothing to show),
    and the words that have a definition keep Baidu's reading."""
    db = sqlite3.connect(base)
    db.execute("CREATE TABLE headwords (word TEXT PRIMARY KEY)")
    db.executemany("INSERT INTO headwords VALUES (?)", [("学习",), ("没有",), ("空",), ("abc",)])
    db.commit()
    db.close()
    stats, _ = conv.build(base, tmp_path / "clean.sqlite", tmp_path / "clean.csv", quiet=True)
    out = sqlite3.connect(tmp_path / "clean.sqlite")
    assert dict(out.execute("SELECT word, has_definition FROM words WHERE word IN ('学习','没有','空','abc')")) == {"学习": 1, "没有": 0, "空": 0}
    assert out.execute("SELECT layout FROM words WHERE word = '没有'").fetchone() == ("headword",)
    assert out.execute("SELECT pinyin FROM words WHERE word = '学习'").fetchone() == ("xué xí",)
    assert stats["headwords without a definition added"] == 2
