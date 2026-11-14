#!/usr/bin/env python3
"""Start the website on this computer, with a detailed log of everything it does.

    python3 scripts/start.py                       # role "dev": http://127.0.0.1:8001 (needs the port free)
    python3 scripts/start.py --port 8002           # another port, for example next to the running dev service
    python3 scripts/start.py --role laptop         # serve as another host's role (port 8000, origin-secret gate)
    python3 scripts/start.py --debug               # gunicorn debug logging and an access log line per request
    python3 scripts/start.py --regenerate          # bring stale generated files up to date first
    python3 scripts/start.py --no-check            # skip the generated-files check

It prepares and then runs deploy/run.sh, the same launcher the systemd units use (one place decides the
gunicorn settings, the accounts replication and the migrations). In order:
  1. preflight: environment, deploy/env.local (key names only, never values), the data files, whether
     the generated files are current, whether the port is free (and what holds it when it is not);
  2. starts the server and logs every line it prints;
  3. waits for /__ping, then sends one real search to show it answers, and prints the addresses.
Ctrl-C stops the server and everything it started. Every line is also in archives/logs/start-<time>.log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "lib"))

DATA = ROOT / "data"
REQUIRED_DATA = [
    "dictionary-databases/cedict.sqlite", "dictionary-databases/wiktionary.sqlite",
    "dictionary-databases/zhwiktionary.sqlite", "dictionary-databases/jpwiktionary.sqlite",
    "dictionary-databases/moe-taiwan/moe.sqlite", "dictionary-databases/unihan.sqlite",
    "dictionary-databases/baidu/baidu_clean.sqlite", "dictionary-databases/corpus_freq.sqlite",
    "dictionary-databases/hsk.sqlite",
]


def read_env_local() -> dict[str, str]:
    """deploy/env.local as a dict (shell-style KEY=value lines)."""
    values: dict[str, str] = {}
    path = ROOT / "deploy" / "env.local"
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def who_holds(port: int) -> str:
    """A one-line answer to "what is listening on this port?" (best effort)."""
    try:
        out = subprocess.run(["ss", "-ltnp", f"sport = :{port}"], capture_output=True, text=True, timeout=5).stdout
        line = next((l for l in out.splitlines() if f":{port}" in l), "")
        owner = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        text = f"{owner.group(1)} (pid {owner.group(2)})" if owner else line.strip() or "unknown process"
    except Exception:
        text = "unknown process"
    for unit in ("yabai-app@dev", "yabai-app@laptop"):
        try:
            if subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=5).stdout.strip() == "active":
                if (unit.endswith("dev") and port == 8001) or (unit.endswith("laptop") and port == 8000):
                    text += f"; this is the systemd unit {unit}"
        except Exception:
            pass
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--role", default="dev", help="deploy/run.sh role: dev (default), laptop, tablet, ... (names the host)")
    parser.add_argument("--port", type=int, help="port to listen on (default: PORT in env.local, else 8001 for dev, 8000 otherwise)")
    parser.add_argument("--bind", help="bind address (default 127.0.0.1; 0.0.0.0 reaches it over the LAN)")
    parser.add_argument("--threads", type=int, help="gunicorn threads (default 8)")
    parser.add_argument("--debug", action="store_true", help="gunicorn debug logging and an access log")
    parser.add_argument("--regenerate", action="store_true", help="run regenerate_everything.py first")
    parser.add_argument("--no-check", action="store_true", help="skip the generated-files check")
    parser.add_argument("--no-sample", action="store_true", help="do not send the sample search after startup")
    args = parser.parse_args()

    from envsetup import ensure_project_environment  # noqa: PLC0415

    ensure_project_environment(str(Path(__file__).resolve()), sys.argv[1:])
    from runlog import Log, human_size, human_time  # noqa: PLC0415

    log = Log("start")
    log.header("start: run the website on this computer")
    env_local = read_env_local()
    port = args.port or int(env_local.get("PORT") or (8001 if args.role == "dev" else 8000))
    bind = args.bind or env_local.get("YABAI_BIND") or "127.0.0.1"

    # ---- 1. preflight -------------------------------------------------------------------------
    log.step("1/3 preflight")
    log.kv("role", f"{args.role}  ({'no origin-secret gate' if args.role == 'dev' else 'behind the origin-secret gate'})")
    log.kv("address", f"{bind}:{port}")
    log.kv("hostname", socket.gethostname())
    log.kv("launcher", ROOT / "deploy" / "run.sh")
    if not (ROOT / "deploy" / "env.local").exists():
        log.error("deploy/env.local is missing: copy deploy/env.local.example and set DJANGO_SECRET_KEY")
        log.close(2)
        return 2
    log.kv("env.local keys", ", ".join(sorted(env_local)) or "(none)")
    if "DJANGO_SECRET_KEY" not in env_local:
        log.error("DJANGO_SECRET_KEY is not set in deploy/env.local: the server cannot start without it")
        log.close(2)
        return 2
    if args.role != "dev" and "YABAI_ORIGIN_SECRET" not in env_local:
        log.warn("YABAI_ORIGIN_SECRET is not set: the origin-secret gate stays off for this role")
    log.kv("accounts replication", "on" if args.role != "dev" and env_local.get("YABAI_CONTROL_URL") else "off")

    log.info("data files:")
    missing = 0
    for name in REQUIRED_DATA:
        path = DATA / name
        if path.exists():
            log.info(f"  {name:28} {human_size(path.stat().st_size):>9}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(path.stat().st_mtime))}")
        else:
            missing += 1
            log.warn(f"  {name:28} MISSING (that dictionary is skipped)")
    if not (DATA / "dictionary-databases" / "cedict.sqlite").exists():
        log.error("data/dictionary-databases/cedict.sqlite is missing: run the importers in scripts/download-dictionaries/ first")
        log.close(2)
        return 2

    if args.regenerate:
        code, _ = log.run([sys.executable, str(HERE / "regenerate_everything.py"), "--no-result-cache"], prefix="regenerate")
        if code:
            log.error("regenerate_everything.py failed: not starting")
            log.close(code)
            return code
    elif not args.no_check:
        code, _ = log.run([sys.executable, str(HERE / "regenerate_everything.py"), "--check", "--quiet"], prefix="generated files")
        if code:
            log.warn("some generated files are stale: the site still works but may be slower or miss results;")
            log.warn("run `python3 scripts/regenerate_everything.py` (or start with --regenerate)")
        else:
            log.ok("every required generated file is current")

    probe = socket.socket()
    probe.settimeout(1)
    busy = probe.connect_ex(("127.0.0.1", port)) == 0
    probe.close()
    if busy:
        log.error(f"port {port} is already in use by {who_holds(port)}")
        log.error("nothing was started or stopped. Use --port <free port> to run next to it.")
        log.close(3)
        return 3
    log.ok(f"port {port} is free")

    # ---- 2. start -----------------------------------------------------------------------------
    log.step("2/3 starting the server")
    # Its own pid file: deploy/update.sh restarts the systemd copy through deploy/run/gunicorn.pid.
    env = {"PORT": str(port), "YABAI_BIND": bind, "YABAI_PID_FILE": f"deploy/run/start-{port}.pid"}
    if args.threads:
        env["YABAI_THREADS"] = str(args.threads)
    if args.debug:
        env["GUNICORN_CMD_ARGS"] = "--log-level debug --access-logfile - --error-logfile -"
        env["DJANGO_LOG_LEVEL"] = "DEBUG"
    else:
        env["GUNICORN_CMD_ARGS"] = "--access-logfile - --error-logfile -"
    log.kv("environment added", ", ".join(f"{k}={v}" for k, v in env.items()))

    ready = threading.Event()
    stop = threading.Event()
    secret = env_local.get("YABAI_ORIGIN_SECRET", "") if args.role != "dev" else ""

    def request(path: str, timeout: float = 5.0) -> tuple[int, bytes, float]:
        started = time.time()
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Origin-Secret": secret} if secret else {})
        with urllib.request.urlopen(req, timeout=timeout) as reply:
            return reply.status, reply.read(), time.time() - started

    def watcher() -> None:
        began = time.time()
        while not stop.is_set() and time.time() - began < 180:
            time.sleep(0.7)
            try:
                status, _body, seconds = request("/__ping", 2)
            except (urllib.error.URLError, OSError, TimeoutError):
                continue
            if status == 200:
                log.ok(f"/__ping answered 200 after {human_time(time.time() - began)} ({seconds * 1000:.0f} ms)")
                ready.set()
                break
        else:
            if not stop.is_set():
                log.error("the server did not answer /__ping within 3 minutes; the lines above should say why")
                return
        if ready.is_set() and not args.no_sample:
            log.step("3/3 the server is up")
            try:
                status, body, seconds = request("/api/search?q=%E9%83%BD&exact=1&lang=EN", 60)
                first = (json.loads(body).get("results") or [{}])[0]
                log.ok(f"sample search 都 -> {status}, {seconds * 1000:.0f} ms, first row {first.get('simplified')} {first.get('pinyin')}")
            except Exception as error:
                log.warn(f"the sample search failed: {error}")
        if ready.is_set():
            log.kv("open", f"http://127.0.0.1:{port}/")
            if bind not in ("127.0.0.1", "localhost"):
                log.kv("also on", f"http://{socket.gethostname()}:{port}/")
            log.info("running; press Ctrl-C to stop")

    thread = threading.Thread(target=watcher, daemon=True)
    thread.start()
    try:
        code, seconds = log.run(["bash", str(ROOT / "deploy" / "run.sh"), args.role], prefix="server", env=env, heartbeat=0, group=True)
        log.warn(f"the server process ended (exit {code}) after {human_time(seconds)}")
    except KeyboardInterrupt:
        code = 0
        log.ok("stopped by Ctrl-C")
    finally:
        stop.set()
    log.close(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
