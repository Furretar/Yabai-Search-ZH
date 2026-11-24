"""Item 2: with the Taiwan toggle, search matches the prefilled Taiwan readings.

Builds the fixture project from test_taiwan_prefill, runs the build script, then
searches. `python tests/test_taiwan_search.py` or pytest.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_taiwan_prefill import _stub_opencc, load, make_project  # noqa: E402


def heads(result) -> list[str]:
    entries, _pinyin = result
    return [entry["traditional"] for entry in entries]


def test_taiwan_search() -> None:
    _stub_opencc()
    with tempfile.TemporaryDirectory() as tmp:
        project = make_project(Path(tmp))
        # Extra fixture rows: a word only Wiktionary has, and CEDICT's 垃圾桶.
        subprocess.run(
            [sys.executable, str(project / "scripts" / "generate-indexes-optimizations" / "build_taiwan_readings.py")],
            check=True,
            capture_output=True,
            env={"PYTHONPATH": str(Path(__file__).parent / "_stubs")},
        )
        db, saved = load(project)
        for name in ("_attach_sources", "_attach_definitions", "_attach_freq",
                     "_attach_hsk", "_attach_source_pinyin"):
            setattr(db, name, lambda entries: entries)
        setattr(db, "_source_only_entries", lambda *a, **k: [])
        setattr(db, "_source_matches", lambda *a, **k: [])
        assert db.taiwan_table_status() == "ok"

        # --- toneless -----------------------------------------------------
        # A Taiwan-only reading finds the word (mainland reading is la ji).
        assert "垃圾" in heads(db.search("lese", 25, taiwan=True))
        # Strict Taiwan search: the mainland reading no longer matches.
        assert "垃圾" not in heads(db.search("laji", 25, taiwan=True))
        # Without the toggle nothing changed.
        assert "垃圾" in heads(db.search("laji", 25, taiwan=False))
        assert "垃圾" not in heads(db.search("lese", 25, taiwan=False))
        # Prefix and contains, both on the Taiwan reading (垃圾桶 is derived per character: lè jī tǒng).
        assert "垃圾桶" in heads(db.search("leji", 25, taiwan=True))  # starts with
        assert "垃圾桶" in heads(db.search("jitong", 25, taiwan=True))  # contains
        assert "垃圾桶" not in heads(db.search("laji", 25, taiwan=True))
        # Override: 血 is xiě in Taiwan.
        assert "血" in heads(db.search("xie", 25, taiwan=True))
        assert "血" not in heads(db.search("xue", 25, taiwan=True))
        # An unchanged word is found either way.
        assert "你好" in heads(db.search("nihao", 25, taiwan=True))

        # --- tone numbers -------------------------------------------------
        assert "垃圾" in heads(db.search("le4se4", 25, taiwan=True))
        assert "垃圾" not in heads(db.search("la1ji1", 25, taiwan=True))
        assert "垃圾" in heads(db.search("la1ji1", 25, taiwan=False))
        assert "垃圾" not in heads(db.search("le4se4", 25, taiwan=False))
        assert "血" in heads(db.search("xie3", 25, taiwan=True))
        assert "血" not in heads(db.search("xue4", 25, taiwan=True))
        # A wrong tone is still rejected on the Taiwan reading.
        assert "垃圾" not in heads(db.search("le1se4", 25, taiwan=True))
        # The whole reading ranks first.
        assert heads(db.search("le4se4", 25, taiwan=True))[:1] == ["垃圾"]
        # The fast first tier is a prefix match on the same Taiwan reading.
        assert "垃圾" in heads(db.search("le4se4", 25, taiwan=True, part="primary"))

        # --- source-only dictionaries ---------------------------------------
        # 垃圾袋 exists only in Wiktionary; its derived Taiwan reading is lè jī dài.
        found = db.search("lejidai", 25, taiwan=True)[0]
        assert [e["traditional"] for e in found] == ["垃圾袋"]
        assert found[0]["pinyin"] == "lā jī dài"  # the row keeps the source's reading
        assert found[0]["pinyin_taiwan"] == "lè jī dài"
        assert db.search("lajidai", 25, taiwan=True)[0] == []
        assert [e["traditional"] for e in db.search("lajidai", 25, taiwan=False)[0]] == ["垃圾袋"]

        # --- without the table nothing is derived while serving -------------
        (project / "data" / "generated-indexes-optimizations" / "taiwan_readings.sqlite").rename(
            project / "data" / "generated-indexes-optimizations" / "taiwan_readings.off"
        )
        db, _ = load(project)
        for name in ("_attach_sources", "_attach_definitions", "_attach_freq",
                     "_attach_hsk", "_attach_source_pinyin"):
            setattr(db, name, lambda entries: entries)
        setattr(db, "_source_only_entries", lambda *a, **k: [])
        setattr(db, "_source_matches", lambda *a, **k: [])
        assert db.taiwan_table_status() == "missing"
        # No table: Taiwan readings come only from the pregenerated table, so the rows' own readings are searched
        # and shown (the mainland one for 垃圾), and nothing is computed live.
        assert "垃圾" not in heads(db.search("lese", 25, taiwan=True))
        found = db.search("laji", 25, taiwan=True)[0]
        assert "垃圾" in [e["traditional"] for e in found]
        assert all(not e.get("pinyin_taiwan") for e in found)
        db.search("le4se4", 25, taiwan=True)
        db.search("la1ji1", 25, taiwan=True)
        for name in [m for m in sys.modules if m.startswith("yabai_search") or m == "opencc"]:
            del sys.modules[name]
        if "_real_opencc" in sys.modules:
            sys.modules["opencc"] = sys.modules.pop("_real_opencc")
        sys.modules.update(saved)
        if str(project) in sys.path:
            sys.path.remove(str(project))


if __name__ == "__main__":
    test_taiwan_search()
    print("ok")
