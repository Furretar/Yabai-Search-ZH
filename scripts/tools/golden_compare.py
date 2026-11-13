#!/usr/bin/env python3
"""Build and compare a golden query set for Yabai Search.

This uses db.search directly so it can run old and new code in separate
processes without depending on a running HTTP server. Timing fields are not
stored; result order, headwords and readings are.

Examples:
    .venv/bin/python scripts/tools/golden_compare.py write
    YABAI_GLOSS_LEGACY=1 .venv/bin/python scripts/tools/golden_compare.py run legacy
    .venv/bin/python scripts/tools/golden_compare.py run new
    .venv/bin/python scripts/tools/golden_compare.py compare
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from yabai_search import db  # noqa: E402
from yabai_search.pinyin import to_toneless  # noqa: E402

DATA = ROOT / "data" / "temp"
QUERIES = DATA / "golden_queries.json"
CEDICT = ROOT / "data" / "dictionary-databases" / "cedict.sqlite"


def make_queries() -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()

    def add(query: str) -> None:
        query = (query or "").strip()
        if query and query not in seen:
            seen.add(query)
            queries.append(query)

    connection = sqlite3.connect(CEDICT)
    rows = connection.execute(
        "SELECT simplified, traditional, pinyin, pinyin_numeric, english "
        "FROM entries WHERE simplified <> '' "
        "ORDER BY id LIMIT 1200"
    ).fetchall()
    connection.close()
    rng = random.Random(20261027)
    rng.shuffle(rows)

    # Chinese headwords, both scripts.
    for index, row in enumerate(rows[:260]):
        add(row[0] if index % 2 == 0 else row[1])

    # Pinyin with and without tones.
    for row in rows[260:340]:
        pinyin = (row[2] or "").strip()
        numeric = (row[3] or "").strip()
        if pinyin:
            add(pinyin)
        if numeric:
            add(numeric)
            add(numeric.replace(" ", ""))
        if pinyin:
            add(to_toneless(pinyin))

    # English terms and phrases from the application tests and common glosses.
    english = [
        "country", "trash", "tetragram", "tetragram of the",
        "17th tetragram of the", "soft palate", "to shrink", "weak",
        "floppy disk", "cork tile", "cork flooring", "to solder",
        "credulous person", "inkfish", "velar sound", "prion",
        "the center", "pattern, repetition", "ascension", "contact",
        "severance", "aggravation", "departure", "vastness",
        "hardness", "expansion", "greatness", "failure", "ease",
        "guardedness", "closeness", "on the verge", "residence",
        "divergence", "constancy", "opposition", "measure",
        "enlargement", "strength", "following", "response", "completion",
        "contrariety", "gathering", "decisiveness", "unity",
        "darkening", "change", "resistance", "joy", "bold resolution",
        "eternity", "sinking", "law", "diminishment", "contention",
        "doubt", "fullness", "legion", "purity", "dimming",
    ]
    for term in english:
        add(term)
        add(f'"{term}"')

    # 1-3 character prefixes, including known slow examples.
    for term in ("d", "shi", "a", "ab", "abc", "ruan3", "ruan4",
                 "档案", "軟", "中国", "人民", "一", "不", "的",
                 "zhong", "zhongguo", "xue", "xuexi"):
        add(term)
    for row in rows[:80]:
        word = row[0]
        add(word[:1])
        add(word[:2])
        add(word[:3])

    # Accents, non-Latin scripts and under-3 terms.
    for term in ("ǎ", "é", "ruǎn", "Привет", "λόγος", "東京", "ab", "é2", "x"):
        add(term)

    # Source-only / quoted regression cases.
    for term in ('"tetragram of the"', '"17th tetragram of the"',
                 '"17th tetragram"', '"tetragram"', '"country"', '"trash"'):
        add(term)

    return queries[:500]


def run(path: Path) -> None:
    queries = json.loads(QUERIES.read_text(encoding="utf-8"))
    with path.open("w", encoding="utf-8") as handle:
        for query in queries:
            primary = db.search(query, limit=25, part="primary")[0]
            full = db.search(query, limit=100, part="all")[0]

            def norm(entries):
                return [
                    {
                        "s": entry.get("simplified"),
                        "t": entry.get("traditional"),
                        "p": entry.get("pinyin"),
                    }
                    for entry in entries
                ]

            handle.write(
                json.dumps(
                    {"q": query, "primary": norm(primary), "full": norm(full)},
                    ensure_ascii=False,
                )
                + "\n"
            )


def compare(old: Path, new: Path, report: Path) -> int:
    old_rows = [json.loads(line) for line in old.open(encoding="utf-8")]
    new_rows = [json.loads(line) for line in new.open(encoding="utf-8")]
    if len(old_rows) != len(new_rows):
        print(f"row count differs: {len(old_rows)} vs {len(new_rows)}")
        return 1
    differences = 0
    lines: list[str] = []
    for before, after in zip(old_rows, new_rows):
        if before["q"] != after["q"]:
            lines.append(f"query order differs: {before['q']} vs {after['q']}")
            differences += 1
            continue
        for tier in ("primary", "full"):
            b = before[tier]
            a = after[tier]
            if b == a:
                continue
            differences += 1
            missing = [x for x in b if x not in a]
            extra = [x for x in a if x not in b]
            lines.append(
                f"{before['q']!r} {tier}: old={len(b)} new={len(a)} "
                f"missing={len(missing)} extra={len(extra)}"
            )
            if missing:
                lines.append(f"  missing: {missing[:5]}")
            if extra:
                lines.append(f"  extra: {extra[:5]}")
            # Check shared rows preserve order.
            shared_old = [x for x in b if x in a]
            shared_new = [x for x in a if x in b]
            if shared_old != shared_new:
                lines.append("  shared-row order differs")
    report.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"differences: {differences}; report: {report}")
    return 1 if differences else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("write", "run", "compare"))
    parser.add_argument("mode", nargs="?", choices=("legacy", "new"))
    args = parser.parse_args()
    if args.action == "write":
        DATA.mkdir(parents=True, exist_ok=True)
        queries = make_queries()
        QUERIES.write_text(
            json.dumps(queries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"wrote {len(queries)} queries to {QUERIES}")
    elif args.action == "run":
        if not args.mode:
            raise SystemExit("run needs legacy or new")
        out = DATA / f"golden_{args.mode}.jsonl"
        run(out)
        print(f"wrote {out}")
    else:
        raise SystemExit(
            compare(
                DATA / "golden_legacy.jsonl",
                DATA / "golden_new.jsonl",
                DATA / "golden_diff.txt",
            )
        )


if __name__ == "__main__":
    main()
