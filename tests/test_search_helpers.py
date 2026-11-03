"""Unit tests for the small helpers that drive search behavior."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from yabai_search import db
from yabai_search.default_dictionaries import DEFAULTS
from yabai_search.services import _clean_report_settings, _report_search_url
from yabai_search.overrides import normalize_english
from yabai_search.pinyin import numeric_to_marked, to_numeric, to_toneless
from scripts.import_cedict import see_target

DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "cedict.sqlite").exists(), reason="CEDICT database not built"
)


def test_page_groups_keeps_a_simplified_word_on_one_page():
    """Rows sharing a simplified form page as one group, never split."""
    rows = [
        {"simplified": "了"},
        {"simplified": "瞭"},
        {"simplified": "了"},
        {"simplified": "和"},
    ]
    groups = db.group_rows(rows)
    assert [[row["simplified"] for row in group] for group in groups] == [
        ["了", "了"],
        ["瞭"],
        ["和"],
    ]
    page, total = db.page_groups(rows, 0, 2)
    assert total == 3
    assert [row["simplified"] for row in page] == ["了", "了", "瞭"]


def test_dungan_reading_keeps_latin_and_tones():
    """A Dungan reading drops the Cyrillic and keeps the Latin + tone numerals."""
    assert (
        db._clean_dungan_reading("җигуанзыхуар (jiguanzɨhuar, I-I-I-I)")
        == "jiguanzɨhuar (I-I-I-I)"
    )
    assert db._clean_dungan_reading("билүн (bilün, II-III)") == "bilün (II-III)"
    # No Latin parenthetical: leave the source spelling alone.
    assert db._clean_dungan_reading("җигуанзыхуар") == "җигуанзыхуар"


def test_joined_reading_for_a_split_phrase():
    """A 歇后语 joins both halves; a polyphonic word keeps its first reading."""
    assert db._joined_reading(
        "猪八戒吃人参果——全不知滋味",
        ["Zhū Bā jiè chī rén shēn guǒ", "quán bù zhī zī wèi"],
    ) == "Zhū Bā jiè chī rén shēn guǒ quán bù zhī zī wèi"
    # No separator: this is a polyphonic word, not two halves.
    assert db._joined_reading("行", ["xíng", "háng"]) is None
    # A separator whose parts do not match the readings is left alone.
    assert db._joined_reading("甲——乙——丙", ["jiǎ", "yǐ"]) is None


def test_see_target_reads_qualified_variant_notes():
    """"old variant of X" is a form link, not a definition."""
    assert see_target("old variant of 柿[shi4]") == ("柿", "柿", "old")
    assert see_target("erhua variant of 一個勁|一个劲[yi1 ge4 jin4]") == (
        "一個勁",
        "一个劲",
        "erhua",
    )
    assert see_target("(literary) variant of 倘[tang3]") == ("倘", "倘", "literary")
    assert see_target("variant of 逼格[bi1 ge2]") == ("逼格", "逼格", "")
    # An English sense that merely uses the word "see" is not a form link.
    assert see_target("to see incidentally") is None
    assert see_target("as you see fit") is None
    # A sense that mentions a variant in passing stays a sense.
    assert see_target("crispy, deep-fried (variant of 卡拉[ka3 la1])") is None


def test_opencc_converts_between_scripts():
    """Simplified and Traditional conversion handles whole-word differences."""
    assert db.to_simplified("頭髮") == "头发"
    assert db.to_traditional("干净") == "乾淨"


def test_to_toneless_removes_tones_spaces_and_case():
    """Search keys are lowercase, unspaced and tone-insensitive."""
    assert to_toneless("Shì Shí") == "shishi"
    assert to_toneless("han4 yu3") == "hanyu"


def test_numeric_and_marked_pinyin_round_trip():
    """Numeric pinyin and tone-marked pinyin convert in both directions."""
    assert numeric_to_marked("han4 yu3") == "hàn yǔ"
    assert to_numeric("hàn yǔ") == "han4 yu3"


def test_pinyin_prefix_entries_reads_one_dictionary():
    """A pinyin preview can read MOE instead of always CC-CEDICT."""
    entries = db.pinyin_prefix_entries("xue4", "moe", 5)
    assert entries
    assert all(entry.get("pinyin") for entry in entries)
    assert all(entry.get("traditional") for entry in entries)


def test_glob_match():
    """'*' matches any run, '?' exactly one character, over the whole word."""
    assert db._glob_match("后", "后")
    assert not db._glob_match("?后", "后")
    assert db._glob_match("?后", "以后")
    assert db._glob_match("*后", "以后")
    assert db._glob_match("*后*", "售后服务")
    assert not db._glob_match("*后", "售后服务")
    assert db._glob_match("后*", "后")
    assert db._glob_match("?怒??", "喜怒哀乐")


def test_wildcard_tier():
    """Chinese patterns rank exact, then starts-with, then contains."""
    assert db._wildcard_tier(["以后"], "?后") == 1
    assert db._wildcard_tier(["售后服务"], "?后") == 2
    assert db._wildcard_tier(["对以后"], "?后") == 3
    assert db._wildcard_tier(["后"], "?后") is None
    assert db._wildcard_tier(["后"], "*后") == 1
    assert db._wildcard_tier(["对以后"], "*后") == 1
    assert db._wildcard_tier(["售后服务"], "*后") == 2
    assert db._wildcard_tier(["对以后再说"], "*后") == 2


def test_is_wildcard_query():
    """Only unquoted '*' / '?' queries count as wildcards."""
    assert db._is_wildcard_query("?后")
    assert db._is_wildcard_query("＊后")
    assert not db._is_wildcard_query('"?后"')
    assert not db._is_wildcard_query("后")


def test_rank_by_category_keeps_selected_dictionary_first():
    """Category 1 rows stay ahead of category 2 rows across the whole list."""
    entries, _ = db.search("gan", 100)
    ranked = db.rank_by_category(entries, {"cedict"})
    seen_other = False
    for entry in ranked:
        is_selected = bool(entry.get("english"))
        if is_selected and seen_other:
            pytest.fail("a category 1 row followed a category 2 row")
        if not is_selected:
            seen_other = True


def test_wiktionary_alias_gloss_counts_as_defined():
    """Wiktionary can store a gloss under another traditional spelling."""
    defined = db._anchor_defined_words("wiktionary", ["党群", "㼕"])
    assert "党群" in defined
    assert "㼕" not in defined


def test_rank_by_category_uses_real_wiktionary_glosses():
    """A gloss-only word must not sink behind a definition-less exact match."""
    rows = [
        {"simplified": "㼕", "traditional": "㼕", "pinyin": "dāng", "english": ""},
        {
            "simplified": "党群",
            "traditional": "黨羣",
            "pinyin": "dǎng qún",
            "english": "",
        },
    ]
    ranked = db.rank_by_category(rows, {"wiktionary"}, "dang", False)
    assert [entry["simplified"] for entry in ranked] == ["党群", "㼕"]


def test_reading_key_is_tone_aware():
    """Readings that differ only by tone stay distinct."""
    assert db.reading_key("zhī") == db.reading_key("zhi1") == "zhi1"
    assert db.reading_key("zhǐ") == "zhi3"
    assert db.reading_key("") == ""


def test_reading_counts_keep_surname_capitalisation():
    """Gān (surname) and gān (dry) are separate readings, not one."""
    counts = db._cedict_reading_counts(["干"])
    by_reading = counts["干"]
    assert by_reading["gān"] > by_reading["Gān"] > 0


def test_entry_sense_count_uses_the_key_that_has_the_reading():
    """A traditional key with other readings must not score this reading as 0."""
    counts = {"乾": {"qián": 4}, "干": {"gān": 12, "Gān": 2}}
    assert (
        db._entry_sense_count(
            counts, {"traditional": "乾", "simplified": "干", "pinyin": "gān"}
        )
        == 12
    )
    assert (
        db._entry_sense_count(
            counts, {"traditional": "乾", "simplified": "干", "pinyin": "Gān"}
        )
        == 2
    )


def test_sense_count_for_prefers_the_exact_reading():
    """Case decides which group a reading's senses come from."""
    by_reading = {"gān": 12, "Gān": 2}
    assert db.sense_count_for(by_reading, "gān") == 12
    assert db.sense_count_for(by_reading, "Gān") == 2
    # Numeric and marked spellings of the same tone still match.
    assert db.sense_count_for({"gan1": 3}, "gān") == 3


def test_entry_sense_count_prefers_exact_case_across_both_keys():
    """An exact reading in the other key beats a case variant in this one."""
    counts = {"乾": {"Gān": 1}, "干": {"gān": 12}}
    assert (
        db._entry_sense_count(
            counts, {"traditional": "乾", "simplified": "干", "pinyin": "gān"}
        )
        == 12
    )
    assert (
        db._entry_sense_count(
            counts, {"traditional": "乾", "simplified": "干", "pinyin": "Gān"}
        )
        == 1
    )


def test_light_decorate_scores_each_reading_from_its_own_key():
    """The light path sees the 12 gān senses, not the surname's 2."""
    entries = [
        {"traditional": "乾", "simplified": "干", "pinyin": "Gān"},
        {"traditional": "乾", "simplified": "干", "pinyin": "gān"},
        {"traditional": "乾", "simplified": "乾", "pinyin": "qián"},
    ]
    db._light_decorate(entries)
    scores = {entry["pinyin"]: entry["_reading_count"] for entry in entries}
    assert scores["gān"] > scores["Gān"] > 0
    assert scores["qián"] > 0


def test_xref_senses_are_not_counted_as_definitions():
    """Variant pointers are lookup hints, not meanings."""
    assert db.is_xref_sense("variant of 隻|只[zhi1]")
    assert db.is_xref_sense("old variant of 柿[shi4]")
    assert db.is_xref_sense("also written 沒|没[mei2]")
    assert not db.is_xref_sense("to dry")


def test_owned_by_entry_splits_semicolon_and_slash_rows():
    """A shared CEDICT pool is split using the spelling's own english text."""
    senses = [
        ("zhī", "grain that has begun to ripen"),
        ("zhī", "classifier for birds and certain animals"),
    ]
    classifier = {"english": "classifier for birds and certain animals"}
    assert db._owned_by_entry(senses, classifier) == [senses[1]]

    slash = {
        "english": "grain that has begun to ripen"
        "/classifier for birds and certain animals"
    }
    assert db._owned_by_entry(senses, slash) == senses

    semicolon = {
        "english": "grain that has begun to ripen"
        "; classifier for birds and certain animals"
    }
    assert db._owned_by_entry(senses, semicolon) == senses

    # A format surprise keeps the whole pool instead of hiding a definition.
    assert db._owned_by_entry(senses, {"english": "something else"}) == senses


def test_like_pattern_turns_star_into_sql_wildcard():
    """The app wildcard becomes SQL %, while literal % and _ are escaped."""
    assert db._like_pattern("a*b") == "a%b"
    assert db._like_pattern("%_") == "\\%\\_"


def test_has_cjk_recognizes_han_and_rejects_latin():
    """Only Han characters count as Chinese for headword matching."""
    assert db.has_cjk("档案")
    assert db.has_cjk("字1")
    assert not db.has_cjk("dangan")


def test_preferred_reading_for_mei():
    """没 defaults to méi for parsed single characters, despite mò's senses."""
    assert db.lookup("没")[0]["pinyin"] == "méi"
    assert db.lookup("沒")[0]["pinyin"] == "méi"


def test_sense_counts_match_the_live_queries(monkeypatch):
    """The precomputed counts produce the same lookup order and counts."""
    if not db.SENSE_COUNTS_DB_PATH.exists():
        pytest.skip("sense_counts.sqlite not built")
    words = ["乾", "干", "只", "隻", "了", "瞭", "没", "沒", "檔案"]
    try:
        db._sense_counts_ok_cache = None
        db._lookup_cache.clear()
        precomputed = {word: db.lookup(word) for word in words}
        precomputed_counts = db._cedict_reading_counts(words)

        monkeypatch.setenv("YABAI_SENSE_COUNTS", "0")
        db._sense_counts_ok_cache = None
        db._lookup_cache.clear()
        live = {word: db.lookup(word) for word in words}
        live_counts = db._cedict_reading_counts(words)

        assert precomputed == live
        assert precomputed_counts == live_counts
    finally:
        db._sense_counts_ok_cache = None
        db._lookup_cache.clear()


def test_sense_counts_fall_back_when_the_file_is_missing(monkeypatch, tmp_path):
    """A missing sense_counts.sqlite must not break lookup."""
    monkeypatch.setattr(db, "SENSE_COUNTS_DB_PATH", tmp_path / "missing.sqlite")
    db._sense_counts_ok_cache = None
    try:
        assert db._sense_counts_ok() is False
        assert db.lookup("没")
    finally:
        db._sense_counts_ok_cache = None


def test_non_standard_pinyin_falls_through_to_definitions():
    """A Latin word spelled out as pinyin must not hijack an English query."""
    entries, pinyin_match = db.search("word", limit=200, part="all", hydrate=0)
    assert not pinyin_match
    words = {entry["simplified"] for entry in entries}
    assert "词汇" in words
    # An unquoted Latin query also matches Latin text inside a headword or a
    # reading, and those tier-0 rows come first.
    assert entries[0]["simplified"] == "word姐"
    assert entries[0]["tier"] == 1
    assert entries[0]["category"] == 1
    # A real pinyin query still takes the pinyin path.
    _entries, pinyin_match = db.search("shan", limit=10, part="all", hydrate=0)
    assert pinyin_match


def test_latin_reading_run_counts_as_tier_zero():
    """A headword that only spells the query out in its reading still matches."""
    entries, pinyin_match = db.search("cos", limit=10, part="all", hydrate=0)
    assert not pinyin_match
    assert entries[0]["simplified"] == "cos"
    assert entries[0]["tier"] == 1


def test_headword_reading_with_more_definitions_comes_first():
    '''A headword's main reading is the one with the most senses.'''
    entries, _ = db.search("德", limit=20, part="all", hydrate=0)
    de_entries = [entry for entry in entries if entry["simplified"] == "德"]
    assert de_entries and de_entries[0]["pinyin"] == "dé"

    entries, _ = db.search("Dé", limit=20, part="all", hydrate=0)
    de_entries = [entry for entry in entries if entry["simplified"] == "德"]
    assert de_entries and de_entries[0]["pinyin"] == "dé"

    assert db.lookup("德")[0]["pinyin"] == "dé"
    assert db.lookup_reading("德") == [{"pinyin": "dé"}]


def test_quoted_latin_query_stays_definition_only():
    """Quoting is an explicit phrase search; no headword/reading T0 rows."""
    entries, pinyin_match = db.search('"word"', limit=100, part="all", hydrate=0)
    assert not pinyin_match
    assert "word姐" not in {entry["simplified"] for entry in entries}
    assert all(entry.get("tier") != 0 for entry in entries)


def test_lookup_many_matches_lookup_for_each_word():
    """The batched path returns the same rows, in the same order."""
    words = ["没", "沒", "檔案", "了", "乾", "干", "只", "隻"]
    batched = db.lookup_many(words)
    for word in words:
        assert batched[word] == db.lookup(word)


def test_lookup_many_uses_one_query_for_a_batch(monkeypatch):
    """Five or fifty targets cost one lookup statement, not one each."""

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConnection:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append(sql)
            return FakeCursor()

        def close(self):
            pass

    connection = FakeConnection()
    monkeypatch.setattr(db, "connect", lambda: connection)
    for words in (
        ["没", "檔案", "了", "乾", "干"],
        [f"missing{i}" for i in range(50)],
    ):
        connection.calls.clear()
        db._lookup_uncached(words)
        assert (
            sum("FROM entries WHERE simplified IN" in sql for sql in connection.calls)
            == 1
        )


def test_expand_variants_batch_matches_per_target(monkeypatch):
    """Batching the redirect lookups does not change the expanded list."""
    from yabai_search import services as app_main

    cases = {}
    for query in ("gan", "tian", "shan"):
        results, _ = db.search(query, limit=100, part="all")
        cases[query] = app_main._expand_variants([dict(entry) for entry in results])
    monkeypatch.setattr(
        db,
        "lookup_many",
        lambda words: {word: db.lookup(word) for word in words},
    )
    for query in ("gan", "tian", "shan"):
        results, _ = db.search(query, limit=100, part="all")
        got = app_main._expand_variants([dict(entry) for entry in results])
        assert got == cases[query]


def test_parse_pinyin_query_reads_one_tone():
    """A tone digit marks both its tone and a syllable boundary."""
    assert db._parse_pinyin_query("shi4shi") == (
        "shishi",
        {3: "4"},
        {3},
    )


def test_parse_pinyin_query_reads_two_tones():
    """Tones on both syllables are both recorded."""
    assert db._parse_pinyin_query("shi4shi4") == (
        "shishi",
        {3: "4", 6: "4"},
        {3, 6},
    )


def test_parse_pinyin_query_rejects_toneless_input():
    """Plain pinyin takes a different, tone-insensitive search path."""
    assert db._parse_pinyin_query("dangan") is None


def test_parse_pinyin_query_treats_apostrophe_as_boundary():
    """An apostrophe pins a syllable break even when a tone is present."""
    assert db._parse_pinyin_query("dan4'gan") == ("dangan", {3: "4"}, {3})


def test_reading_rank_exact_prefix_and_contains():
    """Readings rank exact first, then prefix, then contains."""
    assert db._reading_rank("dang4 an4", "dangan", {}, set()) == 0
    assert db._reading_rank("dang4 an4 guan3", "dangan", {}, set()) == 1
    assert db._reading_rank("x dang4 an4", "dangan", {}, set()) == 2


def test_reading_rank_rejects_wrong_tone():
    """A specified tone must match the reading's tone."""
    assert db._reading_rank("dan1 gan4", "dangan", {3: "4"}, {3}) is None


def test_reading_rank_rejects_boundary_inside_syllable():
    """dan'gan cannot match 档案's dang'an split."""
    assert db._reading_rank("dang4 an4", "dangan", {}, {3}) is None


def test_reading_rank_accepts_boundary_at_syllable_split():
    """A boundary at the real syllable break matches normally."""
    assert db._reading_rank("dan1 gan4", "dangan", {}, {3}) == 0


def test_tonal_variant_returns_toneless_letters():
    """Tonal and Bopomofo queries produce their tone-insensitive variant."""
    assert db.tonal_variant("shi4shi") == "shishi"
    assert db.tonal_variant("ㄉㄢㄍㄢˋ") == "dangan"
    assert db.tonal_variant("dangan") is None


@pytest.mark.skipif(
    not (DATA / "moe.sqlite").exists() and not (DATA / "moe_revised.sqlite").exists(),
    reason="MOE databases not built",
)
def test_zhuyin_converts_to_numeric_pinyin():
    """Bopomofo is converted to spaced numeric pinyin before matching."""
    assert db._zhuyin_to_numeric("ㄉㄢㄍㄢˋ") == "dan1 gan4"
    assert db._zhuyin_to_numeric("ㄉㄤˋㄢˋ") == "dang4 an4"


def test_normalize_english_expands_cedict_abbreviations():
    """CEDICT shorthand is expanded before definitions are indexed."""
    assert (
        normalize_english("sb sth abbr. onom.")
        == "somebody something abbreviation onomatopoeia"
    )


def test_simple_form_rejects_sql_and_fts_wildcards():
    """Only plain forms may use the trigram fast path."""
    assert db._simple_form("档案")
    assert not db._simple_form("档*")
    assert not db._simple_form("a_b")
    assert not db._simple_form("a%b")


def test_default_dictionaries_are_registered_anchors():
    """Every language default points at a real source."""
    anchors = {source["anchor"] for source in db.dictionaries()}
    assert set(DEFAULTS) == {"EN", "ZH", "TW", "JP"}
    for values in DEFAULTS.values():
        assert set(values) <= anchors
    assert "moe_revised" in DEFAULTS["TW"]


def test_has_english_match_checks_definition_words():
    """The English hint appears only when the phrase exists in a definition."""
    assert db.has_english_match("country")
    assert not db.has_english_match("qqzzxxnotaword")


@pytest.mark.skipif(
    not db.FREQ_DB_PATH.exists(), reason="frequency database not built"
)
def test_frequency_source_metadata():
    """The frequency source reports its anchor, count and update date."""
    source = db.frequency_source()
    assert source is not None
    assert source["anchor"] == db.FREQ_ANCHOR
    assert source["entries"] > 0


@pytest.mark.skipif(
    not (DATA / "hsk.sqlite").exists(), reason="HSK database not built"
)
def test_hsk_source_metadata():
    """The HSK source reports its anchor and roughly 11k vocabulary entries."""
    source = db.hsk_source()
    assert source is not None
    assert source["anchor"] == "hsk"
    assert source["entries"] > 10_000


def test_report_search_url_carries_display_settings():
    """A reported word links back to a search with the reporter's settings."""
    report = {
        "word": "過激",
        "settings": {
            "script": "traditional",
            "toneColoring": True,
            "phonetic": "zhuyin",
            "taiwan": False,
            "customDicts": True,
            "compact": True,
            "lang": "TW",
            "selectedDictionaries": ["cedict", "moe", "moe_revised"],
        },
    }
    url = _report_search_url(report)
    params = parse_qs(urlparse(url).query)
    assert params["q"] == ["過激"]
    assert params["script"] == ["traditional"]
    assert params["tone"] == ["1"]
    assert params["phonetic"] == ["zhuyin"]
    assert params["taiwan"] == ["0"]
    assert params["custom"] == ["1"]
    assert params["compact"] == ["1"]
    assert params["lang"] == ["TW"]
    assert params["dicts"] == ["cedict,moe,moe_revised"]


def test_report_search_url_reads_old_logs():
    """Older reports have no settings field; their debug log is the fallback."""
    log = "Yabai Search debug log\n" + json.dumps(
        {
            "currentState": {
                "script": "simplified",
                "toneColoring": False,
                "phonetic": "pinyin",
                "taiwan": True,
                "customDicts": False,
                "compact": False,
                "lang": "ZH",
                "selectedDictionaries": ["cedict"],
            }
        }
    ) + "\n"
    params = parse_qs(urlparse(_report_search_url({"word": "过", "log": log})).query)
    assert params["script"] == ["simplified"]
    assert params["tone"] == ["0"]
    assert params["taiwan"] == ["1"]
    assert params["lang"] == ["ZH"]


def test_report_settings_ignore_unknown_and_oversized_values():
    """Only known settings and bounded values are stored."""
    cleaned = _clean_report_settings(
        {
            "lang": "TW",
            "script": "x" * 200,
            "selectedDictionaries": [f"anchor-{i}" for i in range(150)],
            "unknown": "ignored",
        }
    )
    assert cleaned["lang"] == "TW"
    assert len(cleaned["script"]) == 80
    assert len(cleaned["selectedDictionaries"]) == 100
    assert "unknown" not in cleaned
