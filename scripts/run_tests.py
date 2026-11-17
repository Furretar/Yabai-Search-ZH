#!/usr/bin/env python3
"""Run every test of the project, with a detailed log of what is happening.

    python3 scripts/run_tests.py                      # the whole Python suite, then the JavaScript tests
    python3 scripts/run_tests.py -k taiwan            # only tests whose name matches
    python3 scripts/run_tests.py tests/test_parser.py # only this file (or tests/test_x.py::test_name)
    python3 scripts/run_tests.py --fast               # stop at the first failure
    python3 scripts/run_tests.py --no-js              # leave out the node tests
    python3 scripts/run_tests.py --no-preflight       # do not check the generated files first

What it does:
  1. preflight: environment, test-file inventory, and whether the generated files are current (a stale
     minified-assets manifest fails tests/test_ui_contracts.py; this tells you before the run);
  2. pytest -v, with every test result logged the moment it finishes and a heartbeat while a slow test runs;
  3. the node tests in scripts/tools/ (test_hosts.mjs, test_custom_search.mjs, test_dictionary_sections.mjs);
  4. a summary: counts, every failed test, the slowest tests.

Every line is also written to archives/logs/run_tests-<time>.log. The exit code is 0 only when everything passed.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "lib"))

JS_TESTS = [
    HERE / "tools" / "test_hosts.mjs",
    HERE / "tools" / "test_custom_search.mjs",
    HERE / "tools" / "test_dictionary_sections.mjs",  # needs the dev site + chromium, skips without
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", help="test files or node ids (default: all of tests/ and yabai_search/tests/)")
    parser.add_argument("-k", dest="expression", help="only tests whose name matches this pytest expression")
    parser.add_argument("--fast", "-x", action="store_true", help="stop at the first failure")
    parser.add_argument("--no-js", action="store_true", help="leave out the node tests")
    parser.add_argument("--no-preflight", action="store_true", help="skip the generated-files check")
    parser.add_argument("--durations", type=int, default=15, help="how many slowest tests to list (default 15)")
    parser.add_argument("--pytest-args", nargs=argparse.REMAINDER, default=[], help="everything after this goes to pytest")
    args = parser.parse_args()

    from envsetup import ensure_project_environment  # noqa: PLC0415

    ensure_project_environment(str(Path(__file__).resolve()), sys.argv[1:])
    from runlog import Log, human_time  # noqa: PLC0415

    log = Log("run_tests")
    log.header("run_tests: the project's test suite")

    # ---- 1. preflight -------------------------------------------------------------------------
    log.step("1/4 preflight")
    test_files = sorted((ROOT / "tests").glob("test_*.py")) + sorted((ROOT / "yabai_search" / "tests").glob("test_*.py"))
    log.kv("python test files", f"{len(test_files)} ({sum(p.stat().st_size for p in test_files) // 1024} KB)")
    log.kv("settings module", "yabai_site.test_settings (pyproject.toml)")
    log.kv("node", shutil.which("node") or "not found (JS tests will be skipped)")
    for module in ("django", "pytest", "opencc", "jieba"):
        try:
            imported = __import__(module)
            log.kv(f"import {module}", getattr(imported, "__version__", "ok"))
        except Exception as error:
            log.error(f"cannot import {module}: {error}")
            log.close(2)
            return 2
    if not args.no_preflight:
        code, _ = log.run([sys.executable, str(HERE / "regenerate_everything.py"), "--check", "--quiet"], prefix="generated files")
        if code:
            log.warn("some generated files are stale: tests that read them (test_ui_contracts, the index tests) may fail;")
            log.warn("run `python3 scripts/regenerate_everything.py` to bring them up to date")
        else:
            log.ok("every required generated file is current")

    # ---- 2. pytest ----------------------------------------------------------------------------
    log.step("2/4 python tests (pytest)")
    command = [sys.executable, "-m", "pytest", "-v", "-ra", "-p", "no:cacheprovider", "--color=no",
               f"--durations={args.durations}", "--durations-min=0.5"]
    if args.fast:
        command.append("-x")
    if args.expression:
        command += ["-k", args.expression]
    command += args.pytest_args + (args.paths or [])
    results = {"PASSED": 0, "FAILED": 0, "ERROR": 0, "SKIPPED": 0, "XFAIL": 0, "XPASS": 0}
    failed: list[str] = []
    slow: list[str] = []
    summary_line = ""
    started = time.time()

    # Count results from the log lines as they stream (the logger hands every line to this wrapper).
    original_emit = log._emit

    def counting_emit(level: str, message: str) -> None:
        nonlocal summary_line
        original_emit(level, message)
        if level != "OUT":
            return
        text = re.sub(r"^\[[^\]]*\]\s*", "", message)
        match = re.search(r"\s(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b", text)
        if match and "::" in text and not text.startswith(("FAILED ", "ERROR ", "SKIPPED ", "PASSED ")):
            results[match.group(1)] += 1
        if text.startswith(("FAILED ", "ERROR ")) and "::" in text:
            failed.append(text.split(" - ")[0])
        if re.match(r"^\s*\d+\.\d+s (call|setup|teardown)\s", text):
            slow.append(text.strip())
        if re.search(r"=+ .*(passed|failed|error).* in [\d.]+s", text):
            summary_line = text.strip("= ").strip()

    log._emit = counting_emit  # type: ignore[method-assign]
    pytest_code, _ = log.run(command, prefix="pytest", heartbeat=20)
    log._emit = original_emit  # type: ignore[method-assign]
    log.kv("pytest finished in", human_time(time.time() - started))

    # ---- 3. node tests ------------------------------------------------------------------------
    log.step("3/4 javascript tests (node)")
    js_failed: list[str] = []
    if args.no_js or args.paths or args.expression:
        log.skip("skipped (--no-js, or a subset of the Python tests was chosen)")
    elif not shutil.which("node"):
        log.warn("node is not installed: the JS tests were not run")
    else:
        for script in JS_TESTS:
            code, seconds = log.run(["node", str(script)], prefix=script.name)
            (log.ok if code == 0 else log.error)(f"{script.name}: {'passed' if code == 0 else f'FAILED (exit {code})'} in {human_time(seconds)}")
            if code:
                js_failed.append(script.name)

    # ---- 4. summary ---------------------------------------------------------------------------
    log.step("4/4 summary")
    log.kv("pytest", summary_line or f"exit code {pytest_code}")
    log.kv("counted from the log", ", ".join(f"{n} {k.lower()}" for k, n in results.items() if n))
    for entry in dict.fromkeys(failed):
        log.error(f"  failed: {entry}")
    for name in js_failed:
        log.error(f"  failed: node {name}")
    if slow:
        log.info(f"  slowest ({min(len(slow), 8)} shown):")
        for line in slow[:8]:
            log.info(f"    {line}")
    code = 0 if pytest_code == 0 and not js_failed else 1
    (log.ok if code == 0 else log.error)("ALL TESTS PASSED" if code == 0 else "SOME TESTS FAILED")
    log.close(code)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
