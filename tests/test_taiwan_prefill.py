"""Item 1: the build script prefills Taiwan readings for every dictionary.

Runs against a throwaway copy of the project with tiny fixture databases, so it
cannot touch real data. `python tests/test_taiwan_prefill.py` or pytest.
"""

from __future__ import annotations

import importlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _stub_opencc() -> None:
    """OpenCC is not needed for these fixtures (all words are already traditional)."""
    current = sys.modules.get("opencc")
    if current is not None and getattr(current, "_is_stub", False):
        return
    if current is not None and "_real_opencc" not in sys.modules:
        sys.modules["_real_opencc"] = current
    module = types.ModuleType("opencc")

    class OpenCC:
        def __init__(self, *_args):
            pass

        def convert(self, text):
            return {"垃圾": "垃圾", "會": "会", "袋": "袋", "桶": "桶"}.get(text, text)

    module.OpenCC = OpenCC
    module._is_stub = True
    sys.modules["opencc"] = module


def make_project(base: Path) -> Path:
    project = base / "proj"
    shutil.copytree(
        ROOT, project, ignore=shutil.ignore_patterns("data", "__pycache__", "tests")
    )
    data = project / "data"
    data.mkdir()
    (data / "dictionary-databases").mkdir()  # every dictionary database lives here

    from yabai_search.pinyin import to_toneless

    cedict = sqlite3.connect(data / "dictionary-databases" / "cedict.sqlite")
    cedict.execute(
        "CREATE TABLE entries (id INTEGER PRIMARY KEY, simplified TEXT, traditional TEXT,"
        " pinyin TEXT, pinyin_numeric TEXT, pinyin_toneless TEXT, english TEXT,"
        " english_normalized TEXT)"
    )
    rows = [
        # simplified, traditional, marked, numeric, english
        ("垃圾", "垃圾", "lā jī", "la1 ji1", "rubbish"),
        ("垃圾桶", "垃圾桶", "lā jī tǒng", "la1 ji1 tong3", "trash can"),
        ("血", "血", "xuè", "xue4", "blood"),
        ("会", "會", "huì", "hui4", "can"),
        ("会", "會", "kuài", "kuai4", "accounting"),
        ("下海", "下海", "xià hǎi", "xia4 hai3", "to go to sea (Taiwan pr. [xia4 hai2])"),
        ("你好", "你好", "nǐ hǎo", "ni3 hao3", "hello"),
        ("袋", "袋", "dài", "dai4", "bag"),  # also gives the Wiktionary splitter "dài"
    ]
    for index, (simp, trad, marked, numeric, english) in enumerate(rows, 1):
        cedict.execute(
            "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)",
            (index, simp, trad, marked, numeric, to_toneless(numeric), english, english),
        )
    cedict.commit()
    # FTS index used by the English definition fallback.
    cedict.execute(
        "CREATE VIRTUAL TABLE entries_fts USING fts5("
        "english_normalized, content='entries', content_rowid='id')"
    )
    cedict.execute(
        "INSERT INTO entries_fts(rowid, english_normalized) "
        "SELECT id, english_normalized FROM entries"
    )
    cedict.commit()
    cedict.close()

    (data / "dictionary-databases" / "moe-taiwan").mkdir()
    moe = sqlite3.connect(data / "dictionary-databases" / "moe-taiwan" / "moe.sqlite")
    moe.execute("CREATE TABLE moe (word TEXT, pinyin TEXT, zhuyin TEXT)")
    for word, pinyin in [
        ("垃圾", "lè sè"),
        ("垃", "lè"),
        ("圾", "sè"),
        ("血", "xuè"),  # MOE says xuè; the override in overrides.py says xiě
        ("你好", "nǐ hǎo"),
    ]:
        moe.execute("INSERT INTO moe VALUES (?,?,'')", (word, pinyin))
    moe.commit()
    moe.close()

    wik = sqlite3.connect(data / "dictionary-databases" / "wiktionary.sqlite")
    wik.execute(
        "CREATE TABLE readings (id INTEGER PRIMARY KEY, word TEXT, kind TEXT, pinyin TEXT, "
        "pinyin_toneless TEXT, pinyin_spaced TEXT, pinyin_numeric TEXT, simplified TEXT, "
        "is_cedict INTEGER DEFAULT 0)"
    )
    wik.execute(
        "INSERT INTO readings (word, kind, pinyin, pinyin_toneless, pinyin_spaced, "
        "pinyin_numeric, simplified, is_cedict) VALUES "
        "('垃圾袋','mandarin','lā jī dài','lajidai','lā jī dài','la1 ji1 dai4','垃圾袋',0)"
    )
    wik.execute(
        "INSERT INTO readings (word, kind, pinyin, pinyin_toneless, pinyin_spaced, "
        "pinyin_numeric, simplified, is_cedict) VALUES "
        "('垃圾袋','wu','le sa de','lesade','le sa de','le sa de','垃圾袋',0)"
    )
    wik.commit()
    wik.close()
    return project


def load(project: Path):
    saved = {
        name: sys.modules[name]
        for name in list(sys.modules)
        if name == "yabai_search" or name.startswith("yabai_search.")
    }
    for name in list(saved):
        del sys.modules[name]
    sys.path.insert(0, str(project))
    _stub_opencc()
    return importlib.import_module("yabai_search.db"), saved


def test_prefill() -> None:
    _stub_opencc()
    with tempfile.TemporaryDirectory() as tmp:
        project = make_project(Path(tmp))
        result = subprocess.run(
            [sys.executable, str(project / "scripts" / "generate-indexes-optimizations" / "build_taiwan_readings.py")],
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(Path(__file__).parent / "_stubs")},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        out = sqlite3.connect(project / "data" / "generated-indexes-optimizations" / "taiwan_readings.sqlite")
        got = {
            (src, word, main): (reading, changed)
            for src, word, main, reading, changed in out.execute(
                "SELECT tw_source, tw_word, tw_mainland, tw_reading, tw_changed FROM readings"
            )
        }
        # Whole-word MOE reading replaces CEDICT's (monophonic word).
        assert got[("cedict", "垃圾", "lā jī")] == ("lè sè", 1)
        # Not in MOE as a word: derived character by character.
        assert got[("cedict", "垃圾桶", "lā jī tǒng")] == ("lè sè tǒng", 1)
        # overrides.py beats the MOE reading.
        assert got[("cedict", "血", "xuè")] == ("xiě", 1)
        # Polyphonic word keeps each specific CEDICT reading.
        assert got[("cedict", "會", "huì")] == ("huì", 0)
        assert got[("cedict", "會", "kuài")] == ("kuài", 0)
        # CEDICT's own "Taiwan pr." note is used when MOE lacks the word.
        assert got[("cedict", "下海", "xià hǎi")] == ("xià hái", 1)
        # Unchanged words are stored too, as their own reading.
        assert got[("cedict", "你好", "nǐ hǎo")] == ("nǐ hǎo", 0)
        # MOE rows are Taiwan readings already; the override still applies.
        assert got[("moe", "血", "xuè")] == ("xiě", 1)
        assert got[("moe", "垃圾", "lè sè")] == ("lè sè", 0)
        # Other dictionaries are covered too (Wiktionary: Mandarin rows only).
        assert got[("wiktionary", "垃圾袋", "lā jī dài")] == ("lè sè dài", 1)
        assert not any(src == "wiktionary" and main == "le sa de" for src, _w, main in got)
        # Search keys.
        row = out.execute(
            "SELECT tw_numeric, tw_toneless FROM readings "
            "WHERE tw_source='cedict' AND tw_word='垃圾'"
        ).fetchone()
        assert row == ("le4 se4", "lese")
        out.close()

        # The source databases were not modified.
        for name in ("cedict", "moe", "wiktionary"):
            conn = sqlite3.connect(project / "data" / f"{name}.sqlite")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            assert "readings" not in tables or name == "wiktionary"
            conn.close()

        # Lookup + status, and staleness detection.
        db, saved = load(project)
        assert db.taiwan_table_status() == "ok"
        lookup = db._taiwan_lookup([("垃圾", "lā jī"), ("你好", "nǐ hǎo"), ("無", "wú")])
        assert lookup == {("垃圾", "lā jī"): "lè sè", ("你好", "nǐ hǎo"): None}
        # The fixture has no definition tables; only the reading step is under test.
        for name in ("_attach_sources", "_attach_definitions", "_attach_freq",
                     "_attach_hsk", "_attach_source_pinyin"):
            setattr(db, name, lambda entries: entries)
        entries = db._decorate(
            [
                {"simplified": "垃圾", "traditional": "垃圾", "pinyin": "lā jī",
                 "english": "rubbish", "english_normalized": "rubbish"},
            ]
        )
        assert entries[0]["pinyin_taiwan"] == "lè sè"

        original = db.TAIWAN_READINGS["血"]
        db.TAIWAN_READINGS["血"] = "xuě"  # an override edit changes the signature
        assert db.taiwan_table_status() == "stale"
        db.TAIWAN_READINGS["血"] = original
        assert db.taiwan_table_status() == "ok"
        conn = sqlite3.connect(project / "data" / "dictionary-databases" / "moe-taiwan" / "moe.sqlite")  # a source is edited
        conn.execute("INSERT INTO moe VALUES ('桶','tǒng','')")
        conn.commit()
        conn.close()
        assert db.taiwan_table_status() == "stale"
        for name in [m for m in sys.modules if m.startswith("yabai_search") or m == "opencc"]:
            del sys.modules[name]
        if "_real_opencc" in sys.modules:
            sys.modules["opencc"] = sys.modules.pop("_real_opencc")
        sys.modules.update(saved)
        if str(project) in sys.path:
            sys.path.remove(str(project))


if __name__ == "__main__":
    test_prefill()
    print("ok")
