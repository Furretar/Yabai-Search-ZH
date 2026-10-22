"""Tests for pinyin, Bopomofo, tones, fuzzy matching and wildcards."""

from __future__ import annotations

from pathlib import Path

import pytest

from yabai_search.pinyin import to_numeric, to_toneless

DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "cedict.sqlite").exists(), reason="CEDICT database not built"
)


def _words(data):
    return [entry["simplified"] for entry in data["results"]]


def test_toneless_pinyin_matches_both_syllable_splits(search, find_result):
    """dangan matches both dan'gan (单干) and dang'an (档案)."""
    data = search("dangan")
    assert data["pinyin_match"] is True
    assert find_result(data, "档案") is not None
    assert find_result(data, "单干") is not None


def test_apostrophe_pins_syllable_boundaries(search, find_result):
    """dan'gan finds 单干 but not 档案; dang'an finds 档案."""
    dan_gan = search("dan'gan")
    assert find_result(dan_gan, "单干") is not None
    assert find_result(dan_gan, "档案") is None
    dang_an = search("dang'an")
    assert find_result(dang_an, "档案") is not None


def test_tone_number_restricts_first_syllable(search):
    """shi4shi requires tone 4 on the first syllable but any tone on the second."""
    data = search("shi4shi")
    assert len(data["results"]) >= 5
    for entry in data["results"][:5]:
        assert to_numeric(entry["pinyin"]).split()[0] == "shi4"


def test_tone_numbers_restrict_both_syllables(search):
    """shi4shi4 requires tone 4 on both syllables."""
    data = search("shi4shi4")
    assert len(data["results"]) >= 4
    for entry in data["results"][:4]:
        assert to_numeric(entry["pinyin"]) == "shi4 shi4"


def test_tone_number_on_second_syllable(search):
    """shi2shi4 is a different tone pattern and finds 時事 first."""
    data = search("shi2shi4")
    assert data["results"][0]["simplified"] == "时事"
    assert to_numeric(data["results"][0]["pinyin"]) == "shi2 shi4"


def test_tone_five_matches_neutral_tone(search):
    """Tone 5 matches a neutral-tone syllable (试试)."""
    data = search("shi4shi5")
    assert data["results"][0]["simplified"] == "试试"
    assert to_numeric(data["results"][0]["pinyin"]) == "shi4 shi5"


def test_tonal_query_with_no_matches_offers_fuzzy(search):
    """A tonal query that finds nothing still offers tone-insensitive results."""
    data = search("dang4gan4")
    assert data["results"] == []
    assert data["total"] >= len(data["fuzzy"])
    assert data["total"] > 0
    assert data["fuzzy"]
    assert all("danggan" in to_toneless(entry["pinyin"]) for entry in data["fuzzy"])


def test_tonal_query_with_matches_also_offers_fuzzy(search):
    """Fuzzy rows are placed after the official rows, on the last page."""
    first = search("shi4shi")
    assert first["results"]
    assert first["fuzzy"] == []
    last_offset = ((first["total"] - 1) // 25) * 25
    last = search("shi4shi", offset=last_offset)
    assert last["total"] == first["total"]
    assert last["fuzzy"]
    assert len(last["results"]) + len(last["fuzzy"]) <= 25
    result_keys = {
        (entry["traditional"], entry["simplified"]) for entry in first["results"]
    }
    fuzzy_keys = {
        (entry["traditional"], entry["simplified"]) for entry in last["fuzzy"]
    }
    assert result_keys.isdisjoint(fuzzy_keys)


def test_tone_insensitive_fuzzy_contains_both_split_words(search, find_result):
    """dan4gan4 finds no tonal match but both 档案 and 单干 in fuzzy."""
    data = search("dan4gan4")
    assert data["results"] == []
    assert find_result(data, "档案", section="fuzzy") is not None
    assert find_result(data, "单干", section="fuzzy") is not None


def test_bopomofo_matches_tonal_pinyin(search):
    """Bopomofo is converted to numeric pinyin and follows the same ranking."""
    dan_gan = search("ㄉㄢㄍㄢˋ")
    assert dan_gan["pinyin_match"] is True
    assert dan_gan["results"][0]["simplified"] == "单干"
    dang_an = search("ㄉㄤˋㄢˋ")
    assert dang_an["results"][0]["simplified"] == "档案"


def test_bopomofo_spaces_do_not_change_results(search):
    """Whitespace between Bopomofo syllables is optional."""
    compact = search("ㄉㄢㄍㄢˋ")
    spaced = search("ㄉㄢ ㄍㄢˋ")
    assert compact["total"] == spaced["total"]
    assert _words(compact) == _words(spaced)


def test_pinyin_wildcard_prefix(search):
    """A latin wildcard matches toneless readings from the start."""
    data = search("shi*")
    assert data["pinyin_match"] is True
    assert data["results"]
    assert all(to_toneless(entry["pinyin"]).startswith("shi") for entry in data["results"])


def test_pinyin_wildcard_contains(search, find_result):
    """*xinu* matches readings containing xinu anywhere."""
    data = search("*xinu*")
    assert data["pinyin_match"] is True
    assert find_result(data, "喜怒哀乐") is not None
    assert all("xinu" in to_toneless(entry["pinyin"]) for entry in data["results"])


def test_chinese_wildcard_suffix(search):
    """*字 matches headwords ending with 字, exact 字 first."""
    data = search("*字")
    assert data["total"] > 50
    assert data["results"][0]["simplified"] == "字"
    assert all(entry["simplified"].endswith("字") for entry in data["results"])


def test_chinese_wildcard_prefix(search):
    """字* matches headwords starting with 字, exact 字 first."""
    data = search("字*")
    assert data["total"] > 50
    assert data["results"][0]["simplified"] == "字"
    assert all(entry["simplified"].startswith("字") for entry in data["results"])


def test_wildcard_handles_traditional_and_simplified_queries(search):
    """A traditional wildcard is converted to simplified before matching."""
    traditional = search("*檔")
    simplified = search("*档")
    assert traditional["total"] == simplified["total"]
    assert _words(traditional) == _words(simplified)
