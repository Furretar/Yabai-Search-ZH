"""Show where one word survives or drops out of a search, stage by stage.

    python -m yabai_search.trace_query zhi 纸
    python -m yabai_search.trace_query zhi 只 --page-size 25

Stages (the same calls /api/search makes for the complete request):
  1. data     the raw CEDICT rows for the word (pinyin, toneless key, frequency)
  2. search   db.search(query) - was the word matched and ranked at all?
  3. expand   main._expand_variants - was it collapsed into / redirected to another row?
  4. page     which result page it lands on, and what the page-1 rows are
"""

from __future__ import annotations

import argparse

from yabai_search import db


def _label(entry: dict) -> str:
    freq = entry.get("freq") or {}
    return (
        f"{entry.get('traditional')}/{entry.get('simplified')} "
        f"{entry.get('pinyin', '')!s:<8} main={entry.get('main') or '-'} "
        f"score={entry.get('reading_score', '-')} "
        f"freq={freq.get('rank', '-')} forms={[f['traditional'] for f in entry.get('forms', [])]}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("query", help="the search, e.g. zhi")
    parser.add_argument("word", help="the headword to follow, e.g. 纸 or 紙")
    parser.add_argument("--page-size", type=int, default=25)
    args = parser.parse_args()
    word, trad, simp = args.word, db.to_traditional(args.word), db.to_simplified(args.word)
    forms = {word, trad, simp}

    print(f"== 1. data: CEDICT rows for {sorted(forms)}")
    connection = db.connect()
    try:
        rows = connection.execute(
            "SELECT id, traditional, simplified, pinyin, pinyin_toneless, "
            f"{db.FREQ_RANK_COLUMN} AS rank FROM entries "
            f"WHERE simplified IN ({','.join('?' * len(forms))}) "
            f"OR traditional IN ({','.join('?' * len(forms))})",
            tuple(forms) * 2,
        ).fetchall()
        variants = connection.execute(
            "SELECT variant_trad, main_trad, pinyin, redirect FROM forms "
            f"WHERE variant_trad IN ({','.join('?' * len(forms))}) "
            f"OR main_trad IN ({','.join('?' * len(forms))})",
            tuple(forms) * 2,
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        print("  entry ", tuple(row))
    for row in variants:
        print("  forms ", tuple(row))
    if not rows:
        print("  (no CEDICT row at all: the word is not in the entries table)")

    entries, pinyin_match = db.search(args.query, db.UNBOUNDED, False, hydrate=0)
    hits = [e for e in entries if forms & {e["traditional"], e["simplified"]}]
    print(f"\n== 2. search: {len(entries)} rows (pinyin_match={pinyin_match}); "
          f"{len(hits)} are the word")
    for entry in hits:
        print("  ", _label(entry))
    if not hits:
        print("  -> never matched. Compare the query with pinyin_toneless above.")

    from yabai_search.services import _expand_variants

    expanded = _expand_variants(entries)
    positions = [
        (i, e) for i, e in enumerate(expanded) if forms & {e["traditional"], e["simplified"]}
    ]
    print(f"\n== 3. expand: {len(expanded)} rows after variant redirect + de-duplication")
    for i, entry in positions:
        print(f"   row {i + 1} (page {i // args.page_size + 1}):", _label(entry))
    if hits and not positions:
        print("  -> DROPPED in _expand_variants: its redirect target or a duplicate "
              "key replaced it. Look at main= above.")

    print(f"\n== 4. page 1 ({args.page_size} rows)")
    for entry in expanded[: args.page_size]:
        print("  ", _label(entry))


if __name__ == "__main__":
    main()
