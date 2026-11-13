"""End-to-end tests for the /api/search result contract and ranking."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from yabai_search.pinyin import to_toneless

DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "dictionary-databases" / "cedict.sqlite").exists(), reason="CEDICT database not built"
)

requires_zh_sources = pytest.mark.skipif(
    not (DATA / "dictionary-databases" / "baidu" / "baidu.sqlite").exists()
    or not (DATA / "dictionary-databases" / "zhwiktionary.sqlite").exists(),
    reason="Baidu and Chinese Wiktionary databases not built",
)


def _words(data):
    return [entry["simplified"] for entry in data["results"]]


def test_search_reports_client_lag_header(client):
    """The API reports how long the request took to reach the server."""
    sent = int(time.time() * 1000)
    response = client.get(
        "/api/search",
        params={"q": "gan", "limit": 1},
        headers={"X-Client-Sent": str(sent)},
    )
    assert response.status_code == 200
    lag = response.headers.get("x-yabai-lag")
    assert lag is not None
    assert int(lag) >= 0


def test_star_lists_every_headword_in_pages(search):
    """A bare * lists every headword, 25 per page, without overlap."""
    first = search("*")
    second = search("*", offset=25)
    assert first["total"] > 200_000
    assert len(first["results"]) == 25
    assert first["page_size"] == 25
    assert first["pinyin_match"] is False
    assert set(_words(first)).isdisjoint(set(_words(second)))


def _entry_tier(entry):
    """Mirror entryTier() in app.js for a bare "*" query."""
    if entry.get("pinyin_label") or entry.get("pinyin_no_tone"):
        return 4
    has_def = any(
        definition.get("defs")
        or any(group.get("senses") for group in definition.get("groups") or [])
        for definition in entry.get("definitions") or []
    )
    if has_def:
        return 1
    return 5 if entry.get("pinyin") else 6


def test_star_lists_headwords_by_tier_then_frequency(search):
    """A bare * leaves tiers 1-3 moot but still demotes tiers 4-6.

    Every ordinary result is tier 1; a word with no definition is tier 5 or 6
    and must come after every defined word. Inside a tier, frequency ranks
    never go backwards and unranked words come last.
    """
    data = search("*")
    tiers = [_entry_tier(entry) for entry in data["results"]]
    assert tiers == sorted(tiers), tiers
    ranks_by_tier = {}
    for entry, tier in zip(data["results"], tiers):
        rank = (entry.get("freq") or {}).get("rank")
        ranks_by_tier.setdefault(tier, []).append(rank)
    for tier, ranks in ranks_by_tier.items():
        ranked = [rank for rank in ranks if rank is not None]
        assert ranked == sorted(ranked), (tier, ranks)
        assert ranks[: len(ranked)] == ranked, (tier, ranks)

    # The cached server list must be tier-ordered across the whole list, not
    # just inside one page. (The browser re-sorts each page; the server has to
    # put tier 1 first so later pages cannot jump ahead.)
    from yabai_search import db

    groups = db.all_headword_groups()
    assert groups == sorted(groups), "server groups are not tier-first"
    assert set(groups) <= {1, 4, 5, 6}, set(groups)


def test_server_merge_returns_one_row_per_simplified_word(search):
    """With merge=1 the API sends merged rows and both layouts."""
    data = search("le", merge=1)
    words = [entry["simplified"] for entry in data["results"]]
    assert words
    assert len(words) == len(set(words)), words
    assert data["script"] == "simplified"
    assert isinstance(data["alt_same"], bool)
    if not data["alt_same"]:
        assert data["alt_results"]


def test_grouped_pages_do_not_split_a_simplified_word(search):
    """A word that has several rows (了/瞭) stays on one page."""
    first = search("shi", offset=0)
    second = search("shi", offset=25)
    first_words = {entry["simplified"] for entry in first["results"]}
    second_words = {entry["simplified"] for entry in second["results"]}
    assert first["total"] == second["total"] > 25
    assert first_words.isdisjoint(second_words)


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

    With many prefix matches, the first API page is entirely prefix matches;
    contains rows such as 倒垃圾 are on later pages.
    """
    first_page = _words(search("垃圾"))
    assert first_page[0] == "垃圾"
    assert all(word.startswith("垃圾") for word in first_page)

    from yabai_search import db

    entries, _ = db.search("垃圾", 5000)
    words = [entry["simplified"] for entry in entries]
    ranks = []
    for word in words:
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


def _all_senses(entry):
    return [
        sense
        for definition in entry.get("definitions", [])
        for sense in definition.get("defs", [])
    ]


def test_quoted_english_phrase_only_matches_displayed_glosses(search):
    """A quoted Latin phrase must match a gloss the result actually shows.

    The live Wiktionary gloss table has extra senses for common characters
    (the Taixuanjing tetragram named 文, for example) that are not displayed on
    the CEDICT entry. Searching that table returned 79 results whose visible
    definitions did not contain the phrase. Only the three source-only glosses
    should match, and each result must visibly contain the phrase.
    """
    data = search('"tetragram of the"')
    assert data["total"] == 3
    assert [entry["simplified"] for entry in data["results"]] == [
        "礥",
        "䎡",
        "𫔵",
    ]
    assert all(
        any("tetragram of the" in sense for sense in _all_senses(entry))
        for entry in data["results"]
    )
    assert not any(
        entry["simplified"] in {"文", "中", "上", "事", "交"}
        for entry in data["results"]
    )


def test_unquoted_english_phrase_matches_same_three_glosses(search):
    """The unquoted definition path returns the same three displayed glosses."""
    data = search("tetragram of the")
    assert data["total"] == 3
    assert [entry["simplified"] for entry in data["results"]] == [
        "礥",
        "䎡",
        "𫔵",
    ]


def test_quoted_specific_tetragram_returns_exactly_one(search):
    """A fully specified tetragram phrase resolves to one entry."""
    data = search('"17th tetragram of the"')
    assert data["total"] == 1
    assert data["results"][0]["simplified"] == "䎡"


@pytest.mark.skipif(
    not (DATA / "dictionary-databases" / "corpus_freq.sqlite").exists(),
    reason="frequency database not built",
)
def test_frequency_label_page_is_exact_and_deduped(search):
    """The frequency label page pages over the deduped count, not the raw rows.

    The raw freq table has 405,472 rows, but the pager must reach the deduped
    count (289,606 with the current list). The last page must contain the
    remaining rows and one page past the end must be empty.
    """
    import sqlite3

    from yabai_search.db import to_simplified

    connection = sqlite3.connect(DATA / "dictionary-databases" / "corpus_freq.sqlite")
    try:
        seen = set()
        for (word,) in connection.execute("SELECT word FROM freq ORDER BY rank"):
            seen.add(to_simplified(word))
    finally:
        connection.close()

    first = search('"frequency"')
    assert first["total"] == len(seen)
    assert first["total"] > 5_000
    assert len(first["results"]) == 25

    second = search('"frequency"', offset=25)
    assert len(second["results"]) == 25
    assert second["results"][0]["simplified"] != first["results"][0]["simplified"]

    last_offset = ((first["total"] - 1) // 25) * 25
    last = search('"frequency"', offset=last_offset)
    assert len(last["results"]) == first["total"] - last_offset
    assert len(last["results"]) > 0

    past = search('"frequency"', offset=first["total"])
    assert past["results"] == []
    assert past["total"] == first["total"]


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
    # Variant expansion can merge one extra row onto a page.
    assert len(second["results"]) <= 26

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


def test_variant_expansion_keeps_the_matched_reading(search):
    """Searching "gan" must not pull in 乾's unrelated qián readings via 乹."""
    data = search("gan")
    assert data["pinyin_match"] is True
    assert all(
        to_toneless(entry.get("pinyin", "")) != "qian"
        for entry in data["results"]
    )


def test_traditional_query_keeps_its_own_readings(search):
    """Searching 乾 must still return the qián entries."""
    data = search("乾")
    assert any(
        to_toneless(entry.get("pinyin", "")) == "qian"
        for entry in data["results"]
    )


def test_qian2_tonal_search_finds_the_qian_entry(search):
    """The numeric tonal query qian2 matches 乾[qián]."""
    data = search("qian2")
    assert data["pinyin_match"] is True
    rows = [
        entry
        for entry in data["results"]
        if entry["simplified"] == "乾" and entry["traditional"] == "乾"
    ]
    assert rows
    assert to_toneless(rows[0]["pinyin"]) == "qian"


def test_traditional_qian_merges_the_variant_rows(search):
    """乾 returns one 乾/乾 qián row, not separate 乹/亁 rows."""
    data = search("乾")
    qian = [
        entry
        for entry in data["results"]
        if entry["simplified"] == "乾" and entry["traditional"] == "乾"
    ]
    assert len(qian) == 1
    assert to_toneless(qian[0]["pinyin"]) == "qian"
    # The gān entry also has 乾 as its traditional spelling, but its simplified
    # headword is 干, so it stays a separate row.
    assert any(
        entry["simplified"] == "干" and entry["traditional"] == "乾"
        for entry in data["results"]
    )
    assert not any(
        entry["traditional"] in {"乹", "亁"} for entry in data["results"]
    )


def test_zhi_keeps_the_only_reading_and_its_variants(search):
    """zhi keeps 只[zhǐ] "only" and lists 祇/衹 as its forms."""
    data = search("zhi")

    only = [
        entry
        for entry in data["results"]
        if entry["simplified"] == "只" and entry["traditional"] == "只"
    ]
    assert only
    assert only[0]["pinyin"] == "zhǐ"
    forms = {form["traditional"] for form in only[0].get("forms", [])}
    assert {"祇", "衹"} <= forms

    # The classifier 隻 and the grain word 秖 stay separate readings.
    readings = {
        (entry["traditional"], entry["pinyin"])
        for entry in data["results"]
        if entry["simplified"] == "只"
    }
    assert ("隻", "zhī") in readings
    assert ("秖", "zhī") in readings

    # 纸 (a variant of 紙) is still in the result set. Its page position
    # depends on the data version, so it is not asserted here.


def _cedict_senses(entry):
    return [
        sense
        for definition in entry.get("definitions", [])
        if definition.get("anchor") == "cedict"
        for group in definition.get("groups", [])
        for sense in group.get("senses", [])
    ]


def test_zhi_spellings_keep_their_own_cedict_senses(search):
    """隻 gets the classifier sense and 秖 gets grain, not the shared pool."""
    data = search("只", limit=10)
    rows = {
        entry["traditional"]: entry
        for entry in data["results"]
        if entry["simplified"] == "只"
    }
    assert {"隻", "秖", "只"} <= rows.keys()

    classifier = _cedict_senses(rows["隻"])
    grain = _cedict_senses(rows["秖"])
    only = _cedict_senses(rows["只"])

    assert classifier and all("classifier" in sense for sense in classifier)
    assert grain == ["grain that has begun to ripen"]
    # One CEDICT definition ("only; merely; just") is one sense: ";" separates close glosses, "/" senses.
    assert only == ["only; merely; just"]
    assert not any("grain" in sense for sense in classifier)
    assert not any("classifier" in sense for sense in grain)


def test_zhi_and_zhi_make_keep_their_own_cedict_senses(search):
    """制 "system" and 製 "to make" no longer share one pool of senses."""
    data = search("制", limit=10)
    rows = {
        entry["traditional"]: entry
        for entry in data["results"]
        if entry["simplified"] in {"制", "製"}
    }
    assert {"制", "製"} <= rows.keys()
    system = _cedict_senses(rows["制"])
    make = _cedict_senses(rows["製"])
    assert "system" in system
    assert "to make" in make
    assert "system" not in make
    assert "to make" not in system


def test_pinyin_contains_match_is_full_tier_only(search):
    """nuaile finds 喜怒哀樂 in the full tier, not the fast prefix tier."""
    primary = search("nuaile", part="primary")
    assert not any(
        entry["simplified"] == "喜怒哀乐" for entry in primary["results"]
    )
    full = search("nuaile")
    assert any(
        entry["simplified"] == "喜怒哀乐" and to_toneless(entry["pinyin"]) == "xinuaile"
        for entry in full["results"]
    )


def test_pinyin_contains_matches_follow_prefix_matches():
    """A containing reading (记者 / jìzhě) ranks after prefix matches (这)."""
    from yabai_search import db

    entries, _ = db.search("zhe", 10000)

    def index(word: str) -> int:
        return next(
            position
            for position, entry in enumerate(entries)
            if entry["simplified"] == word
        )

    assert index("这") < index("记者")


def test_chinese_contains_matches_are_full_tier_only_and_last(search):
    """乐 lists 喜怒哀樂 only in the full tier, after prefix matches."""
    primary = search("乐", part="primary")
    assert not any(
        entry["simplified"] == "喜怒哀乐" for entry in primary["results"]
    )

    from yabai_search import db

    entries, _ = db.search("乐", 5000)

    def index(word: str) -> int:
        return next(
            position
            for position, entry in enumerate(entries)
            if entry["simplified"] == word
        )

    assert index("乐") < index("喜怒哀乐")


def test_pinyin_prefix_matches_rank_above_contains(search):
    """All xinu prefixes come before 奧西娜斯, which only contains "xinu"."""
    data = search("xinu", limit=25)
    words = [entry["simplified"] for entry in data["results"]]
    assert "喜怒不形于色" in words
    assert "奥西娜斯" in words
    assert words.index("喜怒不形于色") < words.index("奥西娜斯")

    readings = [to_toneless(entry["pinyin"]) for entry in data["results"]]
    prefix_flags = [reading.startswith("xinu") for reading in readings]
    assert prefix_flags == sorted(prefix_flags, reverse=True)


def test_offset_paginates_wildcard_search(search):
    """Wildcard pages use the fixed 25-result page size, with stable totals."""
    page0 = search("*字", limit=10, offset=0, dicts="cedict,wiktionary")
    page1 = search("*字", limit=10, offset=25, dicts="cedict,wiktionary")
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


@requires_zh_sources
@pytest.mark.parametrize(
    ("dicts", "expected"),
    [
        ("baidu,zhwiktionary", "baidu"),
        ("zhwiktionary,baidu", "zhwiktionary"),
    ],
)
def test_primary_preview_follows_dictionary_drag_order(client, dicts, expected):
    """The first selected dictionary in the chosen language previews the query."""
    response = client.get(
        "/api/search",
        params={
            "q": "档",
            "part": "primary",
            "lang": "ZH",
            "dicts": dicts,
        },
    )
    assert response.status_code == 200
    debug = json.loads(response.headers["x-yabai-debug"])
    assert debug["primary_anchor"] == expected


def test_pinyin_match_flag(search):
    """The pinyin flag is true only for reading-based searches."""
    assert search("dangan")["pinyin_match"] is True
    assert search("档案")["pinyin_match"] is False
    assert search("country")["pinyin_match"] is False
    assert search("*字")["pinyin_match"] is False
    assert search('"country"')["pinyin_match"] is False


def test_unquoted_word_includes_word_jie(search):
    """An unquoted Latin query keeps Latin headword/reading matches."""
    data = search("word", limit=25)
    assert data["pinyin_match"] is False
    words = _words(data)
    assert words[0] == "word姐"
    assert data["results"][0]["tier"] == 1
    assert "词汇" in words


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
        "script",
        "alt_same",
        "alt_results",
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
    readings = [to_toneless(entry["pinyin"]) for entry in entries]
    assert all("xiang" in reading for reading in readings)
    assert any(reading.startswith("xiang") for reading in readings)
    assert any(
        "xiang" in reading and not reading.startswith("xiang")
        for reading in readings
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
    readings = [to_toneless(entry["pinyin"]) for entry in entries]
    assert all("wu" in reading for reading in readings)
    assert any(reading.startswith("wu") for reading in readings)
    assert any(
        "wu" in reading and not reading.startswith("wu")
        for reading in readings
    )


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


def test_taiwan_toggle_searches_prefilled_readings(search):
    """Taiwan readings come from taiwan_readings.sqlite, not mainland pinyin."""
    data = search("lese", lang="TW", taiwan=1)
    assert "垃圾" in [entry["simplified"] for entry in data["results"]]

    # A mainland-only reading is not found with the toggle on.
    data = search("laji", lang="TW", taiwan=1)
    assert "垃圾" not in [entry["simplified"] for entry in data["results"]]
    # ...but it is found with the toggle off.
    data = search("laji", lang="TW", taiwan=0)
    assert "垃圾" in [entry["simplified"] for entry in data["results"]]


def test_taiwan_toggle_honours_overrides_and_tones(search):
    """An override (血 -> xiě) wins, and a wrong tone is rejected."""
    # 血 is an override and should appear in the result set (page position
    # depends on the data version, so check membership via db.search).
    from yabai_search import db

    assert "血" in [entry["simplified"] for entry in db.search("xie", 100, taiwan=True)[0]]
    data = search("xue", lang="TW", taiwan=1)
    assert "血" not in [entry["simplified"] for entry in data["results"]]


def test_taiwan_toggle_contains_search(search):
    """A Taiwan reading is found by a substring, not only a prefix."""
    data = search("setong", lang="TW", taiwan=1)
    assert "垃圾桶" in [entry["simplified"] for entry in data["results"]]


def test_taiwan_toggle_ranks_by_taiwan_reading(search):
    """The tonal query ranks 垃圾 ahead of 垃圾桶 on the Taiwan reading."""
    data = search("le4se4", lang="TW", taiwan=1)
    words = [entry["simplified"] for entry in data["results"]]
    assert "垃圾" in words
    if "垃圾桶" in words:
        assert words.index("垃圾") < words.index("垃圾桶")


def test_taiwan_toggle_finds_wiktionary_only_by_taiwan_reading(search):
    """A Wiktionary-only word is found by its Taiwan reading, not its own."""
    data = search("lesedai", lang="TW", taiwan=1)
    assert "垃圾袋" in [entry["simplified"] for entry in data["results"]]
    data = search("lajidai", lang="TW", taiwan=1)
    assert "垃圾袋" not in [entry["simplified"] for entry in data["results"]]
    """gān (12 senses) ranks above Gān (surname, 2 senses) for 干."""
    data = search("干", lang="EN")
    rows = [entry for entry in data["results"] if entry["simplified"] == "干"]
    assert rows
    first = rows[0]
    assert first["pinyin"] == "gān"

    groups = [
        group
        for definition in first.get("definitions", [])
        if definition.get("anchor") == "cedict"
        for group in definition.get("groups", [])
    ]
    # The surname group stays under the row, after the common gān reading.
    readings = [group["pinyin"] for group in groups]
    assert readings.index("gān") < readings.index("Gān")
    assert any(
        group["pinyin"] == "gān" and "dry" in group["senses"]
        for group in groups
    )
    assert any(
        group["pinyin"] == "Gān" and "surname Gan" in group["senses"]
        for group in groups
    )


def test_wildcard_search_honours_taiwan_toggle(search):
    """A pinyin wildcard can match a Taiwan-only reading."""
    data = search("le*e", lang="TW", taiwan=1, limit=10)
    assert "垃圾" in [entry["simplified"] for entry in data["results"]]
    data = search("le*e", lang="TW", taiwan=0, limit=10)
    assert "垃圾" not in [entry["simplified"] for entry in data["results"]]


def test_chinese_wildcard_ranks_exact_matches_first(search):
    """`?后` opens with two-character words that end in 后."""
    from yabai_search import db

    data = search("?后", lang="EN", dicts="cedict,wiktionary", limit=25)
    assert data["pinyin_match"] is False
    assert data["results"]
    for entry in data["results"]:
        assert (
            db._wildcard_tier(
                [entry["simplified"], entry["traditional"]], "?后"
            )
            == 1
        )


def test_chinese_wildcard_star_exact_matches_first(search):
    """`*后` starts with every word that ends in 后."""
    from yabai_search import db

    data = search("*后", lang="EN", dicts="cedict,wiktionary", limit=25)
    assert "后" in [entry["simplified"] for entry in data["results"]]
    for entry in data["results"]:
        assert (
            db._wildcard_tier(
                [entry["simplified"], entry["traditional"]], "*后"
            )
            == 1
        )


def test_shared_simplified_headword_does_not_repeat_a_sense(search):
    """CEDICT variants that share a simplified headword dedupe identical senses.

    乾, 干 and 幹 all store their senses under the simplified word 干, so the
    "surname Gan" sense used to appear twice in the Gān group.
    """
    data = search("干", lang="EN")
    groups = [
        group
        for entry in data["results"]
        for definition in entry.get("definitions", [])
        if definition.get("anchor") == "cedict"
        for group in definition.get("groups", [])
    ]
    assert groups
    for group in groups:
        senses = group.get("senses", [])
        assert len(senses) == len(set(senses)), (group.get("pinyin"), senses)
    surname_groups = [
        group for group in groups if "surname Gan" in group.get("senses", [])
    ]
    assert surname_groups
    assert all(
        group["senses"].count("surname Gan") == 1 for group in surname_groups
    )


@requires_zh_sources
def test_star_puts_the_selected_dictionarys_words_first(search):
    """Picking one dictionary leads the "*" list with the words it defines.

    Before, the first page was the same whatever was selected, so choosing only
    百度汉语 showed words that only CEDICT could define, all under "Other
    dictionaries".
    """
    everything = _words(search("*", lang="ZH"))
    baidu = _words(search("*", lang="ZH", dicts="baidu"))
    assert everything != baidu
    data = search("*", lang="ZH", dicts="baidu", limit=8)
    for entry in data["results"]:
        anchors = {block["anchor"] for block in entry.get("definitions", [])}
        assert "baidu" in anchors, entry["simplified"]
