#!/usr/bin/env python3
"""Rebuild every derived index, or check that none is stale.

Run it after ANY source database changes (a dictionary import, a frequency list, overrides.py, the
Taiwan-reading logic in db.py). Every importer in scripts/create-dict-db/ calls it for you when it
finishes (skip with --no-indexes). The deploy scripts run `--check` first and refuse to ship stale
indexes: several of them are exact negative filters (definition_grams, gram_words, head_grams), so a
stale one hides results that exist.

    uv run python scripts/generate-indexes-optimizations/rebuild_all.py            # rebuild all, in order
    uv run python scripts/generate-indexes-optimizations/rebuild_all.py --only definition_grams gram_words
    uv run python scripts/generate-indexes-optimizations/rebuild_all.py --check    # exit 1 when one is stale

When you add an index builder or a source, add it to STEPS (and to the check, if the app can tell).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

# (name, script, app check or None), in dependency order: later steps read what earlier ones wrote.
STEPS = [
    ("head_grams", "build_head_grams.py", "_head_grams_version_ok"),
    ("source_gloss", "build_source_gloss.py", "_source_gloss_version_ok"),
    ("sense_counts", "build_sense_counts.py", "_sense_counts_ok"),
    ("english_words", "import_english_words.py", None),
    ("gram_words", "import_gram_words.py", "_gram_words_version_ok"),
    ("definition_grams", "import_definition_grams.py", "_definition_grams_ok"),
    ("taiwan_readings", "build_taiwan_readings.py", "_taiwan_ok"),
    ("search_index", "build_search_index.py", "_search_index_ok"),
    ("segmenter", "build_segmenter.py", "_segmenter_ok"),
]


# Slow to build (about ten minutes) and never needed for correct results, so not part of a normal
# rebuild: `--results` (or `--only result_cache`) builds it, and --check only reports its state.
OPTIONAL = [("result_cache", "build_result_cache.py")]


def result_cache_state() -> str:
    """ "current", "stale" (built for another data or code version) or "missing"."""
    import sqlite3  # noqa: PLC0415

    sys.path.insert(0, str(ROOT))
    from yabai_search import result_cache, services  # noqa: PLC0415

    if not result_cache.SHIPPED_PATH.exists():
        return "missing"
    try:
        connection = sqlite3.connect(f"file:{result_cache.SHIPPED_PATH}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return "stale"
    return "current" if row and row[0] == services.search_version() else "stale"


def check() -> int:
    """Print the state of every index the app can verify; exit status 1 when any is stale or missing."""
    sys.path.insert(0, str(ROOT))
    from yabai_search import db  # noqa: PLC0415  (needs the project's dependencies)

    def definition_grams_ok() -> bool:
        db._definition_grams_cache = None
        return bool(db._definition_grams())

    def segmenter_ok() -> bool:
        from yabai_search import segment  # noqa: PLC0415

        return segment.cache_current() and not segment.sources_changed()

    checks = {
        "_taiwan_ok": lambda: db.taiwan_table_status() == "ok",
        "_definition_grams_ok": definition_grams_ok,
        "_segmenter_ok": segmenter_ok,
    }
    bad = 0
    for name, _script, func in STEPS:
        if func is None:
            print(f"  {name:17s} (no check; rebuilt with the rest)")
            continue
        try:
            ok = checks[func]() if func in checks else bool(getattr(db, func)())
        except Exception as exc:  # a missing file or table is as good as stale
            print(f"  {name:17s} MISSING or unreadable ({exc})")
            bad += 1
            continue
        print(f"  {name:17s} {'ok' if ok else 'STALE'}")
        bad += 0 if ok else 1
    state = result_cache_state()
    note = "" if state == "current" else "  (optional; rebuild_all.py --results makes first searches fast again)"
    print(f"  {'result_cache':17s} {state}{note}")
    if bad:
        print(f"{bad} index(es) not current: run scripts/generate-indexes-optimizations/rebuild_all.py", file=sys.stderr)
    return 1 if bad else 0


def rebuild(only: list[str] | None, results: bool = False) -> int:
    names = {name for name, _s, _c in STEPS} | {name for name, _s in OPTIONAL}
    unknown = set(only or []) - names
    if unknown:
        print(f"unknown index: {', '.join(sorted(unknown))} (known: {', '.join(sorted(names))})", file=sys.stderr)
        return 2
    started = time.time()
    for name, script, _check in STEPS:
        if only and name not in only:
            continue
        print(f"== {name} ({time.strftime('%H:%M:%S')})", flush=True)
        result = subprocess.run([sys.executable, str(HERE / script)], cwd=ROOT)
        if result.returncode:
            print(f"FAILED: {script} (exit {result.returncode}); later indexes were not rebuilt", file=sys.stderr)
            return result.returncode
    for name, script in OPTIONAL:
        if not (results or (only and name in only)):
            continue
        print(f"== {name} ({time.strftime('%H:%M:%S')})", flush=True)
        result = subprocess.run([sys.executable, str(HERE / script)], cwd=ROOT)
        if result.returncode:
            return result.returncode
    print(f"indexes rebuilt in {time.time() - started:.0f} s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only report which indexes are stale")
    parser.add_argument("--only", nargs="+", metavar="NAME", help="rebuild just these (names as printed)")
    parser.add_argument("--results", action="store_true", help="also prebuild the heavy-search result cache (slow)")
    args = parser.parse_args()
    return check() if args.check else rebuild(args.only, args.results)


if __name__ == "__main__":
    sys.exit(main())
