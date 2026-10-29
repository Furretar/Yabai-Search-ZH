"""Regression tests: result order must not depend on where the page breaks fall.

The server pages the ranked list and the browser re-sorts each page by
(category, tier) (entryCategory() / entryTier() in static/app.js). If the
server's order is not already that order, a green (tier 3) row can open page 47
while red and yellow rows are still on later pages. These tests pin the server
order to the page's order.

Run from the directory that contains the `yabai_search` package:

    python -m unittest yabai_search.tests.test_tier_order -v

(pytest collects them too.) No database is needed: the two dictionary lookups
are patched, and `opencc` is stubbed when it is not installed.
"""

from __future__ import annotations

import random
import sys
import types
import unittest
from unittest import mock

try:  # db imports opencc at module load; the ranking code never uses it.
    import opencc  # noqa: F401
except ImportError:  # pragma: no cover - only on machines without opencc
    stub = types.ModuleType("opencc")
    stub.OpenCC = lambda *args, **kwargs: types.SimpleNamespace(convert=lambda s: s)
    sys.modules["opencc"] = stub

from yabai_search import db  # noqa: E402

PAGE_SIZE = 25
# Headwords the fake "wiktionary" dictionary defines (selected dict is cedict).
WIKTIONARY_WORDS: set[str] = set()


def fake_defined(anchor: str, words) -> set[str]:
    """Stand-in for db._anchor_defined_words: who has a definition in `anchor`."""
    return {w for w in words if w in WIKTIONARY_WORDS} if anchor == "wiktionary" else set()


def entry(word, pinyin, english="", **extra):
    row = {"simplified": word, "traditional": word, "pinyin": pinyin, "english": english}
    row.update(extra)
    return row


def client_tier(row, query, defined_words, taiwan=False):
    """Independent re-statement of entryTier() in static/app.js.

    Deliberately NOT built on db._match_tier, so the two implementations check
    each other. `defined_words` is every headword with a definition anywhere.
    """
    if row.get("pinyin_label") or row.get("pinyin_no_tone"):
        return 4
    if not (row["english"] or row["simplified"] in defined_words):
        return 5 if row["pinyin"] else 6
    marks = {"ā": "a", "á": "a", "ǎ": "a", "à": "a", "ē": "e", "é": "e", "ě": "e",
             "è": "e", "ī": "i", "í": "i", "ǐ": "i", "ì": "i", "ō": "o", "ó": "o",
             "ǒ": "o", "ò": "o", "ū": "u", "ú": "u", "ǔ": "u", "ù": "u", "ǖ": "ü",
             "ǘ": "ü", "ǚ": "ü", "ǜ": "ü"}

    def toneless(text):
        text = "".join(marks.get(c, c) for c in text.lower())
        return "".join(c for c in text if "a" <= c <= "z" or c == "ü")

    key = toneless(query)
    shown = (row.get("pinyin_taiwan") if taiwan else None) or row["pinyin"]
    reading = toneless(shown)
    if reading == key:
        return 1
    if reading.startswith(key):
        return 2
    return 3


def client_category(row, selected, defined_words):
    """Mirror entryCategory() in static/app.js."""
    in_selected = bool(row["english"]) if "cedict" in selected else False
    if not in_selected and row["simplified"] in defined_words:
        in_selected = "wiktionary" in selected
    if in_selected:
        return 1
    return 2 if (row["english"] or row["simplified"] in defined_words) else 3


def client_key(row, query, selected, defined_words, taiwan=False):
    """Category first, then tier: the order the server and the page must share."""
    return (
        client_category(row, selected, defined_words),
        client_tier(row, query, defined_words, taiwan),
    )


class RankPatch(unittest.TestCase):
    """Patches the two dictionary lookups so no database is touched."""

    def setUp(self):
        WIKTIONARY_WORDS.clear()
        patches = [
            mock.patch.object(db, "_anchor_defined_words", fake_defined),
            mock.patch.object(
                db, "_SOURCE_QUERIES", [{"anchor": "cedict"}, {"anchor": "wiktionary"}]
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def rank(self, rows, query, taiwan=False, selected=("cedict",)):
        return db.rank_by_category(list(rows), set(selected), query, taiwan)

    def words(self, rows):
        return [row["simplified"] for row in rows]


class RegressionDangPage48(RankPatch):
    """The reported bug: ?q=dang, last pages opened with green rows."""

    def test_category_beats_tier_across_dictionaries(self):
        # Every category 1 row comes first, even a tier 3 "contains" over a
        # category 2 tier 1 "exact". This is the README rule.
        WIKTIONARY_WORDS.update({"W_green", "W_exact", "W_prefix"})
        rows = [
            entry("W_green", "tai wan min zhong dǎng"),
            entry("C_green", "shàng dàng", "to be fooled"),
            entry("W_exact", "dàng"),
            entry("C_exact", "dāng", "to act as"),
            entry("W_prefix", "dǎng bù"),
            entry("C_prefix", "dāng dāng", "ding dong"),
        ]
        ranked = self.rank(rows, "dang")
        self.assertEqual(
            self.words(ranked),
            ["C_exact", "C_prefix", "C_green", "W_exact", "W_prefix", "W_green"],
        )

    def test_no_category_2_row_before_a_category_1_row(self):
        WIKTIONARY_WORDS.update({f"W{i}" for i in range(60)})
        rng = random.Random(7)
        readings = ["dāng", "dǎng bù", "shàng dàng", "dàng", "tai wan dǎng"]
        rows = [entry(f"W{i}", rng.choice(readings)) for i in range(60)]
        rows += [entry(f"C{i}", rng.choice(readings), "def") for i in range(60)]
        rng.shuffle(rows)
        keys = [
            client_key(r, "dang", {"cedict"}, WIKTIONARY_WORDS)
            for r in self.rank(rows, "dang")
        ]
        self.assertEqual(keys, sorted(keys))


class PagesAreIrrelevant(RankPatch):
    """Whatever the page size, tier-sorting each page changes nothing."""

    def make_rows(self, seed, count):
        rng = random.Random(seed)
        readings = [
            "dāng", "dàng", "dāng dāng", "dǎng bù", "shàng dàng", "tai wan dǎng",
            "xiǎo dāng", "dāng", "gōng zhí dàng",
        ]
        rows = []
        for i in range(count):
            word = f"X{seed}_{i}"
            kind = rng.choice(["cedict", "wikt", "bare", "bare_noreading", "dialect"])
            reading = rng.choice(readings)
            if kind == "cedict":
                rows.append(entry(word, reading, "a definition"))
            elif kind == "wikt":
                WIKTIONARY_WORDS.add(word)
                rows.append(entry(word, reading))
            elif kind == "bare":
                rows.append(entry(word, reading))
            elif kind == "bare_noreading":
                rows.append(entry(word, ""))
            else:
                rows.append(entry(word, "", "dialect gloss", pinyin_label="Wu"))
        return rows

    def test_page_sort_is_a_noop_for_every_page(self):
        for seed in range(25):
            WIKTIONARY_WORDS.clear()
            rows = self.make_rows(seed, 200)
            for query in ("dang", "dāng", "DANG"):
                ranked = self.rank(rows, query)
                defined = WIKTIONARY_WORDS | {r["simplified"] for r in ranked if r["english"]}
                for size in (1, 7, PAGE_SIZE, 50):
                    rebuilt = []
                    for start in range(0, len(ranked), size):
                        page = ranked[start : start + size]
                        # The browser's stable per-page sort.
                        page = sorted(
                            page,
                            key=lambda r: client_key(
                                r, query, {"cedict"}, defined
                            ),
                        )
                        rebuilt.extend(page)
                    self.assertEqual(
                        self.words(rebuilt), self.words(ranked),
                        f"seed={seed} query={query!r} page size={size}",
                    )

    def test_keys_never_decrease_down_the_whole_list(self):
        for seed in range(25):
            WIKTIONARY_WORDS.clear()
            ranked = self.rank(self.make_rows(seed, 300), "dang")
            defined = WIKTIONARY_WORDS | {r["simplified"] for r in ranked if r["english"]}
            keys = [
                client_key(r, "dang", {"cedict"}, defined) for r in ranked
            ]
            self.assertEqual(keys, sorted(keys), f"seed={seed}")


class OrderingRules(RankPatch):
    def test_selected_dictionary_comes_first_inside_a_tier(self):
        WIKTIONARY_WORDS.add("W")
        rows = [entry("W", "dāng"), entry("C", "dāng", "to act")]
        self.assertEqual(self.words(self.rank(rows, "dang")), ["C", "W"])

    def test_category_beats_tier(self):
        # A CEDICT "contains" (category 1, tier 3) outranks an exact match from
        # another dictionary (category 2, tier 1), as the README says.
        WIKTIONARY_WORDS.add("W")
        rows = [entry("C", "shàng dàng", "to be fooled"), entry("W", "dāng")]
        self.assertEqual(self.words(self.rank(rows, "dang")), ["C", "W"])

    def test_category_1_dialect_beats_category_2_exact(self):
        """Tier 4 stays inside its category: a defined dialect row comes first.

        A dialect row with a definition in the selected dictionary is category
        1 tier 4. It must beat a category 2 tier 1 exact match, because category
        always wins.
        """
        WIKTIONARY_WORDS.add("W")
        rows = [
            entry("W", "dāng"),
            entry("D", "", "dialect gloss", pinyin_label="Wu"),
        ]
        self.assertEqual(self.words(self.rank(rows, "dang")), ["D", "W"])

    def test_stable_inside_one_tier_and_section(self):
        rows = [entry(f"C{i}", "shàng dàng", "def") for i in range(10)]
        self.assertEqual(self.words(self.rank(rows, "dang")), self.words(rows))

    def test_dialect_then_undefined_sink_below_defined(self):
        rows = [
            entry("none", ""),
            entry("reading_only", "dāng"),
            entry("dialect", "", "gloss", pinyin_label="Wu"),
            entry("good", "dāng", "to act"),
        ]
        self.assertEqual(
            self.words(self.rank(rows, "dang")),
            ["good", "dialect", "reading_only", "none"],
        )

    def test_empty_list(self):
        self.assertEqual(self.rank([], "dang"), [])


class MatchTier(unittest.TestCase):
    """_match_tier must agree with matchTier() in static/app.js."""

    def tier(self, reading, query, **extra):
        return db._match_tier(entry("X", reading, "d", **extra), query, extra.pop("taiwan", False))

    def test_pinyin_levels(self):
        self.assertEqual(self.tier("dāng", "dang"), 1)
        self.assertEqual(self.tier("dāng", "dāng"), 1)
        self.assertEqual(self.tier("dāng dāng", "dang"), 2)
        self.assertEqual(self.tier("shàng dàng", "dang"), 3)
        self.assertIsNone(self.tier("hao", "dang"))

    def test_spaces_and_case_are_ignored(self):
        self.assertEqual(self.tier("dāng dāng", "DANGDANG"), 1)
        self.assertEqual(self.tier("dāng dāng", "dang dang"), 1)

    def test_quoted_query_uses_the_letters_inside(self):
        self.assertEqual(self.tier("dāng", '"dang"'), 1)

    def test_u_umlaut_matches_the_client_not_pinyin_helper(self):
        # The page does not turn v into ü, so "nv" is NOT an exact match for nǚ.
        self.assertIsNone(self.tier("nǚ", "nv"))
        self.assertEqual(self.tier("nǚ", "nü"), 1)

    def test_taiwan_reading_is_used_when_the_toggle_is_on(self):
        row = entry("X", "dāng", "d", pinyin_taiwan="dàng")
        self.assertEqual(db._match_tier(row, "dang", True), 1)
        row = entry("X", "yī", "d", pinyin_taiwan="yì")
        self.assertEqual(db._match_tier(row, "yi", False), 1)
        row = entry("X", "xiāng", "d", pinyin_taiwan="shāng")
        self.assertIsNone(db._match_tier(row, "xiang", True))

    def test_cjk_query_compares_headwords(self):
        self.assertEqual(db._match_tier(entry("当", "dāng"), "当", False), 1)
        self.assertEqual(db._match_tier(entry("当然", "dāng rán"), "当", False), 2)
        self.assertEqual(db._match_tier(entry("上当", "shàng dàng"), "当", False), 3)

    def test_empty_query(self):
        self.assertIsNone(db._match_tier(entry("X", "dāng"), "  ", False))


if __name__ == "__main__":
    unittest.main()
