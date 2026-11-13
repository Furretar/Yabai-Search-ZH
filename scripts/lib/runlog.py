"""Shared logging and process helpers for the four top-level scripts (standard library only).

Every line goes to the terminal and to a log file `archives/logs/<script>-<timestamp>.log`
(`archives/logs/<script>-latest.log` always points at the newest), so a run can be watched live or read later.
Format:  HH:MM:SS.mmm  LEVEL  message        (child output is prefixed with the step name)
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "archives" / "logs"

_COLORS = {"INFO": "", "STEP": "\033[1;36m", "OK": "\033[32m", "WARN": "\033[33m", "ERROR": "\033[1;31m",
           "CMD": "\033[35m", "OUT": "\033[2m", "SKIP": "\033[34m"}
_RESET = "\033[0m"
_NESTED = re.compile(r"^\d\d:\d\d:\d\d\.\d{3}\s+(\w+)\s+(.*)$")


def human_size(n: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def human_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


class Log:
    """A timestamped logger writing to stdout and to a file."""

    def __init__(self, name: str, quiet: bool = False) -> None:
        self.name = name
        self.quiet = quiet
        self.started = time.time()
        self.lock = threading.Lock()
        self.color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = LOG_DIR / f"{name}-{stamp}.log"
        self.file = open(self.path, "a", encoding="utf-8", buffering=1)
        latest = LOG_DIR / f"{name}-latest.log"
        try:
            if latest.is_symlink() or latest.exists():
                latest.unlink()
            latest.symlink_to(self.path.name)
        except OSError:
            pass
        # Keep the newest 20 logs per script.
        old = sorted(LOG_DIR.glob(f"{name}-2*.log"))
        for path in old[:-20]:
            try:
                path.unlink()
            except OSError:
                pass

    def _emit(self, level: str, message: str) -> None:
        now = time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int((now % 1) * 1000):03d}"
        plain = f"{stamp}  {level:<5} {message}"
        with self.lock:
            self.file.write(plain + "\n")
            if not (self.quiet and level in ("OUT", "CMD", "INFO")):
                if self.color and _COLORS.get(level):
                    print(f"{stamp}  {_COLORS[level]}{level:<5} {message}{_RESET}", flush=True)
                else:
                    print(plain, flush=True)

    def info(self, message: str) -> None:
        self._emit("INFO", message)

    def ok(self, message: str) -> None:
        self._emit("OK", message)

    def warn(self, message: str) -> None:
        self._emit("WARN", message)

    def error(self, message: str) -> None:
        self._emit("ERROR", message)

    def skip(self, message: str) -> None:
        self._emit("SKIP", message)

    def step(self, message: str) -> None:
        self._emit("STEP", "=" * 8 + " " + message + " " + "=" * 8)

    def kv(self, key: str, value: object) -> None:
        self._emit("INFO", f"  {key:<22} {value}")

    def header(self, title: str, argv: list[str] | None = None) -> None:
        self.step(title)
        self.kv("started", time.strftime("%Y-%m-%d %H:%M:%S"))
        self.kv("project", ROOT)
        self.kv("python", f"{sys.executable} ({sys.version.split()[0]})")
        self.kv("command line", " ".join(shlex.quote(a) for a in (argv or sys.argv)))
        self.kv("log file", self.path)

    def run(
        self,
        command: list[str],
        *,
        prefix: str = "",
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        heartbeat: float = 30.0,
        stdin_devnull: bool = True,
        group: bool = False,
    ) -> tuple[int, float]:
        """Run a command, logging every output line live. Returns (exit code, seconds).

        With group=True the child leads its own process group, and an interrupt stops the whole group
        (a server and everything it started), not just the first process.
        """
        label = f"[{prefix}] " if prefix else ""
        self._emit("CMD", f"{label}$ {' '.join(shlex.quote(c) for c in command)}")
        started = time.time()
        merged = {**os.environ, **(env or {}), "PYTHONUNBUFFERED": "1"}
        try:
            child = subprocess.Popen(
                command,
                cwd=str(cwd or ROOT),
                env=merged,
                stdin=subprocess.DEVNULL if stdin_devnull else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                start_new_session=group,
            )
        except OSError as error:
            self.error(f"{label}could not start: {error}")
            return 127, 0.0
        last_output = [time.time()]

        def pump() -> None:
            assert child.stdout is not None
            for line in child.stdout:
                last_output[0] = time.time()
                text = line.rstrip()
                # A child that logs this way (another top-level script) already has a timestamp and a
                # level: keep its level and drop its timestamp, so lines are not stamped twice.
                nested = _NESTED.match(text)
                if nested and nested.group(1) in _COLORS:
                    self._emit(nested.group(1), f"{label}{nested.group(2)}")
                else:
                    self._emit("OUT", f"{label}{text}")

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            while child.poll() is None:
                time.sleep(0.2)
                quiet_for = time.time() - last_output[0]
                if heartbeat and quiet_for >= heartbeat:
                    self.info(f"{label}still running ({human_time(time.time() - started)} so far, no output for {human_time(quiet_for)})")
                    last_output[0] = time.time()
        except KeyboardInterrupt:
            self.warn(f"{label}interrupted: stopping the child" + (" (whole process group)" if group else ""))
            stop = (lambda sig: os.killpg(child.pid, sig)) if group else (lambda sig: child.send_signal(sig))
            try:
                stop(signal.SIGTERM)
                child.wait(15)
            except subprocess.TimeoutExpired:
                stop(signal.SIGKILL)
            except ProcessLookupError:
                pass
            reader.join(2)
            raise
        reader.join(5)
        return child.returncode, time.time() - started

    def close(self, code: int) -> None:
        elapsed = human_time(time.time() - self.started)
        (self.ok if code == 0 else self.error)(f"finished with exit code {code} in {elapsed}; log: {self.path}")
        self.file.close()
