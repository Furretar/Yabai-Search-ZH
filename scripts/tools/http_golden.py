#!/usr/bin/env python3
"""Run the golden query set through the HTTP API and compare responses."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
QUERIES = ROOT / "data" / "work" / "golden_queries.json"


def norm(entry):
    return {
        "s": entry.get("simplified"),
        "t": entry.get("traditional"),
        "p": entry.get("pinyin"),
    }


def run(base: str, path: Path) -> None:
    queries = json.loads(QUERIES.read_text(encoding="utf-8"))
    with path.open("w", encoding="utf-8") as handle:
        for query in queries:
            record = {"q": query}
            for part in ("primary", "full"):
                params = {"q": query, "limit": 25}
                if part == "primary":
                    params["part"] = "primary"
                url = base + "/api/search?" + urllib.parse.urlencode(params)
                with urllib.request.urlopen(url, timeout=30) as response:
                    data = json.load(response)
                record[part] = {
                    "ids": [norm(e) for e in data.get("results", [])],
                    "total": data.get("total"),
                    "fuzzy": [norm(e) for e in data.get("fuzzy", [])],
                    "pinyin_match": data.get("pinyin_match"),
                }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def compare(old: Path, new: Path) -> int:
    old_rows = [json.loads(line) for line in old.open(encoding="utf-8")]
    new_rows = [json.loads(line) for line in new.open(encoding="utf-8")]
    differences = []
    for before, after in zip(old_rows, new_rows):
        if before != after:
            differences.append(
                {
                    "q": before["q"],
                    "old": before,
                    "new": after,
                }
            )
    out = ROOT / "data" / "work" / "http_golden_diff.json"
    out.write_text(json.dumps(differences, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"differences: {len(differences)}; report: {out}")
    return 1 if differences else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "compare"))
    parser.add_argument("--base")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--old", type=Path)
    parser.add_argument("--new", type=Path)
    args = parser.parse_args()
    if args.action == "run":
        if not args.base or not args.out:
            raise SystemExit("run needs --base and --out")
        run(args.base, args.out)
        print(f"wrote {args.out}")
    else:
        raise SystemExit(compare(args.old, args.new))


if __name__ == "__main__":
    main()
