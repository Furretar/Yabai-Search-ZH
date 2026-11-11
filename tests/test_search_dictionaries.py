"""Per-dictionary integration tests: definitions, chips, readings and metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

from yabai_search import db
from yabai_search.default_dictionaries import DEFAULTS

DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "cedict.sqlite").exists(), reason="CEDICT database not built"
)

def _moe_tables() -> set[str]:
    """Tables of the merged MOE database (the concise one plain, the revised one rev_)."""
    import sqlite3

    path = DATA / "moe-taiwan" / "moe.sqlite"
    if not path.exists():
        return set()
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()


requires_moe = pytest.mark.skipif(
    "moe" not in _moe_tables(), reason="MOE concise dictionary not built"
)
requires_revised = pytest.mark.skipif(
    "rev_moe" not in _moe_tables(), reason="MOE comprehensive dictionary not built"
)
requires_wiktionary = pytest.mark.skipif(
    not (DATA / "wiktionary.sqlite").exists(), reason="Wiktionary database not built"
)
requires_subtlex = pytest.mark.skipif(
    not (DATA / "subtlex.sqlite").exists(), reason="SUBTLEX database not built"
)
requires_freq = pytest.mark.skipif(
    not db.FREQ_DB_PATH.exists(), reason="frequency database not built"
)
requires_hsk = pytest.mark.skipif(
    not (DATA / "hsk.sqlite").exists(), reason="HSK database not built"
)
requires_all_sources = pytest.mark.skipif(
    not (
        "rev_moe" in _moe_tables()
        and all(
            (DATA / name).exists()
            for name in (
                "moe-taiwan/moe.sqlite",
                "wiktionary.sqlite",
                "zhwiktionary.sqlite",
                "jpwiktionary.sqlite",
            )
        )
    ),
    reason="not all dictionary databases are built",
)

requires_baidu = pytest.mark.skipif(
    not (DATA / "baidu.sqlite").exists(), reason="Baidu database not built"
)


def _anchors(entry):
    return [source["anchor"] for source in entry.get("sources", [])]


def _definition_anchors(entry):
    return [definition["anchor"] for definition in entry.get("definitions", [])]


def test_cedict_source_and_definition(search, find_result):
    """CC-CEDICT supplies the headword reading and its definitions."""
    entry = find_result(search("档案"), "档案")
    assert entry is not None
    assert "cedict" in _anchors(entry)
    assert "cedict" in _definition_anchors(entry)
    assert entry["pinyin"] == "dàng àn"


def test_cedict_also_written_notes_become_other_forms(search, find_result):
    """An "also written X" note is a form, not a numbered sense."""
    entry = find_result(search("没精打采"), "没精打采")
    assert entry is not None
    senses = [
        sense
        for definition in entry["definitions"]
        if definition["anchor"] == "cedict"
        for group in definition.get("groups", [])
        for sense in group.get("senses", [])
    ]
    assert not any("also written" in sense for sense in senses)
    assert any(form["traditional"] == "沒精打彩" for form in entry["forms"])


def test_cedict_also_written_variant_without_own_entry(search, find_result):
    """A note-only variant still appears under Other forms."""
    entry = find_result(search("三宝太监"), "三宝太监")
    assert entry is not None
    assert entry["main"] is None
    assert any(form["traditional"] == "三保太監" for form in entry["forms"])


def test_cedict_also_written_does_not_redirect(search, find_result):
    """Two standard spellings stay separate instead of redirecting to each other."""
    entry = find_result(search("来福枪"), "来福枪")
    assert entry is not None
    assert entry["main"] is None
    assert any(form["traditional"] == "來復槍" for form in entry["forms"])


@requires_moe
def test_moe_concise_source_and_taiwan_reading(search, find_result):
    """The concise MOE dictionary supplies Chinese definitions and Taiwan pinyin."""
    entry = find_result(search("垃圾"), "垃圾")
    assert entry is not None
    assert "moe" in _anchors(entry)
    assert "moe" in _definition_anchors(entry)
    assert entry["pinyin_taiwan"] == "lè sè"


@requires_revised
def test_moe_revised_source_and_definition(search, find_result):
    """The comprehensive MOE dictionary adds a separate definition block."""
    entry = find_result(search("档案"), "档案")
    assert entry is not None
    assert "moe_revised" in _anchors(entry)
    definition = next(
        item for item in entry["definitions"] if item["anchor"] == "moe_revised"
    )
    assert definition["label"] == "重編國語辭典修訂本"
    assert definition["defs"]


@requires_revised
def test_moe_revised_definition_drops_examples(search, find_result):
    """The revised MOE definition stops at the first 。; examples stay in the DB."""
    entry = find_result(search("连续"), "连续")
    assert entry is not None
    definition = next(
        item for item in entry["definitions"] if item["anchor"] == "moe_revised"
    )
    assert definition["defs"] == ["繼續不斷。"]
    assert all("如：" not in text and "《" not in text for text in definition["defs"])

    # The source row still holds the full text, including the example.
    import sqlite3

    conn = sqlite3.connect(db.MOE_REVISED_DB_PATH)
    try:
        full = conn.execute(
            "SELECT text FROM rev_senses WHERE word = ? ORDER BY position LIMIT 1",
            ("連續",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert full.startswith("繼續不斷。")
    assert "如：" in full


@requires_revised
@requires_wiktionary
def test_moe_revised_can_supply_a_word_cedict_lacks(search, find_result):
    """A source-only word is returned with its comprehensive MOE definition."""
    assert db.lookup("劣性") == []
    entry = find_result(search("劣性"), "劣性")
    assert entry is not None
    assert "moe_revised" in _anchors(entry)
    assert "moe_revised" in _definition_anchors(entry)


@requires_moe
def test_taiwan_toggle_uses_moe_readings(search, find_result):
    """lese finds 垃圾 only when the Taiwan reading toggle is on."""
    without = search("lese")
    with_taiwan = search("lese", taiwan="true")
    assert find_result(without, "垃圾") is None
    assert find_result(with_taiwan, "垃圾") is not None


@requires_wiktionary
def test_wiktionary_gloss_source(search, find_result):
    """English Wiktionary glosses appear as their own source."""
    entry = find_result(search("擸𢶍"), "擸𢶍")
    assert entry is not None
    assert "wiktionary" in _anchors(entry)
    assert "wiktionary" in _definition_anchors(entry)


def test_zhwiktionary_source(search, find_result):
    """Chinese Wiktionary definitions are attached for its headwords."""
    entry = find_result(search("0擼"), "0撸")
    assert entry is not None
    assert "zhwiktionary" in _anchors(entry)
    assert "zhwiktionary" in _definition_anchors(entry)


@requires_wiktionary
def test_jpwiktionary_source(search, find_result):
    """Japanese Wiktionary definitions are attached separately."""
    entry = find_result(search("劣性"), "劣性")
    assert entry is not None
    assert "jpwiktionary" in _anchors(entry)
    assert "jpwiktionary" in _definition_anchors(entry)


@requires_baidu
def test_baidu_source_chip(search, find_result):
    """The Baidu headword list contributes a source chip."""
    entry = find_result(search("AA制"), "AA制")
    assert entry is not None
    assert "baidu" in _anchors(entry)


@requires_wiktionary
def test_dialect_reading_is_kept_untinted(search, find_result):
    """A Wu/Xiang-only reading is shown but marked as non-Mandarin."""
    entry = find_result(search("㑲"), "㑲")
    assert entry is not None
    assert entry["pinyin_no_tone"] is True
    assert entry["pinyin_label"] == "Xiang"


def test_cedict_taiwan_note_is_used(search, find_result):
    """A CEDICT 'Taiwan pr.' note becomes pinyin_taiwan when MOE lacks the word."""
    entry = find_result(search("便秘"), "便秘")
    assert entry is not None
    assert "moe" not in _anchors(entry)
    assert "moe_revised" not in _anchors(entry)
    assert entry["pinyin_taiwan"] == "biàn bì"


def test_manual_taiwan_override(search, find_result):
    """A manual override wins for a character with a Taiwan reading exception."""
    entry = find_result(search("血"), "血")
    assert entry is not None
    assert entry["pinyin_taiwan"] == "xiě"


@requires_freq
def test_corpus_frequency(search, find_result):
    """The configured frequency rank is attached to matching entries."""
    entry = find_result(search("的"), "的")
    assert entry is not None
    assert entry["freq"]["rank"] == 1
    assert entry["freq"]["count"] > 1_000_000
    source = db.frequency_source()
    assert source is not None and source["anchor"] == db.FREQ_ANCHOR


@requires_hsk
def test_hsk_levels_are_attached(search, find_result):
    """The latest HSK levels (3.0 and 2026) are attached next to frequency."""
    entry = find_result(search("档案"), "档案")
    assert entry is not None
    assert entry["hsk"] == {"new": 6, "newest": 6}
    laji = find_result(search("垃圾"), "垃圾")
    assert laji is not None
    assert laji["hsk"]["new"] == 4
    assert laji["hsk"]["newest"] == 4


@requires_moe
@requires_revised
@requires_wiktionary
def test_source_chips_are_unique_and_in_registry_order(search, find_result):
    """Source chips appear once each, in the dictionary registry order."""
    entry = find_result(search("垃圾"), "垃圾")
    anchors = _anchors(entry)
    assert len(anchors) == len(set(anchors))
    expected = ["cedict", "moe", "moe_revised", "wiktionary", "zhwiktionary", "jpwiktionary"]
    assert anchors == [anchor for anchor in expected if anchor in anchors]


@requires_all_sources
def test_sources_metadata_reports_all_built_dictionaries():
    """The About-page metadata includes every built dictionary and its count."""
    sources = {source["anchor"]: source for source in db.sources()}
    for source in db._SOURCE_QUERIES:
        if source["database"].exists():
            assert source["anchor"] in sources
    assert sources["moe_revised"]["entries"] > 100_000
    assert sources["moe_revised"]["updated"]


def test_default_dictionaries_include_both_taiwan_sources():
    """The TW default switches on the concise and comprehensive MOE dictionaries."""
    assert DEFAULTS["TW"] == ["moe", "moe_revised"]
