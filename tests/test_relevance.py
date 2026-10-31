"""Table-driven tests for the pure English relevance tiers."""

from __future__ import annotations

import random

from yabai_search import relevance


def entry(
    simplified: str,
    english: str,
    *,
    traditional: str | None = None,
    pinyin: str = "",
    rank: int | None = None,
) -> dict:
    return {
        "simplified": simplified,
        "traditional": traditional or simplified,
        "pinyin": pinyin,
        "english": english,
        "freq": None if rank is None else {"rank": rank},
    }


def test_normalization_handles_nested_unbalanced_and_leading_articles():
    assert relevance.normalize_gloss("(literary) to speak") == "speak"
    assert relevance.normalize_gloss("a (b (c) d) e") == "e"
    assert relevance.normalize_gloss("a (b c") == "a"
    assert relevance.normalize_gloss("one's") == "one's"
    assert relevance.normalize_gloss("ü") == "ü"
    assert relevance.normalize_gloss("the") == "the"
    assert relevance.normalize_gloss("to") == "to"
    assert relevance.normalize_gloss("to speak") == "speak"


def test_one_row_per_tier():
    query = "word"
    rows = {
        0: entry("word姐", "my big sister", pinyin="w o r d jiě"),
        1: entry("词", "word; statement", pinyin="cí"),
        2: entry("被", "the following word should be regarded", pinyin="bèi"),
        3: entry("下", "I (modest word in letter)", pinyin="xià"),
        4: entry("关键词", "keyword", pinyin="guān jiàn cí"),
        5: entry("詞", "variant of word", pinyin="cí"),
    }
    for tier, row in rows.items():
        features = relevance.match_entry(row, query)
        assert features is not None, row
        assert features.tier == tier, (tier, features, row)


def test_player_one_row_per_tier_without_a_headword_match():
    query = "word"
    assert relevance.match_features("word; statement", query).tier == 1
    assert relevance.match_features("the following word should be", query).tier == 2
    assert relevance.match_features("I (modest word in letter)", query).tier == 3
    assert relevance.match_features("keyword", query).tier == 4
    assert relevance.match_features("variant of word", query).tier == 5


def test_to_speak_and_empty_or_one_letter_guard():
    assert relevance.match_features("to speak", "to speak").tier == 1
    assert relevance.match_features("speak", "to speak").tier == 1
    assert relevance.match_features("word", "") is None
    assert relevance.match_features("a", "a").tier == 1
    assert relevance.match_features("cat", "a") is None


def test_shuffle_invariance():
    rows = [
        entry("词", "word; statement", rank=10),
        entry("被", "the following word should be regarded", rank=1),
        entry("下", "I (modest word in letter)", rank=5),
        entry("word姐", "my big sister", rank=999),
        entry("关键词", "keyword", rank=2),
    ]
    expected = [row["simplified"] for row in relevance.rank_entries(rows, "word")]
    for seed in range(10):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        assert [
            row["simplified"] for row in relevance.rank_entries(shuffled, "word")
        ] == expected


def test_tier_dominates_frequency():
    """A tier 0 row beats a tier 1 row even if its frequency rank is worse."""
    rows = [
        entry("词", "word", rank=1),
        entry("word姐", "my big sister", rank=999),
    ]
    ranked = relevance.rank_entries(rows, "word")
    assert [row["simplified"] for row in ranked] == ["word姐", "词"]


def test_capitalized_query_prefers_exact_whole_gloss():
    """A proper noun's canonical translation beats a polysemous word."""
    rows = [
        entry("法", "France; French; abbr. for 法國|法国", rank=526),
        entry("法国", "France", rank=1720),
    ]
    ranked = relevance.rank_entries(rows, "France")
    assert [row["simplified"] for row in ranked] == ["法国", "法"]
    # Lowercase keeps the frequency order.
    ranked = relevance.rank_entries(rows, "france")
    assert [row["simplified"] for row in ranked] == ["法", "法国"]
