#!/usr/bin/env python3
"""Prebuild the ranked result lists of the heaviest searches (result_cache.sqlite).

A single character or a pinyin syllable matches thousands of rows, and a new query ranks all of
them: under a second on the laptop, 4 to 10 seconds on a tablet. This runs those queries once, here,
with each search language's default dictionaries, and ships the lists. The app reads a list only
when its key (query, dictionaries, data version, code version) matches exactly, so a stale file is
never wrong, only useless: rebuild it after any data or search-code change (regenerate_everything.py does).

Queries: the most frequent single characters (both scripts) and every pinyin syllable, with and
without its tone number, ranked by how searched they are (a syllable counts as common as the most
common character that has it). Only lists that took at least --min-ms to compute are kept, and of
those the most searched ones that fit in --max-mb.

Usage (from the project root):
    uv run python scripts/generate-indexes-optimizations/build_result_cache.py [--chars 1000] [--max-mb 200] [--jobs 12]
"""
from __future__ import annotations

import argparse
import re
import multiprocessing
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "django"))  # the Django project (yabai_site) lives in ./django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "yabai_site.settings")
os.environ.setdefault("DJANGO_SECRET_KEY", "build-only")
os.environ.setdefault("YABAI_WARM_LOOKUPS", "0")
os.environ.setdefault("YABAI_WARM_PAGE_CACHE", "0")
os.environ.setdefault("YABAI_SQL_LOG", "0")

# The corpus rank of a word nobody has seen yet: it sorts last.
_NO_RANK = 10**9


def queries(chars: int) -> list[tuple[str, int]]:
    """The searches to prebuild, most searched first, as (query, rank).

    The rank is the position in that order, and the size budget keeps the first ones, so what
    is shipped is what people ask for. A character is ranked by its corpus frequency; a pinyin
    syllable by the most common character that has it, so the two kinds interleave the way
    people search them.
    """
    from yabai_search import db  # noqa: PLC0415

    scored: dict[str, int] = {}

    def add(text: str, score: int) -> None:
        """Remember the query with its best score (the lowest corpus rank)."""
        text = (text or "").strip()
        if not text:
            return
        if text not in scored or score < scored[text]:
            scored[text] = score

    connection = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
    try:
        column = db.FREQ_RANK_COLUMN
        for simplified, traditional, rank in connection.execute(
            "SELECT simplified, traditional, "
            f"MIN({column}) FROM entries WHERE length(simplified) = 1 "
            f"GROUP BY simplified, traditional ORDER BY MIN({column}) IS NULL, MIN({column}) LIMIT ?",
            (chars,),
        ):
            score = _NO_RANK if rank is None else int(rank)
            add(simplified, score)
            add(traditional, score)
        # A syllable is as common as the most common character that has it, and its tone form
        # as common as the syllable: the score is the best of the group, not of one row.
        syllable_scores: dict[str, int] = {}
        for toneless, numeric, rank in connection.execute(
            "SELECT pinyin_toneless, lower(pinyin_numeric), "
            f"MIN({column}) FROM entries WHERE length(simplified) = 1 "
            "GROUP BY pinyin_toneless, lower(pinyin_numeric)"
        ):
            score = _NO_RANK if rank is None else int(rank)
            if toneless and toneless.isalpha():
                if toneless not in syllable_scores or score < syllable_scores[toneless]:
                    syllable_scores[toneless] = score
            if numeric and " " not in numeric and numeric[-1:].isdigit():
                add(numeric, score)
        for toneless, score in syllable_scores.items():
            add(toneless, score)
            # A one- or two-letter start of a syllable ("a", "sh") matches tens of thousands of
            # rows: these are the slowest searches (10-40 s cold), far slower than any whole syllable.
            for size in (1, 2):
                if len(toneless) > size:
                    add(toneless[:size], score)
    finally:
        connection.close()
    ranked = sorted(scored, key=lambda text: (scored[text], text))
    return [(text, rank) for rank, text in enumerate(ranked)]


def _start() -> None:
    import django  # noqa: PLC0415

    django.setup()
    from yabai_search import expensive_search_cache as result_cache  # noqa: PLC0415

    result_cache.capture = []
    # The build computes every list itself. A list that is already on disk (the serving
    # machine's own cache, or the file of an earlier build) would be handed back at once and
    # never captured, so the build would miss exactly the searches that were made before it.
    result_cache.LOCAL_PATH = Path("/nonexistent/result_cache_local.sqlite")
    result_cache.SHIPPED_PATH = Path("/nonexistent/result_cache_shipped.sqlite")


def _run(job: tuple[int, str, str, str]) -> tuple[int, list]:
    from yabai_search import expensive_search_cache as result_cache, services  # noqa: PLC0415

    rank, query, lang, dicts = job
    result_cache.capture.clear()
    try:
        services.api_search(
            services.HeaderBag(), q=query, limit=25, taiwan=False, offset=0, part="all",
            lang=lang, dicts=dicts,
        )
    except Exception as exc:  # noqa: BLE001  (one bad query must not stop the build)
        print(f"  skipped {query!r} ({lang}): {exc!r}", flush=True)
    return rank, list(result_cache.capture)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chars", type=int, default=1000)
    parser.add_argument("--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 2) // 2)),
                        help="worker processes (default: half the cores, at most 8, so the machine stays usable)")
    parser.add_argument("--scope", choices=["slow", "top"], default="slow",
                        help="slow (default): only the one- and two-letter pinyin starts, the slowest searches; "
                             "top: the --searches most searched queries of every kind")
    parser.add_argument("--searches", type=int, default=1000,
                        help="with --scope top: how many of the most searched queries to precompute (all three languages counted together)")
    parser.add_argument("--min-ms", type=float, default=60.0)
    parser.add_argument("--max-mb", type=float, default=200.0, help="size budget of the shipped file")
    args = parser.parse_args()
    try:
        os.nice(19)  # the workers inherit it: this build never competes with anything else
    except OSError:
        pass
    os.environ["YABAI_RESULT_CACHE_MIN_MS"] = str(args.min_ms)
    os.environ["YABAI_RESULT_CACHE_MB"] = "100000"

    import django  # noqa: PLC0415

    django.setup()
    from yabai_search import expensive_search_cache as result_cache, services  # noqa: PLC0415
    from yabai_search.default_config import DEFAULTS  # noqa: PLC0415

    words = queries(args.chars)
    jobs = [
        (rank, word, lang, ",".join(DEFAULTS[lang]))
        for lang in ("EN", "ZH", "TW")
        for word, rank in words
    ]
    jobs.sort(key=lambda job: job[0])   # most searched first
    if args.scope == "slow":
        jobs = [job for job in jobs if re.fullmatch(r"[a-z]{1,2}", job[1])]
    else:
        jobs = jobs[: args.searches]
    started = time.time()
    temp = result_cache.SHIPPED_PATH.with_suffix(".tmp")
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.unlink(missing_ok=True)
    out = sqlite3.connect(temp)
    # The rank of the query (how searched it is) is kept beside the list: the size budget
    # drops the least searched lists first.
    out.execute(
        "CREATE TABLE results (key TEXT PRIMARY KEY, at REAL NOT NULL, seconds REAL NOT NULL, "
        "query TEXT NOT NULL, size INTEGER NOT NULL, body BLOB NOT NULL, rank INTEGER NOT NULL)"
    )
    out.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    out.execute("INSERT INTO meta VALUES ('version', ?)", (services.search_version(),))
    kept = 0
    with multiprocessing.Pool(args.jobs, initializer=_start) as pool:
        for done, (rank, rows) in enumerate(pool.imap_unordered(_run, jobs, chunksize=8), start=1):
            for digest, seconds, query, blob in rows:
                out.execute(
                    "INSERT OR REPLACE INTO results (key, at, seconds, query, size, body, rank) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (digest, time.time(), seconds, query, len(blob), blob, rank),
                )
                kept += 1
            if done % 2000 == 0:
                out.commit()
                print(f"  {done:,}/{len(jobs):,} searches, {kept:,} kept", flush=True)
    out.commit()
    # Keep the most searched lists that fit the size budget: they are the ones worth shipping.
    budget = int(args.max_mb * 1024 * 1024)
    total = out.execute("SELECT COALESCE(SUM(size), 0) FROM results").fetchone()[0]
    dropped = 0
    if total > budget:
        for rowid, size in out.execute(
            "SELECT rowid, size FROM results ORDER BY rank DESC, seconds DESC"
        ).fetchall():
            out.execute("DELETE FROM results WHERE rowid = ?", (rowid,))
            total -= size
            dropped += 1
            if total <= budget:
                break
        out.commit()
        out.execute("VACUUM")
    kept -= dropped
    out.close()
    os.replace(temp, result_cache.SHIPPED_PATH)
    size = result_cache.SHIPPED_PATH.stat().st_size / 1e6
    print(
        f"result cache: {kept:,} lists from {len(jobs):,} searches, "
        f"{size:.0f} MB, {time.time() - started:.0f} s"
    )


if __name__ == "__main__":
    main()
