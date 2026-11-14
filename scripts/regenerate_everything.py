#!/usr/bin/env python3
"""Regenerate every pregenerated file and optimization, so nothing is left stale.

    python3 scripts/regenerate_everything.py                 # rebuild whatever is stale, then verify all
    python3 scripts/regenerate_everything.py --check         # only report (exit 1 when something is stale)
    python3 scripts/regenerate_everything.py --dry-run       # say what would be rebuilt, change nothing
    python3 scripts/regenerate_everything.py --force         # rebuild everything, fresh or not
    python3 scripts/regenerate_everything.py --only taiwan_readings main_readings
    python3 scripts/regenerate_everything.py --skip result_cache      # same as --no-result-cache
    python3 scripts/regenerate_everything.py --list          # the steps, in order

What it does, in dependency order (a later step reads what an earlier one wrote):
  1. asks every step, in a fresh process, whether its file is current (the app's own staleness checks:
     source-database stamps, code signatures, overrides.py, ...);
  2. skips the current ones; rebuilds each stale one by running its builder in
     scripts/generate-indexes-optimizations/, logging every line it prints;
  3. when a step was rebuilt, asks again about the steps after it (a rebuilt input makes them stale);
  4. at the end checks everything once more and fails if anything is still stale.

It never downloads or imports a dictionary: that is scripts/download-dictionaries/ (every importer ends
by running this script). Every line is also written to archives/logs/regenerate_everything-<time>.log.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "lib"))

GEN = HERE / "generate-indexes-optimizations"
OUT = ROOT / "data" / "generated-indexes-optimizations"


@dataclass
class Step:
    name: str
    script: str  # file in generate-indexes-optimizations/
    what: str  # one line for the log
    outputs: list[Path] = field(default_factory=list)  # for the size/time report
    optional: bool = False  # slow, and results are correct without it (only first searches are slower)
    args: list[str] = field(default_factory=list)


# In dependency order. `probe` (below) knows how to tell whether each is current.
STEPS: list[Step] = [
    Step("entries", "build_entries.py", "the unified backbone: CEDICT's tables plus a row per (word, reading) of every other dictionary", [OUT / "entries.sqlite"]),
    Step("head_grams", "build_head_grams.py", "character-gram index of every headword (exact negative filter)", [OUT / "head_grams.sqlite"]),
    Step("source_gloss", "build_source_gloss.py", "the gloss text of every source, for definition search", [OUT / "source_gloss.sqlite"]),
    Step("sense_counts", "build_sense_counts.py", "CEDICT senses per (word, reading): what ranks a word's readings", [OUT / "sense_counts.sqlite"]),
    Step("english_words", "import_english_words.py", "every English word in the definitions (the 'search the English word' hint)", [OUT / "english_words.sqlite"]),
    Step("gram_words", "import_gram_words.py", "gram index of headwords (exact negative filter)", [OUT / "gram_words.sqlite"]),
    Step("definition_grams", "import_definition_grams.py", "gram index of definitions (exact negative filter)", [OUT / "definition_grams.sqlite"]),
    Step("taiwan_readings", "build_taiwan_readings.py", "the Taiwan reading of every dictionary row", [OUT / "taiwan_readings.sqlite"]),
    Step("main_readings", "build_main_readings.py", "the fixed main reading and Taiwan reading of every headword", [OUT / "main_readings.sqlite"]),
    Step("headwords", "build_headwords.py", "frequency rank and HSK level of every word form", [OUT / "headwords.sqlite"]),
    Step("search_index", "build_search_index.py", "the sorted '*' headword list and the sources that define each word", [OUT / "search_index.sqlite"]),
    Step("segmenter", "build_segmenter.py", "the text parser's word list and its cache", [OUT / "segmenter.pickle", ROOT / "data" / "dl-base-dictionaries" / "cedict_userdict.txt"]),
    Step("static_assets", "build_static.py", "minified page scripts and stylesheet (esbuild)", [ROOT / "yabai_search" / "static" / "min" / "manifest.json"]),
    Step("result_cache", "build_result_cache.py", "ranked lists of the heaviest searches (top 1000 searches, about 5 minutes; --searches in the script raises it)", [OUT / "result_cache.sqlite"], optional=True),
]
BY_NAME = {step.name: step for step in STEPS}


# --------------------------------------------------------------------------------------------------
# Probing: runs in a FRESH process (`--_probe`), because the app caches its answers (a check that said
# "stale" before a rebuild would say it again after it).
# --------------------------------------------------------------------------------------------------
def probe(names: list[str]) -> dict[str, dict[str, str]]:
    sys.path.insert(0, str(ROOT))
    from yabai_search import db  # noqa: PLC0415

    def newer_than(outputs: list[Path], inputs: list[Path]) -> tuple[str, str]:
        missing = [p.name for p in outputs if not p.exists()]
        if missing:
            return "missing", "missing: " + ", ".join(missing)
        built = min(p.stat().st_mtime for p in outputs)
        changed = [p.name for p in inputs if p.exists() and p.stat().st_mtime > built]
        if changed:
            return "stale", "older than " + ", ".join(changed)
        return "current", "newer than all its inputs"

    def app_check(function: str) -> tuple[str, str]:
        if not getattr(db, function)():
            return "stale", f"db.{function}() says the file does not match its sources/code"
        return "current", f"db.{function}() ok"

    def definition_grams() -> tuple[str, str]:
        db._definition_grams_cache = None
        return ("current", "gram index matches its sources") if db._definition_grams() else ("stale", "gram index unusable or older than its sources")

    def segmenter() -> tuple[str, str]:
        from yabai_search import parser as segmenter_module  # noqa: PLC0415

        if segmenter_module.cache_current() and not segmenter_module.sources_changed():
            return "current", "cache matches the word list and sources"
        return "stale", "cache or word list does not match the sources"

    def static_assets() -> tuple[str, str]:
        static = ROOT / "yabai_search" / "static"
        manifest_path = static / "min" / "manifest.json"
        if not manifest_path.exists():
            return "missing", "min/manifest.json is missing"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        names = ["app.js", "custom_dicts.js", "glyphs.js", "inline_furigana.js", "hosts.js", "style.css"]
        changed = [n for n in names if manifest.get(n) != hashlib.sha256((static / n).read_bytes()).hexdigest()]
        return ("stale", "source changed since the minified copy: " + ", ".join(changed)) if changed else ("current", "manifest matches every source")

    def result_cache() -> tuple[str, str]:
        import sqlite3  # noqa: PLC0415

        from yabai_search import expensive_search_cache, services  # noqa: PLC0415

        path = expensive_search_cache.SHIPPED_PATH
        if not path.exists():
            return "missing", "result_cache.sqlite is missing"
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                row = connection.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
            finally:
                connection.close()
        except sqlite3.Error as error:
            return "stale", f"unreadable ({error})"
        if row and row[0] == services.search_version():
            return "current", "built for the current data and code version"
        return "stale", "built for another data or code version"

    gen = db.GENERATED_DIR
    checks = {
        "head_grams": lambda: app_check("_head_grams_version_ok"),
        "source_gloss": lambda: app_check("_source_gloss_version_ok"),
        "sense_counts": lambda: app_check("_sense_counts_ok"),
        "english_words": lambda: newer_than(
            [gen / "english_words.sqlite"],
            [db.DB_PATH, db.MOE_DB_PATH, db.WIKTIONARY_DB_PATH, GEN / "import_english_words.py"],
        ),
        "gram_words": lambda: app_check("_gram_words_version_ok"),
        "definition_grams": definition_grams,
        "taiwan_readings": lambda: (("current", "signature matches") if db.taiwan_table_status() == "ok" else (db.taiwan_table_status(), "signature does not match the sources/rules")),
        "main_readings": lambda: (("current", "signature matches") if db.main_readings_table_status() == "ok" else (db.main_readings_table_status(), "signature does not match the sources/rules")),
        "headwords": lambda: app_check("_headwords_ok"),
        "entries": lambda: app_check("_entries_ok"),
        "search_index": lambda: app_check("_search_index_ok"),
        "segmenter": segmenter,
        "static_assets": static_assets,
        "result_cache": result_cache,
    }
    result: dict[str, dict[str, str]] = {}
    for name in names:
        try:
            state, detail = checks[name]()
        except Exception as error:  # a missing file or table is as good as stale
            state, detail = "missing", f"{type(error).__name__}: {error}"
        result[name] = {"state": state, "detail": detail}
    return result


def probe_fresh(names: list[str], log) -> dict[str, dict[str, str]]:
    """Run `probe` in a new Python process and return its answers."""
    started = time.time()
    done = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--_probe", *names],
        cwd=ROOT, capture_output=True, text=True,
    )
    for line in done.stderr.splitlines():
        if line.strip() and "warning" not in line.lower():
            log.info(f"  (probe) {line}")
    try:
        answers = json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        log.error(f"the staleness probe failed (exit {done.returncode}):\n{done.stdout}\n{done.stderr}")
        return {n: {"state": "missing", "detail": "probe failed"} for n in names}
    log.info(f"  probed {len(names)} file(s) in {time.time() - started:.1f}s")
    return answers


def describe(paths: list[Path]) -> str:
    from runlog import human_size  # noqa: PLC0415

    parts = []
    for path in paths:
        if path.exists():
            parts.append(f"{path.name} {human_size(path.stat().st_size)} ({time.strftime('%m-%d %H:%M', time.localtime(path.stat().st_mtime))})")
        else:
            parts.append(f"{path.name} (absent)")
    return "; ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only report; exit 1 when a required file is stale")
    parser.add_argument("--dry-run", action="store_true", help="say what would be rebuilt, change nothing")
    parser.add_argument("--force", action="store_true", help="rebuild the selected steps even when current")
    parser.add_argument("--only", nargs="+", metavar="NAME", help="just these steps")
    parser.add_argument("--skip", nargs="+", metavar="NAME", default=[], help="leave these steps out")
    parser.add_argument("--no-result-cache", action="store_true", help="leave out the slow result cache")
    parser.add_argument("--strict", action="store_true", help="with --check, a stale result cache also fails")
    parser.add_argument("--list", action="store_true", help="print the steps and exit")
    parser.add_argument("--quiet", "-q", action="store_true", help="only steps and results on screen (the log file keeps everything)")
    parser.add_argument("--_probe", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args._probe is not None:  # child mode: print the states as one JSON line
        print(json.dumps(probe(args._probe)))
        return 0
    if args.list:
        for number, step in enumerate(STEPS, 1):
            print(f"{number:2}. {step.name:17} {step.script:30} {'(optional, slow) ' if step.optional else ''}{step.what}")
        return 0

    # Re-run under `nix develop` when the project's libraries are not importable here.
    from envsetup import ensure_project_environment  # noqa: PLC0415

    ensure_project_environment(str(Path(__file__).resolve()), sys.argv[1:])
    from runlog import Log, human_time  # noqa: PLC0415

    log = Log("regenerate_everything", quiet=args.quiet)
    log.header("regenerate_everything: bring every pregenerated file up to date")

    unknown = [n for n in (args.only or []) + args.skip if n not in BY_NAME]
    if unknown:
        log.error(f"unknown step(s): {', '.join(unknown)}; known: {', '.join(BY_NAME)}")
        log.close(2)
        return 2
    skipped = set(args.skip) | ({"result_cache"} if args.no_result_cache else set())
    selected = [s for s in STEPS if (not args.only or s.name in args.only) and s.name not in skipped]
    mode = "check only" if args.check else "dry run" if args.dry_run else "rebuild stale" + (" (forced)" if args.force else "")
    log.kv("mode", mode)
    log.kv("steps", ", ".join(s.name for s in selected))
    if skipped:
        log.kv("left out", ", ".join(sorted(skipped)))

    log.step("1/3 which files are stale?")
    states = probe_fresh([s.name for s in selected], log)
    for step in selected:
        answer = states[step.name]
        marker = {"current": "ok   ", "stale": "STALE", "missing": "MISS "}.get(answer["state"], "?    ")
        log.info(f"  {marker} {step.name:17} {answer['detail']}")

    rebuilt: list[str] = []
    failed: str | None = None
    if not (args.check or args.dry_run):
        log.step("2/3 rebuilding")
    elif args.dry_run:
        log.step("2/3 dry run (nothing will be changed)")
    dirty = False  # a step was rebuilt, so later answers may have changed
    for number, step in enumerate(selected, 1):
        if args.check:
            break
        if dirty and step.name not in rebuilt:
            now = probe_fresh([step.name], log)[step.name]
            if now["state"] != states[step.name]["state"]:
                log.info(f"  {step.name}: state after the earlier rebuild is '{now['state']}' ({now['detail']})")
            states[step.name] = now
        state = states[step.name]
        if state["state"] == "current" and not args.force:
            log.skip(f"[{number}/{len(selected)}] {step.name}: current, skipped ({state['detail']})")
            continue
        reason = "forced" if state["state"] == "current" else f"{state['state']}: {state['detail']}"
        log.step(f"[{number}/{len(selected)}] {step.name}: {step.what}")
        log.kv("why", reason)
        log.kv("builder", GEN / step.script)
        log.kv("before", describe(step.outputs))
        if args.dry_run:
            log.info("  dry run: would rebuild this")
            continue
        code, seconds = log.run([sys.executable, str(GEN / step.script), *step.args], prefix=step.name)
        if code:
            log.error(f"{step.name} FAILED (exit {code}) after {human_time(seconds)}; later steps were not run")
            failed = step.name
            break
        log.ok(f"{step.name} rebuilt in {human_time(seconds)}")
        log.kv("after", describe(step.outputs))
        rebuilt.append(step.name)
        dirty = True

    log.step("3/3 verification" if not (args.check or args.dry_run) else "3/3 result (nothing was changed)")
    final = states if (args.check or args.dry_run) else probe_fresh([s.name for s in selected], log)
    required_bad = []
    optional_bad = []
    for step in selected:
        answer = final[step.name]
        ok = answer["state"] == "current"
        marker = "ok   " if ok else answer["state"].upper()[:5].ljust(5)
        log.info(f"  {marker} {step.name:17} {answer['detail']}")
        if not ok:
            (optional_bad if step.optional else required_bad).append(step.name)
    log.kv("rebuilt this run", ", ".join(rebuilt) or "nothing")
    if required_bad:
        log.error(f"STALE: {', '.join(required_bad)}")
    if optional_bad:
        (log.error if args.strict else log.warn)(f"optional and stale: {', '.join(optional_bad)} (first searches are slower until it is rebuilt)")
    if args.dry_run:
        code = 0  # a dry run reports; finding stale files is its job, not a failure
    else:
        code = 1 if failed or required_bad or (args.strict and optional_bad) else 0
    log.close(code)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
