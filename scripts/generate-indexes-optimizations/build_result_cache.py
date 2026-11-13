#!/usr/bin/env python3
"""Prebuild the ranked result lists of the heaviest searches (result_cache.sqlite).

A single character or a pinyin syllable matches thousands of rows, and a new query ranks all of
them: under a second on the laptop, 4 to 10 seconds on a tablet. This runs those queries once, here,
with each search language's default dictionaries, and ships the lists. The app reads a list only
when its key (query, dictionaries, data version, code version) matches exactly, so a stale file is
never wrong, only useless: rebuild it after any data or search-code change (rebuild_all.py does).

Queries: the most frequent single characters (both scripts) and every pinyin syllable, with and
without its tone number. Only lists that took at least --min-ms to
compute are kept, and of those the slowest that fit in --max-mb.

Usage (from the project root):
    uv run python scripts/generate-indexes-optimizations/build_result_cache.py [--chars 1000] [--max-mb 200] [--jobs 12]
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "yabai_site.settings")
os.environ.setdefault("DJANGO_SECRET_KEY", "build-only")
os.environ.setdefault("YABAI_WARM_LOOKUPS", "0")
os.environ.setdefault("YABAI_WARM_PAGE_CACHE", "0")
os.environ.setdefault("YABAI_SQL_LOG", "0")


def queries(chars: int) -> list[str]:
    from yabai_search import db  # noqa: PLC0415

    out: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        text = (text or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    connection = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
    try:
        column = db.FREQ_RANK_COLUMN
        for simplified, traditional in connection.execute(
            "SELECT simplified, traditional FROM entries WHERE length(simplified) = 1 "
            f"GROUP BY simplified, traditional ORDER BY MIN({column}) IS NULL, MIN({column}) LIMIT ?",
            (chars,),
        ):
            add(simplified)
            add(traditional)
        for toneless, numeric in connection.execute(
            "SELECT DISTINCT pinyin_toneless, lower(pinyin_numeric) FROM entries "
            "WHERE length(simplified) = 1"
        ):
            if toneless and toneless.isalpha():
                add(toneless)
            if numeric and " " not in numeric and numeric[-1:].isdigit():
                add(numeric)
    finally:
        connection.close()
    return out


def _start() -> None:
    import django  # noqa: PLC0415

    django.setup()
    from yabai_search import result_cache  # noqa: PLC0415

    result_cache.capture = []


def _run(job: tuple[str, str, str]) -> list:
    from yabai_search import result_cache, services  # noqa: PLC0415

    query, lang, dicts = job
    result_cache.capture.clear()
    try:
        services.api_search(
            services.HeaderBag(), q=query, limit=25, taiwan=False, offset=0, part="all",
            lang=lang, dicts=dicts,
        )
    except Exception as exc:  # noqa: BLE001  (one bad query must not stop the build)
        print(f"  skipped {query!r} ({lang}): {exc!r}", flush=True)
    return list(result_cache.capture)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chars", type=int, default=1000)
    parser.add_argument("--jobs", type=int, default=max(1, min(12, (os.cpu_count() or 2) - 2)))
    parser.add_argument("--min-ms", type=float, default=60.0)
    parser.add_argument("--max-mb", type=float, default=200.0, help="size budget of the shipped file")
    args = parser.parse_args()
    os.environ["YABAI_RESULT_CACHE_MIN_MS"] = str(args.min_ms)
    os.environ["YABAI_RESULT_CACHE_MB"] = "100000"

    import django  # noqa: PLC0415

    django.setup()
    from yabai_search import result_cache, services  # noqa: PLC0415
    from yabai_search.default_dictionaries import DEFAULTS  # noqa: PLC0415

    words = queries(args.chars)
    jobs = [
        (word, lang, ",".join(DEFAULTS[lang])) for lang in ("EN", "ZH", "TW") for word in words
    ]
    started = time.time()
    temp = result_cache.SHIPPED_PATH.with_suffix(".tmp")
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.unlink(missing_ok=True)
    out = sqlite3.connect(temp)
    out.execute(result_cache._SCHEMA)
    out.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    out.execute("INSERT INTO meta VALUES ('version', ?)", (services.search_version(),))
    kept = 0
    with multiprocessing.Pool(args.jobs, initializer=_start) as pool:
        for done, rows in enumerate(pool.imap_unordered(_run, jobs, chunksize=8), start=1):
            for digest, seconds, query, blob in rows:
                out.execute(
                    "INSERT OR REPLACE INTO results (key, at, seconds, query, size, body) "
                    "VALUES (?,?,?,?,?,?)",
                    (digest, time.time(), seconds, query, len(blob), blob),
                )
                kept += 1
            if done % 2000 == 0:
                out.commit()
                print(f"  {done:,}/{len(jobs):,} searches, {kept:,} kept", flush=True)
    out.commit()
    # Keep the slowest lists that fit the size budget: they are the ones worth shipping.
    budget = int(args.max_mb * 1024 * 1024)
    total = out.execute("SELECT COALESCE(SUM(size), 0) FROM results").fetchone()[0]
    dropped = 0
    if total > budget:
        for rowid, size in out.execute("SELECT rowid, size FROM results ORDER BY seconds").fetchall():
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
