"""Unit tests for the small helpers that drive search behavior."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from yabai_search import db
from yabai_search.default_dictionaries import DEFAULTS
from yabai_search.main import _clean_report_settings, _report_search_url
from yabai_search.overrides import normalize_english
from yabai_search.pinyin import numeric_to_marked, to_numeric, to_toneless
from scripts.import_cedict import see_target

DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "cedict.sqlite").exists(), reason="CEDICT database not built"
)


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
