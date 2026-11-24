#!/usr/bin/env python3
"""Deploy this checkout to the laptop serving copy, with a detailed log of every step.

    python3 scripts/deploy.py --dry-run             # show every step and command, change nothing
    python3 scripts/deploy.py                       # code only: tests, then the laptop clone pulls and restarts
    python3 scripts/deploy.py --data                # code and the pregenerated data files
    python3 scripts/deploy.py --yes --fast          # no question, no test run (the generated files are still checked)
    python3 scripts/deploy.py --method all --data   # the older direct path: deploy/deploy-all.sh (laptop AND tablet)

Steps, in order:
  1. preflight: the commit being deployed, a clean working tree (the laptop clone pulls COMMITS, so an
     uncommitted change would not ship), the laptop clone and its settings;
  2. regenerate_everything.py: every pregenerated file is brought up to date, because it ships with the
     code and a stale one hides results (skips whatever is already current);
  3. run_tests.py (skip with --fast);
  4. asks "deploy?" (skip with --yes; required when there is no terminal);
  5. with --data: copies the databases and generated files to the laptop clone with rsync;
  6. runs deploy/update.sh laptop inside the laptop clone: it pulls this checkout, restarts the app and
     goes back to the old version when the new one does not answer a health check;
  7. checks the laptop copy answers, and that a real search returns a sane first row.
The PC and the tablets follow ~/.links/projects/yabai-laptop through Syncthing, so deploying the laptop
clone is enough. Per-machine files (deploy/env.local, data/accounts.sqlite3) are never copied.
Every line is also written to archives/logs/deploy-<time>.log.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "lib"))


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def load_release_env() -> dict[str, str]:
    """deploy/release.env (shell-style assignments), the same file the shell scripts read."""
    values: dict[str, str] = {}
    path = ROOT / "deploy" / "release.env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = os.path.expandvars(value.strip().strip("'\"").replace("$HOME", str(Path.home())))
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", action="store_true", help="also copy the databases and generated files")
    parser.add_argument("--method", choices=["laptop", "all"], default="laptop", help="laptop (default) or the older deploy-all.sh")
    parser.add_argument("--fast", action="store_true", help="skip the test run")
    parser.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--dry-run", action="store_true", help="show every step and command, change nothing")
    parser.add_argument("--allow-dirty", action="store_true", help="deploy even with uncommitted changes (they will NOT ship)")
    parser.add_argument("--no-regenerate", action="store_true", help="only check the generated files, do not rebuild them")
    parser.add_argument("--no-result-cache", action="store_true", help="do not rebuild the precomputed slowest searches (the one- and two-letter pinyin starts)")
    parser.add_argument("--pc", action="store_true", help="with --method all: restart the Windows scheduled task")
    args = parser.parse_args()

    # Lowest CPU/disk priority for this run and every child (result cache workers, tests): the
    # deploy finishes the same when the machine is idle and gets out of the way when it is not.
    # YABAI_DEPLOY_NICE=0 turns it off.
    try:
        os.nice(int(os.environ.get("YABAI_DEPLOY_NICE", "19")))
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], capture_output=True)
    except (OSError, ValueError, FileNotFoundError):
        pass

    from envsetup import ensure_project_environment  # noqa: PLC0415

    ensure_project_environment(str(Path(__file__).resolve()), sys.argv[1:])
    from runlog import Log, human_size, human_time  # noqa: PLC0415

    log = Log("deploy")
    log.header("deploy: ship this checkout to the laptop serving copy")
    release = load_release_env()
    laptop = Path(release.get("LAPTOP_DIR") or Path.home() / "Desktop" / "ai" / "yabai-laptop")
    dry = args.dry_run

    # ---- 1. preflight -------------------------------------------------------------------------
    log.step("1/7 preflight")
    commit = git("rev-parse", "--short", "HEAD")
    log.kv("commit", f"{commit}  {git('log', '-1', '--format=%s')[:80]}")
    log.kv("branch", git("rev-parse", "--abbrev-ref", "HEAD"))
    dirty = git("status", "--porcelain")
    changed = len(dirty.splitlines())
    log.kv("working tree", "clean" if not dirty else f"{changed} uncommitted change(s)")
    log.kv("method", "deploy/update.sh in the laptop clone" if args.method == "laptop" else "deploy/deploy-all.sh (rsync of the working tree)")
    log.kv("data files", "copied (--data)" if args.data else "not copied (code only)")
    log.kv("laptop clone", f"{laptop} ({'exists' if laptop.exists() else 'MISSING'})")
    if args.method == "laptop":
        if dirty and not args.allow_dirty:
            log.error(f"{changed} uncommitted change(s): the laptop clone pulls commits, so they would not ship.")
            log.error("Commit first, or use --allow-dirty to deploy the last commit anyway. Nothing was changed.")
            for line in dirty.splitlines()[:15]:
                log.info(f"    {line}")
            log.close(2)
            return 2
        if dirty:
            log.warn("uncommitted changes will NOT be deployed (--allow-dirty)")
        if not laptop.exists():
            log.error("the laptop clone does not exist; see AGENTS.md, Deployment")
            log.close(2)
            return 2
        log.kv("laptop clone head", subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=laptop, capture_output=True, text=True).stdout.strip() or "?")

    # ---- 2. generated files -------------------------------------------------------------------
    log.step("2/7 generated files")
    regen = [sys.executable, str(HERE / "regenerate_everything.py")]
    if args.no_regenerate or dry:
        regen += ["--check"]
    if args.no_result_cache:
        regen += ["--no-result-cache"]
    code, _ = log.run(regen, prefix="regenerate")
    if code and not dry:
        log.error("generated files are not all current: not deploying (shipping a stale index hides results)")
        log.close(code)
        return code
    if code:
        log.warn("dry run: some generated files are stale; a real run would rebuild them first")

    # ---- 3. tests -----------------------------------------------------------------------------
    log.step("3/7 tests")
    if args.fast:
        log.skip("skipped (--fast)")
    elif dry:
        log.skip(f"dry run: would run {HERE / 'run_tests.py'}")
    else:
        code, _ = log.run([sys.executable, str(HERE / "run_tests.py"), "--no-preflight"], prefix="tests")
        if code:
            log.error("tests failed: not deploying")
            log.close(code)
            return code

    # ---- 4. confirmation ----------------------------------------------------------------------
    log.step("4/7 confirmation")
    if dry:
        log.skip("dry run: no question asked")
    elif args.yes:
        log.info("--yes: continuing without asking")
    elif sys.stdin.isatty():
        answer = input(f"Deploy {commit} to the laptop serving copy{' with data' if args.data else ''}? [y/N] ").strip().lower()
        log.info(f"answer: {answer or 'n'}")
        if answer not in ("y", "yes"):
            log.warn("cancelled; nothing was changed")
            log.close(1)
            return 1
    else:
        log.error("no terminal to ask on: pass --yes to deploy without a question")
        log.close(2)
        return 2

    def run_or_show(command: list[str], prefix: str, cwd: Path | None = None) -> int:
        if dry:
            log.info(f"[{prefix}] dry run, would run (in {cwd or ROOT}): {' '.join(shlex.quote(c) for c in command)}")
            return 0
        code, seconds = log.run(command, prefix=prefix, cwd=cwd, heartbeat=30)
        (log.ok if code == 0 else log.error)(f"{prefix}: {'done' if code == 0 else f'FAILED (exit {code})'} in {human_time(seconds)}")
        return code

    # ---- 5/6. the actual deploy ---------------------------------------------------------------
    if args.method == "all":
        log.step("5-6/7 deploy/deploy-all.sh")
        command = ["bash", str(ROOT / "deploy" / "deploy-all.sh"), "--fast"]  # tests already ran above
        if args.data:
            command.append("--data")
        if args.pc:
            command.append("--pc")
        code = run_or_show(command, "deploy-all")
        if code:
            log.close(code)
            return code
    else:
        log.step("5/7 copying the data files" if args.data else "5/7 data files")
        if not args.data:
            log.skip("not copied (no --data)")
        else:
            data = ROOT / "data"
            gen = data / "generated-indexes-optimizations"
            dicts = data / "dictionary-databases"
            copies = [
                # the plain databases (dictionaries, the frequency list and HSK), then the databases of the
                # dictionaries that have their own folder
                ([str(p) for p in sorted(dicts.glob("*.sqlite"))], laptop / "data" / "dictionary-databases"),
                ([str(dicts / "moe-taiwan" / "moe.sqlite")], laptop / "data" / "dictionary-databases" / "moe-taiwan"),
                ([str(dicts / "baidu" / "baidu_clean.sqlite")], laptop / "data" / "dictionary-databases" / "baidu"),
                ([str(p) for p in (data / "dl-base-dictionaries" / "cedict_userdict.txt", data / "dl-base-dictionaries" / "cedict_userdict.version") if p.exists()], laptop / "data" / "dl-base-dictionaries"),
                ([str(p) for p in sorted(gen.glob("*.sqlite"))] + [str(gen / "segmenter.pickle")], laptop / "data" / "generated-indexes-optimizations"),
            ]
            total = sum(Path(f).stat().st_size for files, _ in copies for f in files if Path(f).exists())
            log.kv("to copy", f"{sum(len(f) for f, _ in copies)} files, {human_size(total)} (rsync skips what is unchanged)")
            log.kv("never copied", "data/accounts.sqlite3, data/writer-epoch, deploy/env.local (per-machine)")
            for files, target in copies:
                if not files:
                    continue
                if not dry:
                    target.mkdir(parents=True, exist_ok=True)
                code = run_or_show(["rsync", "-a", "--info=name,stats1", *files, f"{target}/"], f"rsync {target.name}")
                if code:
                    log.close(code)
                    return code
        log.step("6/7 laptop clone: pull, restart, health check")
        code = run_or_show(["bash", str(laptop / "deploy" / "update.sh"), "laptop"], "update.sh", cwd=laptop)
        if code:
            log.error("the update failed (update.sh goes back to the old version when the new one is unhealthy)")
            log.close(code)
            return code

    # ---- 7. verify ----------------------------------------------------------------------------
    log.step("7/7 verify the laptop copy")
    if dry:
        log.skip("dry run: would call /__ping and a sample search on http://127.0.0.1:8000")
        log.close(0)
        return 0
    laptop_env: dict[str, str] = {}
    env_file = laptop / "deploy" / "env.local"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                laptop_env[key.strip()] = value.strip().strip("'\"")
    port = laptop_env.get("PORT", "8000")
    headers = {"X-Origin-Secret": laptop_env["YABAI_ORIGIN_SECRET"]} if laptop_env.get("YABAI_ORIGIN_SECRET") else {}
    healthy = False
    for attempt in range(1, 31):
        try:
            started = time.time()
            with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/__ping", headers=headers), timeout=5) as reply:
                log.ok(f"/__ping -> {reply.status} ({(time.time() - started) * 1000:.0f} ms, attempt {attempt})")
                healthy = reply.status == 200
                break
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            log.info(f"  attempt {attempt}: not answering yet ({error})")
            time.sleep(2)
    if not healthy:
        log.error("the laptop copy does not answer; check `journalctl --user -u yabai-app@laptop`")
        log.close(4)
        return 4
    try:
        started = time.time()
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/search?q=%E9%83%BD&exact=1&lang=EN", headers=headers), timeout=60) as reply:
            body = json.loads(reply.read())
            served = reply.headers.get("X-Served-By", "?")
        first = (body.get("results") or [{}])[0]
        log.ok(f"sample search 都 -> {first.get('simplified')} {first.get('pinyin')} ({(time.time() - started) * 1000:.0f} ms, served by {served})")
        if not first.get("pinyin"):
            log.error("the sample search returned no reading: the data on the laptop looks wrong")
            log.close(5)
            return 5
    except Exception as error:
        log.error(f"the sample search failed: {error}")
        log.close(5)
        return 5
    log.kv("deployed", f"{commit} to {laptop}")
    log.close(0)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
