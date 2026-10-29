"""Tests for the '?' wildcard: exactly one character or pinyin syllable."""

from __future__ import annotations

from pathlib import Path

import pytest

from yabai_search import db
from yabai_search.pinyin import to_toneless

DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "cedict.sqlite").exists(), reason="CEDICT database not built"
)


def _words(data):
    return [entry["simplified"] for entry in data["results"]]


def _wildcard_rows(query: str, limit: int = 1000):
    rows, total, pinyin_match = db.wildcard_search(query, limit, 0)
    return rows, total, pinyin_match


def _wildcard_words(query: str, limit: int = 1000):
    rows, total, pinyin_match = _wildcard_rows(query, limit)
    return {entry["simplified"] for entry in rows}, total, pinyin_match


def test_question_matches_exactly_one_han_character(search, find_result):
    """国家? matches 国家级 but not 国家 or the longer 国家级旅游度假区."""
    data = search("国家?", dicts="cedict,wiktionary")
    assert data["pinyin_match"] is False
    assert find_result(data, "国家级") is not None
    assert find_result(data, "国家队") is not None
    assert find_result(data, "国家") is None
    assert find_result(data, "国家级旅游度假区") is None
    from yabai_search import db
    tiers = [db._wildcard_tier([word], "国家?") for word in _words(data)]
    assert tiers == sorted(tiers)


def test_question_matches_one_character_at_the_start():
    """?家 is exactly two characters and ends in 家."""
    words, total, pinyin_match = _wildcard_words("?家")
    assert total > 0
    assert pinyin_match is False
    assert {"大家", "回家", "国家"} <= words
    assert "国家级" not in words
    assert all(len(word) == 2 and word.endswith("家") for word in words)


def test_question_matches_one_character_in_the_middle():
    """一?一 is exactly three characters, starting and ending with 一."""
    words, _, _ = _wildcard_words("一?一")
    assert {"一对一", "一百一", "一等一"} <= words
    assert all(
        len(word) == 3 and word.startswith("一") and word.endswith("一")
        for word in words
    )


def test_two_questions_match_two_characters():
    """国?? is exactly three characters starting with 国."""
    words, _, _ = _wildcard_words("国??")
    assert "国家级" in words
    assert "国家" not in words
    assert all(len(word) == 3 and word.startswith("国") for word in words)


def test_question_and_star_mix():
    """国家?* needs one extra character, then accepts any number more."""
    words, _, _ = _wildcard_words("国家?*")
    assert "国家级" in words
    assert "国家级旅游度假区" in words
    assert "国家" not in words


def test_star_still_matches_zero_or_more_characters():
    """The existing * wildcard is unchanged by the new ? wildcard."""
    words, _, pinyin_match = _wildcard_words("国家*")
    assert pinyin_match is False
    assert {"国家", "国家级", "国家级旅游度假区"} <= words


def test_question_is_script_insensitive(search):
    """A traditional query gets the same ? results as its simplified form."""
    simplified = search("国家?", dicts="cedict,wiktionary")
    traditional = search("國家?", dicts="cedict,wiktionary")
    assert traditional["total"] == simplified["total"]
    assert set(_words(traditional)) == set(_words(simplified))


def test_question_wildcard_pages(search):
    """? wildcard results use the normal 25-row pager with stable totals."""
    first = search("?家", dicts="cedict,wiktionary")
    second = search("?家", offset=25, dicts="cedict,wiktionary")
    assert first["total"] == second["total"] > 25
    assert first["page_size"] == 25
    # Script-form collapsing can remove a row from a raw 25-row page.
    assert 0 < len(first["results"]) <= 25
    assert second["results"]
    assert set(_words(first)) != set(_words(second))
    from yabai_search import db
    tiers = [db._wildcard_tier([word], "?家") for word in _words(first)]
    assert tiers == sorted(tiers)


def test_chinese_wildcard_pages_do_not_split_a_simplified_word(search):
    """Wildcard pages also page whole simplified-headword groups."""
    first = search("?家")
    second = search("?家", offset=25)
    first_words = {entry["simplified"] for entry in first["results"]}
    second_words = {entry["simplified"] for entry in second["results"]}
    assert first["total"] == second["total"] > 25
    assert first_words.isdisjoint(second_words)


def test_pinyin_question_matches_one_syllable(search, find_result):
    """guojia? is guo + jia + exactly one more syllable."""
    data = search("guojia?")
    assert data["pinyin_match"] is True
    assert find_result(data, "国家级") is not None
    assert find_result(data, "国家队") is not None
    assert find_result(data, "国家") is None
    assert find_result(data, "国家级旅游度假区") is None
    for entry in data["results"]:
        syllables = entry["pinyin"].split()
        assert len(syllables) == 3
        assert to_toneless(entry["pinyin"]).startswith("guojia")


def test_pinyin_question_at_the_start(search, find_result):
    """?jia is exactly two syllables ending in jia."""
    data = search("?jia")
    assert data["pinyin_match"] is True
    assert find_result(data, "国家") is not None
    assert find_result(data, "大家") is not None
    assert find_result(data, "国家级") is None
    for entry in data["results"]:
        syllables = entry["pinyin"].split()
        assert len(syllables) == 2
        assert to_toneless(syllables[-1]) == "jia"


def test_pinyin_question_in_the_middle(search, find_result):
    """guo?jia is exactly guo + one syllable + jia."""
    data = search("guo?jia")
    assert data["pinyin_match"] is True
    assert find_result(data, "过家家") is not None
    assert find_result(data, "国家级") is None
    for entry in data["results"]:
        syllables = entry["pinyin"].split()
        assert len(syllables) == 3
        assert to_toneless(syllables[0]) == "guo"
        assert to_toneless(syllables[-1]) == "jia"


def test_pinyin_question_requires_a_whole_syllable(search):
    """guoj? cannot match 国家: 'j' is only part of the syllable jia."""
    data = search("guoj?")
    assert data["pinyin_match"] is True
    assert data["results"] == []
    assert data["total"] == 0


def test_pinyin_question_ignores_tones(search):
    """Tone digits do not change which syllables ? matches."""
    plain = search("guojia?")
    toned = search("guo2jia1?")
    assert toned["total"] == plain["total"]
    assert set(_words(toned)) == set(_words(plain))


def test_pinyin_question_mixed_with_star(search):
    """shi?* is shi + one syllable + anything, so at least two syllables."""
    data = search("shi?*")
    assert data["pinyin_match"] is True
    assert data["total"] > 25
    for entry in data["results"]:
        syllables = entry["pinyin"].split()
        assert len(syllables) >= 2
        assert to_toneless(syllables[0]) == "shi"


def test_pinyin_question_with_star_before(search):
    """*?jia ends with one whole syllable then jia, so at least two syllables."""
    data = search("*?jia")
    assert data["pinyin_match"] is True
    assert data["results"]
    for entry in data["results"]:
        syllables = entry["pinyin"].split()
        assert len(syllables) >= 2
        assert to_toneless(syllables[-1]) == "jia"


def test_like_pattern_translates_question_to_one_character():
    """The SQL helper maps ? to _, while still escaping literal wildcards."""
    assert db._like_pattern("a?b") == "a_b"
    assert db._like_pattern("国家?") == "国家_"
    assert db._like_pattern("a*b?") == "a%b_"
    assert db._like_pattern("50%?") == "50\\%_"


def test_simple_form_rejects_question_wildcard():
    """A ? query must not use the trigram/LIKE fast paths meant for plain text."""
    assert db._simple_form("档案")
    assert not db._simple_form("档?")
