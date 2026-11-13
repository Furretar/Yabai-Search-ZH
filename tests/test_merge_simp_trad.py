"""Unit tests for the server-side result merge (yabai_search/merge_simp_trad.py).

These mirror mergeEntries()/mergeEntry() in static/app.js. They do not need a
database: the rows are small hand-built dictionaries.
"""

from __future__ import annotations

from yabai_search import merge_simp_trad as merge


def row(
    simplified,
    traditional,
    pinyin="",
    defs=None,
    score=0,
    anchor="cedict",
    sources=None,
    forms=None,
):
    """A CEDICT-shaped row with one definition block."""
    block = {
        "anchor": anchor,
        "label": anchor,
        "defs": list(defs or []),
        "groups": [],
        "classifiers": [],
    }
    return {
        "simplified": simplified,
        "traditional": traditional,
        "pinyin": pinyin,
        "reading_score": score,
        "definitions": [block],
        "sources": list(sources or [{"anchor": anchor, "label": anchor}]),
        "forms": list(forms or []),
        "mergedForms": [],
    }


def test_simplified_merges_rows_sharing_a_simplified_form():
    """了/瞭 display as 了 in simplified mode and become one row."""
    rows = [
        row("了", "了", "le", ["(particle)"], score=5),
        row("了", "瞭", "liǎo", ["to look"], score=1),
    ]
    merged = merge.merge_entries(rows)
    assert len(merged) == 1
    merged_row = merged[0]
    # The base is the row with the best reading_score.
    assert merged_row["traditional"] == "了"
    assert merged_row["pinyin"] == "le"
    # The other spelling is listed as a merged form, not as a second row.
    assert {"traditional": "瞭", "pinyin": "liǎo"} in merged_row["mergedForms"]
    # Definition blocks stay separate per traditional spelling.
    assert [block["traditional"] for block in merged_row["definitions"]] == [
        "了",
        "瞭",
    ]
    assert [block["defs"] for block in merged_row["definitions"]] == [
        ["(particle)"],
        ["to look"],
    ]


def test_traditional_keeps_spellings_apart():
    """隻 and 只 are different rows in traditional mode."""
    rows = [
        row("只", "只", "zhī", ["classifier"]),
        row("只", "隻", "zhī", ["classifier"]),
    ]
    merged = merge.merge_entries(rows, script="traditional")
    assert [entry["traditional"] for entry in merged] == ["只", "隻"]


def test_traditional_merges_two_readings_of_one_spelling():
    """The same spelling with two readings becomes one row, best reading first."""
    rows = [
        row("只", "只", "zhī", ["classifier"], score=1),
        row("只", "只", "zhǐ", ["only"], score=4),
    ]
    merged = merge.merge_entries(rows, script="traditional")
    assert len(merged) == 1
    assert merged[0]["pinyin"] == "zhǐ"
    # The two definition blocks share a spelling and anchor, so their senses
    # merge into one block.
    assert merged[0]["definitions"][0]["defs"] == ["only", "classifier"]


def test_sources_and_forms_are_deduplicated():
    """A chip is listed once, and a row never lists its own spelling."""
    rows = [
        row("和", "和", "hé", ["and"], anchor="cedict"),
        row(
            "和",
            "咊",
            "hé",
            ["and"],
            anchor="wiktionary",
            sources=[
                {"anchor": "cedict", "label": "CC-CEDICT"},
                {"anchor": "wiktionary", "label": "Wiktionary"},
            ],
            forms=[{"traditional": "和"}, {"traditional": "龢"}],
        ),
    ]
    merged = merge.merge_entries(rows)
    assert [chip["anchor"] for chip in merged[0]["sources"]] == [
        "cedict",
        "wiktionary",
    ]
    assert [form["traditional"] for form in merged[0]["forms"]] == ["龢"]


def test_page_layouts_marks_identical_pages():
    """No split spellings on the page means both scripts share one layout."""
    rows = [
        row("和", "和", "hé", ["and"]),
        row("了", "了", "le", ["(particle)"]),
    ]
    simplified, traditional, alt_same = merge.page_layouts(rows)
    assert alt_same is True
    assert simplified == traditional
    assert len(simplified) == 2


def test_page_layouts_sends_both_when_a_group_splits():
    """隻/只 make the two scripts different, so both layouts are built."""
    rows = [
        row("只", "只", "zhī", ["classifier"]),
        row("只", "隻", "zhī", ["classifier"]),
    ]
    simplified, traditional, alt_same = merge.page_layouts(rows)
    assert alt_same is False
    assert len(simplified) == 1
    assert len(traditional) == 2
    assert [entry["traditional"] for entry in traditional] == ["只", "隻"]


def test_page_layout_cache_reuses_identical_requests():
    """The server builds a page's two layouts once per request key."""
    from yabai_search.services import _page_layout_cache, _page_layouts_cached

    rows = [row("和", "和", "hé", ["and"])]
    key = ("q", 0, 25, "EN", ("cedict",), False, "all", "data-v1")
    _page_layout_cache.clear()
    first = _page_layouts_cached(key, rows, False)
    second = _page_layouts_cached(key, rows, False)
    # The cached layout lists are reused, not rebuilt.
    assert first[0] is second[0]
    assert first[1] is second[1]
    assert first[2] is True


def test_merge_does_not_mutate_the_input_rows():
    """The API data is reused when the display options change."""
    rows = [row("和", "和", "hé", ["and"]), row("和", "咊", "hé", ["and"])]
    before = [dict(entry) for entry in rows]
    merge.merge_entries(rows)
    for original, snapshot in zip(rows, before):
        assert original["definitions"] == snapshot["definitions"]
        assert original["sources"] == snapshot["sources"]
        assert original["mergedForms"] == snapshot["mergedForms"]
