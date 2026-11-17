"""A definition search only counts the SELECTED dictionaries as category 1.

A query that matches no reading or headword ("word") finds rows through some
dictionary's definition text. Which dictionary matched decides the section: a
row whose matching definition is in a selected dictionary is a main result, any
other row is listed under "Other Dictionaries" (the fallback to the lower
priority dictionaries). That a selected dictionary also lists the HEADWORD
(字 exists in the MOE dictionary) must not move a CC-CEDICT-only definition
match out of "Other Dictionaries".

The ranking tests build their rows and databases by hand, so they do not need
the real data. The compact-mode tests run the real functions of static/app.js in
Node.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from yabai_search import db, merge_simp_trad as merge

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "yabai_search" / "static" / "app.js"
TAIWAN = {"moe", "moe_revised"}


def cedict_row(simplified: str, text: str = "word", **extra) -> dict:
    """A CC-CEDICT definition match, as rank_by_category receives it."""
    return {
        "simplified": simplified,
        "traditional": simplified,
        "pinyin": "zi4",
        "english": text,
        "english_normalized": text,
        "_relevance": True,
        **extra,
    }


@pytest.fixture
def moe_lists_every_headword(monkeypatch):
    """Every headword also exists in the MOE dictionaries (like 字 in real data)."""
    real = db._anchor_defined_words

    def defined(anchor, words):
        return set(words) if anchor in TAIWAN else real(anchor, words)

    monkeypatch.setattr(db, "_anchor_defined_words", defined)
    # Only CC-CEDICT's own text counts as a definition match in these unit
    # tests; the real source databases would make the result depend on what is
    # installed.
    import re as _re

    def _mock_match(entries, query):
        needle = db._definition_needle(query)
        out = []
        for entry in entries:
            text = entry.get("english_normalized") or entry.get("english") or ""
            if needle and _re.search(db._query_regex(needle).pattern, text.lower()):
                out.append({"cedict"})
            else:
                out.append(set())
        return out

    monkeypatch.setattr(db, "_definition_match_anchors", _mock_match)


def rank(entries, selected, query="word", taiwan=True):
    return db.rank_by_category(entries, set(selected), query, taiwan, True)


def test_definition_needle_drops_quotes_and_case():
    assert db._definition_needle('"Word"') == "word"
    assert db._definition_needle("  word ") == "word"
    assert db._definition_needle('"') == '"'


def test_cedict_only_match_is_other_when_taiwan_is_selected(
    moe_lists_every_headword,
):
    """Taiwan mode, "word": every result is in "Other Dictionaries"."""
    ranked = rank([cedict_row("字"), cedict_row("词")], TAIWAN)
    assert [entry["category"] for entry in ranked] == [2, 2]
    # The page shows the dictionary that matched, not the MOE definition.
    assert [entry["match_anchors"] for entry in ranked] == [["cedict"], ["cedict"]]


def test_cedict_match_is_a_main_result_when_cedict_is_selected(
    moe_lists_every_headword,
):
    ranked = rank([cedict_row("字")], {"cedict"})
    assert ranked[0]["category"] == 1
    assert ranked[0]["match_anchors"] == ["cedict"]


def test_relevance_order_is_kept_inside_a_category(moe_lists_every_headword):
    """The category sort is stable: the search's own order holds within it."""
    rows = [cedict_row("丙"), cedict_row("甲", "a word"), cedict_row("乙")]
    ranked = rank(rows, TAIWAN)
    assert [entry["simplified"] for entry in ranked] == ["丙", "甲", "乙"]
    assert {entry["category"] for entry in ranked} == {2}


@pytest.fixture(autouse=True)
def _ask_each_dictionary(monkeypatch):
    """These tests build small fake dictionaries, so the ranking must ask each dictionary in turn.

    The precomputed `definers` table describes the real data and cannot know about them.
    """
    monkeypatch.setattr(db, "definer_masks", lambda words: None)


def test_selected_source_definition_beats_other_dictionary_rows(
    monkeypatch, tmp_path
):
    """A row matched in a selected source sorts above rows matched elsewhere."""
    moe = tmp_path / "moe.sqlite"
    con = sqlite3.connect(moe)
    con.execute("CREATE TABLE senses (word TEXT, position INTEGER, text TEXT)")
    con.execute("INSERT INTO senses VALUES ('甲', 1, 'a Word in plain text')")
    con.commit()
    con.close()
    sources = [
        {
            "anchor": "cedict",
            "lang": "english",
            "database": tmp_path / "missing.sqlite",
            "sense_sql": db._CEDICT_SENSES_SQL,
        },
        {
            "anchor": "moe",
            "lang": "taiwan",
            "database": moe,
            "sense_sql": db._SENSES_SQL,
        },
    ]
    monkeypatch.setattr(db, "_SOURCE_QUERIES", sources)
    source_only = {
        "simplified": "甲",
        "traditional": "甲",
        "pinyin": "",
        "english": "",
        "english_normalized": "",
        "_relevance": True,
    }
    ranked = rank([cedict_row("乙"), source_only], {"moe"})
    assert [(e["simplified"], e["category"]) for e in ranked] == [
        ("甲", 1),
        ("乙", 2),
    ]
    assert ranked[0]["match_anchors"] == ["moe"]


def test_latin_headword_match_keeps_the_headword_rule(moe_lists_every_headword):
    """word姐 matched by its headword, not by a definition."""
    row = cedict_row("word姐", "(slang) a woman", _head_match=True)
    ranked = rank([row], TAIWAN)
    assert ranked[0]["category"] == 1  # MOE lists the headword
    assert "_head_match" not in ranked[0]
    assert "match_anchors" not in ranked[0]


def test_unverifiable_row_keeps_the_headword_rule(moe_lists_every_headword):
    """A row no dictionary's text confirms is ranked as before."""
    row = cedict_row("字", "unrelated")  # "word" is not in its text
    ranked = rank([row], TAIWAN)
    assert ranked[0]["category"] == 1
    assert "match_anchors" not in ranked[0]


def test_head_match_marker_never_leaks_from_a_normal_ranking():
    """The internal marker is removed even when the row is not definition-ranked."""
    row = {
        "simplified": "字",
        "traditional": "字",
        "pinyin": "zi4",
        "english": "word",
        "_head_match": True,
    }
    ranked = db.rank_by_category([row], {"cedict"}, "zi", False)
    assert "_head_match" not in ranked[0]


def test_merge_keeps_match_anchors_of_every_spelling():
    rows = [
        {"simplified": "了", "traditional": "了", "match_anchors": ["cedict"]},
        {"simplified": "了", "traditional": "瞭", "match_anchors": ["wiktionary"]},
    ]
    merged = merge.merge_entries(rows, "simplified", False)
    assert len(merged) == 1
    assert merged[0]["match_anchors"] == ["cedict", "wiktionary"]


def test_api_taiwan_definition_search_lists_everything_under_other(
    search, monkeypatch
):
    """/api/search?q=word with Taiwan selected: no main result at all."""
    real = db._anchor_defined_words
    monkeypatch.setattr(
        db,
        "_anchor_defined_words",
        lambda anchor, words: set(words) if anchor in TAIWAN else real(anchor, words),
    )
    import yabai_search.services as _main

    monkeypatch.setattr(_main, "_RANK_CACHE_ENABLED", False)
    monkeypatch.setattr(_main, "_rank_cache_get_or_compute", lambda key, compute: (compute(), "skipped"))
    import re as _re

    def _mock_match(entries, query):
        needle = db._definition_needle(query)
        out = []
        for entry in entries:
            text = entry.get("english_normalized") or entry.get("english") or ""
            if needle and _re.search(db._query_regex(needle).pattern, text.lower()):
                out.append({"cedict"})
            else:
                out.append(set())
        return out

    monkeypatch.setattr(db, "_definition_match_anchors", _mock_match)
    data = search("vocabulary", taiwan="true", lang="TW", dicts="moe,moe_revised")
    assert data["results"], "the dummy data has CC-CEDICT rows containing 'vocabulary'"
    # Definition matches in a non-selected language land under "Other
    # Dictionaries"; headword matches keep the old rule.
    definition_rows = [row for row in data["results"] if row.get("match_anchors")]
    assert definition_rows
    assert {row["category"] for row in definition_rows} == {2}
    assert all("cedict" in row["match_anchors"] for row in definition_rows)

    main = search("vocabulary", lang="EN", dicts="cedict")
    # Definition matches in the selected dictionary are main results; headword
    # matches or unverifiable rows may also appear (category 1 or 2).
    main_defs = [row for row in main["results"] if row.get("match_anchors")]
    assert main_defs
    assert {row["category"] for row in main_defs} == {1}


# --- compact mode (custom dictionaries off) -------------------------------

HARNESS = r"""
// With custom dictionaries off the selection is the search language's defaults (TW: the two MOE dictionaries).
const state = {customDicts: false, compact: true, lang: "TW", dicts: new Set(["moe", "moe_revised"])};
// The Dictionaries panel's toggles, in panel order (what the page reads its tie-break order from).
const langDefaults = {EN: ["cedict", "wiktionary"], TW: ["moe", "moe_revised"]}; // the page's per-language defaults
const dictToggles = ["cedict", "wiktionary", "moe", "moe_revised"].map((anchor) => ({dataset: {anchor}}));
let dictOrder = {english: ["cedict", "wiktionary"], taiwan: ["moe", "moe_revised"]};
const anchorLang = new Map([
  ["cedict", "english"], ["wiktionary", "english"],
  ["moe", "taiwan"], ["moe_revised", "taiwan"],
]);
// What the page builds from the dictionary toggles (the traits in db.py): the pair shown as one row, and the
// dictionary that supplies variant forms.
const mergedMembers = new Map([["moe", ["moe", "moe_revised"]], ["moe_revised", ["moe", "moe_revised"]]]);
const formsAnchors = new Set(["cedict"]);
const anyEnabled = (enabled, anchors) => [...anchors].some((anchor) => enabled.has(anchor));
%(slice)s
%(has_selected)s
const block = (anchor) => ({anchor, lang: anchorLang.get(anchor)});
const entry = (anchors, extra = {}) => ({
  traditional: "字", simplified: "字",
  definitions: anchors.map(block), ...extra,
});
const shown = (e, setup) => {
  entryDictState.clear();
  if (setup) setup();
  return [...entryDicts(e)].sort();
};
const out = {};
%(body)s
console.log(JSON.stringify(out));
"""


def run_js(body: str) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("const entryDictState = new Map();")
    end = source.index("// Which dictionaries to render for a result.")
    marker = "// A \"see X\" variant has no senses, only \"other forms\""
    begin = source.index(marker)
    defs_at = source.index("function hasSelectedDefs(entry) {", begin)
    has_selected = source[begin : source.index("\n}\n", defs_at) + 3]
    script = HARNESS % {
        "slice": source[start:end],
        "has_selected": has_selected,
        "body": body,
    }
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_compact_mode_keeps_both_moe_dictionaries_for_the_merged_block():
    """Both MOE anchors stay on so the frontend can merge their definitions."""
    out = run_js(
        """
        out.both = shown(entry(["moe", "moe_revised"]));
        out.only_revised = shown(entry(["moe_revised"]));
        out.revised_order = shown(entry(["moe", "moe_revised"]), () => {
          dictOrder.taiwan = ["moe_revised", "moe"];
        });
        """
    )
    assert out["both"] == ["moe", "moe_revised"]
    assert out["only_revised"] == ["moe_revised"]
    # Both anchors stay on regardless of the saved order; the merged block
    # still puts the concise definitions on top.
    assert out["revised_order"] == ["moe", "moe_revised"]


def test_compact_mode_shows_the_dictionary_that_matched_a_definition_search():
    out = run_js(
        """
        const other = entry(["cedict", "moe"], {category: 2, match_anchors: ["cedict"]});
        out.shown = shown(other);
        out.selected = hasSelectedDefs(other);
        const main = entry(["cedict", "moe"], {category: 1, match_anchors: ["moe"]});
        out.main = shown(main);
        out.main_selected = hasSelectedDefs(main);
        """
    )
    assert out["shown"] == ["cedict"]
    assert out["selected"] is False  # still listed under "Other Dictionaries"
    assert out["main"] == ["moe"]
    assert out["main_selected"] is True


def test_compact_mode_falls_back_to_other_languages_best_dictionary():
    out = run_js(
        """
        out.english = shown(entry(["wiktionary", "cedict"], {category: 2}));
        out.reordered = shown(entry(["wiktionary", "cedict"], {category: 2}), () => {
          dictOrder.english = ["wiktionary", "cedict"];
        });
        out.none = shown(entry([], {category: 3}));
        """
    )
    assert out["english"] == ["cedict"]  # one dictionary, highest priority
    assert out["reordered"] == ["wiktionary"]
    assert out["none"] == []


def test_custom_dictionaries_keep_the_selection_unchanged():
    out = run_js(
        """
        state.customDicts = true;
        state.compact = false;
        state.dicts = new Set(["moe"]);
        out.custom = shown(entry(["cedict", "moe"], {category: 2, match_anchors: ["cedict"]}));
        """
    )
    assert out["custom"] == ["moe"]
