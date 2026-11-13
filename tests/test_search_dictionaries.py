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
requires_unihan = pytest.mark.skipif(
    not (DATA / "unihan.sqlite").exists(), reason="Unihan database not built"
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
    expected = [
        "cedict", "moe", "moe_revised", "wiktionary", "zhwiktionary", "jpwiktionary", "baidu", "unihan",
    ]
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


@requires_unihan
@requires_revised
def test_quoted_han_character_finds_the_characters_it_defines(search):
    """A quoted Han character searches definitions, Unihan's glosses included: "艖" finds 䑡 ("same as 艖").

    艖 also appears in a poem quoted under 小船。 and 切意's 全心全意。 in the revised MOE dictionary, but
    the page shows only the definition (up to the first 。), so those words must not be results.
    """
    data = search('"艖"')
    assert [entry["simplified"] for entry in data["results"]] == ["䑡"]
    defs = [d for d in data["results"][0]["definitions"] if d["anchor"] == "unihan"]
    assert defs and any("艖" in sense for sense in defs[0]["defs"])


def test_stale_definition_grams_are_ignored(tmp_path, monkeypatch):
    """The gram index is a negative filter: built from other files than the current ones, it must not be used."""
    import sqlite3

    from yabai_search import db

    path = tmp_path / "definition_grams.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE grams (gram TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO grams VALUES ('艖')")
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    source = tmp_path / "source.sqlite"
    source.write_bytes(b"current contents")
    monkeypatch.setattr(db, "DEFINITION_GRAMS_DB_PATH", path)
    monkeypatch.setattr(db, "_definition_grams_sources", lambda: {"fake": source})
    monkeypatch.setattr(db, "_file_sha256_cache", {})

    def grams():
        monkeypatch.setattr(db, "_definition_grams_cache", None)
        return db._definition_grams()

    connection.execute("INSERT INTO meta VALUES ('source:fake', 'not-the-current-hash')")
    connection.commit()
    assert grams() == set()  # stale: no filtering, the scans run

    connection.execute("UPDATE meta SET value = ?", (db._file_sha256(source),))
    connection.commit()
    connection.close()
    assert grams() == {"艖"}  # built from the current file: used


def test_reading_ranking_counts_glosses_not_grouped_senses():
    """Grouping ";" glosses into one displayed sense must not change which reading leads a word.

    The ranking only works on gloss counts: as grouped senses 那 would lead with nuó, 只 with zhī,
    地方 with dì fāng and 干 with gàn. Checked on the SQL helper, so it needs no database.
    """
    import sqlite3

    from yabai_search import db

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE senses (word TEXT, reading TEXT, position INTEGER, text TEXT)")
    connection.executemany(
        "INSERT INTO senses VALUES (?, ?, ?, ?)",
        [("只", "zhǐ", 1, "only; merely; just"), ("只", "zhī", 1, "classifier for birds"), ("只", "zhī", 2, "single")],
    )
    counts = dict(
        ((reading), count)
        for reading, count in connection.execute(
            f"SELECT reading, SUM({db._gloss_count()}) FROM senses GROUP BY reading"
        )
    )
    assert counts == {"zhǐ": 3, "zhī": 2}  # three glosses in one sense still outweigh two plain senses


@requires_revised
def test_quoted_definition_search_puts_unselected_matches_under_other_dictionaries(search):
    """A row whose definition matches only in an unselected dictionary is category 2 ("Other Dictionaries").

    "會" is found in many MOE definitions. With only CC-CEDICT selected, the rows that match in CEDICT come
    first (category 1) and every row found through MOE alone is category 2 with its matching dictionaries
    listed. The match is verified in both scripts (會 also finds 会) and through the headword a row was
    found as (the MOE entry 公安 is shown as CEDICT's 公安县), so no row is left unverified.
    """
    rows = []
    for offset in range(0, 1000, 25):  # category 2 starts after the CEDICT matches
        rows += search('"會"', dicts="cedict", lang="EN", offset=offset)["results"]
    categories = [row["category"] for row in rows]
    assert categories == sorted(categories)  # category 1 first, then 2
    assert 2 in categories
    for row in rows:
        if row["category"] == 1:  # never category 1 without a verified match in a selected dictionary
            assert "cedict" in row.get("match_anchors", []), row["simplified"]
        else:
            assert "cedict" not in row.get("match_anchors", [])
    # With the dictionaries that hold the matches selected, the same rows are category 1.
    moe = search('"會"', dicts="moe,moe_revised", lang="EN")["results"]
    assert moe and all(row["category"] == 1 for row in moe)


def test_toneless_pinyin_search_keeps_frequency_order(search):
    """A toneless pinyin query lists the matching words by frequency, not by how many senses each has."""
    ranks = [
        row["freq"]["rank"]
        for row in search("zhi", dicts="cedict,wiktionary", lang="EN")["results"]
        if row.get("freq")
    ]
    assert len(ranks) >= 10
    assert ranks == sorted(ranks)


@requires_revised
def test_quoted_definition_search_shows_the_common_reading_and_hides_unshown_glosses(search):
    """會 is listed under "Other Dictionaries" with huì (never guì), since the Wiktionary gloss
    "only used in 會稽" that contains it is not shown on the page."""
    rows = []
    for offset in range(0, 3900, 25):
        rows = [r for r in search('"會"', dicts="cedict,wiktionary", lang="EN", offset=offset)["results"] if r["traditional"] == "會"]
        if rows:
            break
    assert rows
    assert rows[0]["pinyin"] == "huì"
    assert rows[0]["category"] == 2


def test_baidu_headwords_use_the_gram_index():
    """百度汉语 headwords are in gram_words (a 1-2 character query is one lookup, not a scan of 550k rows)."""
    if not (db.BAIDU_DB_PATH.exists() and db.GRAM_WORDS_DB_PATH.exists()):
        pytest.skip("Baidu or gram_words database not built")
    assert db._gram_words_version_ok()
    assert "美味" in db._gram_candidates("baidu_head", "美味")
    assert db._gram_candidates("baidu_head", "美") is None or "美味" in db._gram_candidates("baidu_head", "美")


def test_gram_filtered_heads_match_the_like_scan_they_replace():
    """A wildcard or mixed headword pattern answered from the gram index gives the scan's exact rows."""
    import sqlite3

    from yabai_search import db

    if not (db.BAIDU_DB_PATH.exists() and db._gram_words_version_ok()):
        pytest.skip("Baidu database or gram index not built")
    connection = sqlite3.connect(f"file:{db.BAIDU_DB_PATH}?mode=ro", uri=True)
    connection.execute("PRAGMA case_sensitive_like=ON")
    try:
        for form, anywhere in (("乐*", False), ("?后", False), ("*美*", False), ("不*不*", False), ("卡拉OK", True), ("图书馆", True)):
            pattern = db._like_pattern(f"*{form}*" if anywhere else form)
            scanned = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT word FROM headwords WHERE word LIKE ? ESCAPE '\\' ORDER BY word",
                    (pattern,),
                )
            ]
            assert db._gram_filtered_heads("baidu_head", form) == scanned, form
    finally:
        connection.close()
    assert db._gram_filtered_heads("baidu_head", "abc*") is None  # no Han character: scan
