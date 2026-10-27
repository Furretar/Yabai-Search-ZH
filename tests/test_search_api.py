"""End-to-end tests for the /api/search result contract and ranking."""

from __future__ import annotations

from pathlib import Path

import pytest

from yabai_search.pinyin import to_toneless

DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "cedict.sqlite").exists(), reason="CEDICT database not built"
)


def _words(data):
    return [entry["simplified"] for entry in data["results"]]


def test_star_lists_every_headword_in_pages(search):
    """A bare * lists every headword, 25 per page, without overlap."""
    first = search("*")
    second = search("*", offset=25)
    assert first["total"] > 200_000
    assert len(first["results"]) == 25
    assert first["page_size"] == 25
    assert first["pinyin_match"] is False
    assert set(_words(first)).isdisjoint(set(_words(second)))


def test_blank_query_lists_all_headwords(search):
    """The API treats a blank query like * (the home page does not call this)."""
    data = search("")
    assert data["total"] > 200_000
    assert len(data["results"]) == 25


def test_quoted_empty_phrase_returns_nothing(search):
    """A quoted empty string is a real empty phrase, not an all-headword list."""
    data = search('""')
    assert data["results"] == []
    assert data["total"] == 0


def test_chinese_exact_match_is_first(search):
    """An exact Chinese headword is the first result and is not a pinyin match."""
    data = search("档案")
    assert data["results"][0]["simplified"] == "档案"
    assert data["pinyin_match"] is False


def test_chinese_results_rank_exact_prefix_contains(search):
    """CEDICT matches are ordered exact, then prefix, then contains.

    Source-only matches are appended after the CEDICT tier, so this checks the
    ranking within the CEDICT rows rather than across every dictionary.
    """
    data = search("垃圾")
    words = _words(data)
    cedict_words = [
        entry["simplified"]
        for entry in data["results"]
        if any(source["anchor"] == "cedict" for source in entry["sources"])
    ]
    ranks = []
    for word in cedict_words:
        if word == "垃圾":
            ranks.append(0)
        elif word.startswith("垃圾"):
            ranks.append(1)
        elif "垃圾" in word:
            ranks.append(2)
        else:
            ranks.append(3)
    assert ranks == sorted(ranks)
    assert words[0] == "垃圾"
    assert "倒垃圾" in words


def test_headword_matches_hide_definition_only_matches(search):
    """When headwords match, definition-only matches are filtered out."""
    data = search("垃圾")
    assert all(
        "垃圾" in entry["simplified"] or "垃圾" in entry["traditional"]
        for entry in data["results"]
    )


def test_english_definition_search(search):
    """English queries search definitions and rank common words first."""
    assert search("trash")["results"][0]["simplified"] == "垃圾"
    assert search("country")["results"][0]["simplified"] == "国家"


def test_quoted_english_phrase_search(search):
    """Quoted English uses the whole-word FTS phrase search."""
    data = search('"country"')
    assert data["results"][0]["simplified"] == "国家"
    assert data["pinyin_match"] is False
    assert any(
        "country" in sense
        for entry in data["results"]
        for definition in entry.get("definitions", [])
        for sense in definition.get("defs", [])
    )


def test_quoted_english_phrase_finds_wiktionary_gloss(search, find_result):
    """A Wiktionary-only gloss hit must survive the CEDICT-heavy phrase list.

    The phrase is shared by 79 Taixuanjing glosses; 䎡 is a source-only word
    near the end. The old limit*3 source scan dropped it before deduplication.
    """
    data = search('"tetragram of the"')
    found = None
    for offset in range(0, data["total"], data["page_size"]):
        page = search('"tetragram of the"', offset=offset)
        found = find_result(page, "䎡")
        if found is not None:
            break
    assert found is not None
    assert any(
        "17th tetragram of the" in sense
        for definition in found.get("definitions", [])
        for sense in definition.get("defs", [])
    )


def test_unquoted_english_phrase_finds_wiktionary_gloss(search, find_result):
    """The same source-only word is returned by the unquoted definition path."""
    data = search("tetragram of the")
    assert find_result(data, "䎡") is not None


def test_quoted_chinese_definition_phrase(search):
    """A quoted Chinese phrase finds the headword it defines."""
    data = search('"一種中藥。"')
    assert data["results"][0]["simplified"] == "龙骨"
    assert any(
        definition["anchor"] == "moe"
        and any("一種中藥。" in sense for sense in definition.get("defs", []))
        for entry in data["results"]
        for definition in entry.get("definitions", [])
    )


def test_quoted_chinese_headword_searches_definitions(search, find_result):
    """A quoted Chinese headword searches definitions rather than the headword."""
    data = search('"档案"')
    entry = find_result(data, "隔音符号")
    assert entry is not None
    assert any(
        "档案" in sense
        for definition in entry.get("definitions", [])
        for sense in definition.get("defs", [])
    )


def test_unquoted_chinese_definition_search(search):
    """A Chinese phrase with no headword match falls through to definitions."""
    data = search("一種中藥。")
    assert data["results"][0]["simplified"] == "龙骨"


def test_limit_validation(client, search):
    """The API rejects limits outside 1..25 and honours a valid limit."""
    assert client.get("/api/search", params={"q": "档案", "limit": 0}).status_code == 422
    assert client.get("/api/search", params={"q": "档案", "limit": 26}).status_code == 422
    assert len(search("档案", limit=1)["results"]) == 1


def test_offset_paginates_normal_search(search):
    """Offset slices a normal search without changing its total."""
    full = search("档案")
    page0 = search("档案", limit=5, offset=0)
    page1 = search("档案", limit=5, offset=5)
    assert _words(page0) == _words(full)[:5]
    assert page0["total"] == full["total"] == page1["total"]
    assert set(_words(page0)).isdisjoint(set(_words(page1)))


def test_fuzzy_results_are_paged_after_official_results(search):
    """Fuzzy rows wait until the last pages and never inflate a page past 25."""
    first = search("shi4")
    assert first["results"]
    assert first["fuzzy"] == []
    total = first["total"]
    assert total > len(first["results"])

    second = search("shi4", offset=25)
    assert second["fuzzy"] == []
    assert len(second["results"]) <= 25

    last_offset = ((total - 1) // 25) * 25
    last = search("shi4", offset=last_offset)
    assert last["offset"] == last_offset
    assert last["total"] == total
    assert last["fuzzy"]
    assert len(last["results"]) + len(last["fuzzy"]) <= 25


def test_old_variant_is_an_other_form_not_a_result(search):
    """柹 is merged into 柿 as an "other form" instead of a duplicate row."""
    data = search("柿")
    shi = [entry for entry in data["results"] if entry["simplified"] == "柿"]
    assert shi
    forms = shi[0].get("forms") or []
    zhi = [form for form in forms if form["traditional"] == "柹"]
    assert zhi
    assert zhi[0]["qualifier"] == "old"
    assert not any(entry["traditional"] == "柹" for entry in data["results"])


def test_traditional_variant_query_redirects_to_main(search):
    """A traditional-only variant (柹) is found and shown as its main form."""
    data = search("柹")
    assert data["total"] >= 1
    shi = [entry for entry in data["results"] if entry["simplified"] == "柿"]
    assert shi
    assert any(form["traditional"] == "柹" for form in shi[0].get("forms", []))


def test_offset_paginates_wildcard_search(search):
    """Wildcard pages use the fixed 25-result page size, with stable totals."""
    page0 = search("*字", limit=10, offset=0)
    page1 = search("*字", limit=10, offset=25)
    assert page0["total"] == page1["total"]
    assert len(page0["results"]) == 25
    # A raw page can lose a row when two script forms collapse to one headword,
    # and a collapsed headword can repeat once at a page boundary.
    assert 0 < len(page1["results"]) <= 25
    assert set(_words(page1)) - set(_words(page0))
    assert all(word.endswith("字") for word in _words(page0))


def test_part_primary_is_cedict_only_and_a_subset(search):
    """The primary tier is the fast CEDICT-only subset of the full result list."""
    full = search("dan'gan")
    primary = search("dan'gan", part="primary")
    assert primary["pinyin_match"] is True
    assert len(primary["results"]) < len(full["results"])
    assert set(_words(primary)) <= set(_words(full))


def test_pinyin_match_flag(search):
    """The pinyin flag is true only for reading-based searches."""
    assert search("dangan")["pinyin_match"] is True
    assert search("档案")["pinyin_match"] is False
    assert search("country")["pinyin_match"] is False
    assert search("*字")["pinyin_match"] is False
    assert search('"country"')["pinyin_match"] is False


def test_english_match_flag_for_pinyin_query(search):
    """A pinyin search also reports when English definitions match."""
    assert search("kou")["english_match"] is True
    assert search("dangan")["english_match"] is False


def test_proper_noun_rows_collapse(search):
    """Surname and normal readings of 阮 collapse into one row with both groups."""
    data = search("阮")
    rows = [entry for entry in data["results"] if entry["simplified"] == "阮"]
    assert len(rows) == 1
    groups = [
        group["pinyin"]
        for definition in rows[0]["definitions"]
        if definition["anchor"] == "cedict"
        for group in definition["groups"]
    ]
    assert "Ruǎn" in groups
    assert "ruǎn" in groups


def test_traditional_and_simplified_queries_match_same_headword(search):
    """A traditional and a simplified query reach the same entry."""
    for query in ("马", "馬"):
        first = search(query)["results"][0]
        assert first["traditional"] == "馬"
        assert first["simplified"] == "马"


def test_response_shape(search):
    """The API returns the fields the frontend relies on."""
    data = search("档案", limit=1)
    assert set(data) == {
        "query",
        "pinyin_match",
        "english_match",
        "results",
        "fuzzy",
        "total",
        "offset",
        "page_size",
        "ms",
    }
    entry = data["results"][0]
    for key in (
        "traditional",
        "simplified",
        "pinyin",
        "definitions",
        "sources",
        "freq",
        "hsk",
    ):
        assert key in entry


def test_numeric_query_ranks_numeric_headwords_first(search):
    """A number query shows headwords containing it before definition hits."""
    words = _words(search("21"))
    assert "21三体综合症" in words
    assert "421" in words
    # Both numeric headwords must come before ordinary words whose definitions
    # merely mention 21 (e.g. 甲申's "21st year").
    assert words.index("21三体综合症") < 3
    assert words.index("421") < 3


def test_language_query_shows_language_and_note_entries(search):
    """The language entry and note-only entries are on the first page."""
    words = _words(search("cantonese"))
    assert words[0] == "粤语"
    # 唔 only has a "(Cantonese)" note; it must still be visible early.
    assert "唔" in words[:10]


def test_shanghainese_ranks_language_entry_first(search):
    """The dictionary entries for the language itself come first."""
    words = _words(search("shanghainese"))
    assert set(words[:2]) == {"沪语", "上海话"}
    assert "大拐" in words[:10]


def test_pinyin_language_query_skips_definition_matches():
    """sichuan matches readings, so dialect notes must not be searched."""
    from yabai_search import db

    entries, pinyin_match = db.search("sichuan", 50)
    assert pinyin_match is True
    words = [entry["simplified"] for entry in entries]
    assert "巴适" not in words
    assert all(
        to_toneless(entry["pinyin"]).startswith("sichuan") for entry in entries
    )


def test_xiang_reading_query_returns_only_readings():
    """xiang returns reading matches and no definition-only entries."""
    from yabai_search import db

    entries, pinyin_match = db.search("xiang", 50)
    assert pinyin_match is True
    assert entries
    assert all(
        to_toneless(entry["pinyin"]).startswith("xiang") for entry in entries
    )


def test_chinese_phrase_finds_exact_source_headword(search, find_result):
    """An exact source headword is returned with its gloss."""
    data = search("宁为太平犬，不做乱世人")
    entry = find_result(data, "宁为太平犬，不做乱世人")
    assert entry is not None
    assert any(
        "dog in times of tranquility" in sense
        for definition in entry["definitions"]
        for sense in definition.get("defs", [])
    )


def test_dialect_label_search_finds_labelled_words(search):
    """A query with no reading match may still search variety labels."""
    data = search("Sichuanese")
    labelled = [
        entry
        for entry in data["results"]
        if entry.get("pinyin_label") == "Chengdu Sichuanese"
    ]
    assert labelled
    assert any(
        any(source["anchor"] == "wiktionary" for source in entry["sources"])
        for entry in labelled
    )


def test_dialect_label_is_skipped_when_query_has_a_reading(search):
    """Chengdu matches a reading, so label/definition search is skipped."""
    data = search("Chengdu")
    assert data["pinyin_match"] is True
    assert not any(
        entry.get("pinyin_label") == "Chengdu Sichuanese"
        for entry in data["results"]
    )


def test_dialect_reading_pinyin_search(search, find_result):
    """A dialect reading's pinyin finds the labelled headword and definition."""
    data = search("jiangbaor")
    entry = find_result(data, "姜包儿")
    assert entry is not None
    assert entry.get("pinyin_label") == "Chengdu Sichuanese"
    assert any(
        "ginger" in sense
        for definition in entry["definitions"]
        for sense in definition.get("defs", [])
    )


def test_wu_reading_query_skips_definition_notes():
    """wu matches readings, so note-only entries like 侬 are not returned."""
    from yabai_search import db

    entries, pinyin_match = db.search("wu", 50)
    assert pinyin_match is True
    words = [entry["simplified"] for entry in entries]
    assert "侬" not in words
    assert all(to_toneless(entry["pinyin"]).startswith("wu") for entry in entries)


def test_unquoted_definition_fallback_when_no_headword_or_reading(search, find_result):
    """Definitions are still searched when there is no headword or reading."""
    data = search("trash")
    assert data["pinyin_match"] is False
    assert find_result(data, "垃圾") is not None


def test_concurrent_searches_use_separate_connections():
    """Parallel page prefetches must not share one SQLite connection.

    Connections became per-thread after concurrent /api/search calls hit
    "sqlite3.InterfaceError: bad parameter or other API misuse".
    """
    from concurrent.futures import ThreadPoolExecutor

    from yabai_search import db

    def run(_):
        entries, _pinyin_match = db.search("大", 25)
        return len(entries)

    with ThreadPoolExecutor(max_workers=6) as pool:
        counts = list(pool.map(run, range(12)))
    assert all(count > 0 for count in counts)
