"""Ranked result lists kept on disk, so a heavy search is computed once, not once per restart.

A new query fetches, decorates and ranks its whole result list (thousands of rows for a single
character or a pinyin syllable): a fraction of a second on the laptop, 4 to 10 seconds on a tablet.
The list for one cache key never changes (the key holds the data and code version), so it is stored
compressed and read back in a few milliseconds.

Two files, both SQLite, both optional:

  data/generated-indexes-optimizations/result_cache.sqlite   shipped, read-only: the heaviest
      queries, prebuilt by scripts/generate-indexes-optimizations/build_result_cache.py
  deploy/run/result_cache.sqlite                             this machine's own: every slow search it
      has computed (deploy/run/ is ignored by Syncthing, so a serving machine may write there)

YABAI_RESULT_CACHE_MB caps the local file (default 256, 0 turns the disk cache off).
"""
from __future__ import annotations

import hashlib
import os
import pickle
import sqlite3
import threading
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_PATH = ROOT / "data" / "generated-indexes-optimizations" / "result_cache.sqlite"
LOCAL_PATH = ROOT / "deploy" / "run" / "result_cache.sqlite"
# Only searches that took at least this long are worth a disk write.
MIN_SECONDS = float(os.environ.get("YABAI_RESULT_CACHE_MIN_MS", "150")) / 1000
MAX_BYTES = int(float(os.environ.get("YABAI_RESULT_CACHE_MB", "256")) * 1024 * 1024)

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS results ("
    "key TEXT PRIMARY KEY, at REAL NOT NULL, seconds REAL NOT NULL, query TEXT NOT NULL, "
    "size INTEGER NOT NULL, body BLOB NOT NULL)"
)
_lock = threading.Lock()
_local: sqlite3.Connection | None = None
_shipped: sqlite3.Connection | None = None
_shipped_checked = False
# When set (the prebuild script), put() appends (key, seconds, query, blob) here instead of writing.
capture: list | None = None


def key_hash(key: tuple) -> str:
    return hashlib.sha256(repr(key).encode("utf-8")).hexdigest()


def encode(value) -> bytes:
    return zlib.compress(pickle.dumps(value, protocol=4), 3)


def decode(blob: bytes):
    return pickle.loads(zlib.decompress(blob))


def _open_local() -> sqlite3.Connection | None:
    global _local
    if _local is None:
        try:
            LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            _local = sqlite3.connect(LOCAL_PATH, check_same_thread=False, timeout=2)
            _local.execute("PRAGMA journal_mode=WAL")
            _local.execute("PRAGMA synchronous=OFF")  # a lost cache row is recomputed
            _local.execute(_SCHEMA)
            _local.commit()
        except (OSError, sqlite3.Error):
            _local = None
    return _local


def _open_shipped() -> sqlite3.Connection | None:
    global _shipped, _shipped_checked
    if not _shipped_checked:
        _shipped_checked = True
        if SHIPPED_PATH.exists():
            try:
                _shipped = sqlite3.connect(
                    f"file:{SHIPPED_PATH}?mode=ro&immutable=1", uri=True, check_same_thread=False
                )
            except sqlite3.Error:
                _shipped = None
    return _shipped


def get(key: tuple):
    """The stored value for this exact key, or None."""
    if MAX_BYTES <= 0:
        return None
    digest = key_hash(key)
    with _lock:
        for connection in (_open_local(), _open_shipped()):
            if connection is None:
                continue
            try:
                row = connection.execute(
                    "SELECT body FROM results WHERE key = ?", (digest,)
                ).fetchone()
            except sqlite3.Error:
                continue
            if row is not None:
                try:
                    return decode(row[0])
                except Exception:  # noqa: BLE001  (a damaged row is just a miss)
                    continue
    return None


def put(key: tuple, value, seconds: float) -> None:
    """Store a value that took `seconds` to compute (ignored when it was quick)."""
    if seconds < MIN_SECONDS:
        return
    try:
        blob = encode(value)
    except Exception:  # noqa: BLE001  (something unpicklable: keep it in memory only)
        return
    query = str(key[0]) if key else ""
    if capture is not None:
        capture.append((key_hash(key), seconds, query, blob))
        return
    if MAX_BYTES <= 0 or len(blob) > MAX_BYTES // 4:
        return
    with _lock:
        connection = _open_local()
        if connection is None:
            return
        try:
            connection.execute(
                "INSERT OR REPLACE INTO results (key, at, seconds, query, size, body) "
                "VALUES (?,?,?,?,?,?)",
                (key_hash(key), time.time(), seconds, query, len(blob), blob),
            )
            total = connection.execute("SELECT COALESCE(SUM(size), 0) FROM results").fetchone()[0]
            if total > MAX_BYTES:
                # Oldest first, down to 80% of the cap. Rows of an older data or code version can
                # never be read again (their key is gone), and they are the oldest.
                for rowid, size in connection.execute(
                    "SELECT rowid, size FROM results ORDER BY at"
                ).fetchall():
                    connection.execute("DELETE FROM results WHERE rowid = ?", (rowid,))
                    total -= size
                    if total <= MAX_BYTES * 0.8:
                        break
            connection.commit()
        except sqlite3.Error:
            pass


def reset_for_tests(local: Path | None = None, shipped: Path | None = None) -> None:
    """Close the open files and point at other paths (tests only)."""
    global _local, _shipped, _shipped_checked, LOCAL_PATH, SHIPPED_PATH
    with _lock:
        for connection in (_local, _shipped):
            if connection is not None:
                connection.close()
        _local = _shipped = None
        _shipped_checked = False
        if local is not None:
            LOCAL_PATH = local
        if shipped is not None:
            SHIPPED_PATH = shipped
