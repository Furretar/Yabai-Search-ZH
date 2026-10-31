"""Judged English queries, run on the saved in-memory rows (no database)."""

from __future__ import annotations

import json
from pathlib import Path

from yabai_search import relevance

JUDGMENTS = Path(__file__).resolve().parent / "data" / "relevance_judgments.json"


def _entry(row: dict) -> dict:
    return {
        "simplified": row["simplified"],
        "traditional": row["traditional"],
        "pinyin": row["pinyin"],
        "english": row["english"],
        "freq": None if row["rank"] is None else {"rank": row["rank"]},
    }


def test_judged_english_queries_keep_their_top_rows():
    cases = json.loads(JUDGMENTS.read_text(encoding="utf-8"))
    assert len(cases) >= 20
    for case in cases:
        entries = [_entry(row) for row in case["entries"]]
        ranked = relevance.rank_entries(entries, case["query"])
        assert [entry["simplified"] for entry in ranked] == case["expected"], (
            case["query"]
        )
