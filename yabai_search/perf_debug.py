"""Per-request tracing for Yabai Search. Zero cost unless tracing is on.

Turn on with env YABAI_TRACE=1 (or call enable()). Everything here is debug-only:
it never changes results.

Typical wiring (see PLAN.md):
    trace = perf_debug.start(request_id, query)         # in middleware
    perf_debug.mark("handler_start")                     # first line of endpoint
    with perf_debug.stage("pinyin_lookup"): ...          # around each phase
    rows = perf_debug.run_sql(conn, SQL, params, "pinyin")  # at the ~6 main queries
    perf_debug.finish(trace)                             # in middleware, writes jsonl
"""
import asyncio
import collections
import contextvars
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

ENABLED = os.environ.get("YABAI_TRACE") == "1"
SLOW_PLAN_MS = float(os.environ.get("YABAI_PLAN_MS", "30"))   # legacy snapshot value; plan capture is explicit
ALWAYS_PLAN = os.environ.get("YABAI_PLAN") == "1"
TRACE_FILE = os.environ.get("YABAI_TRACE_FILE", "debug_trace.jsonl")

_current = contextvars.ContextVar("yabai_trace", default=None)
_stage_calls: dict[str, int] = {}          # stage name -> calls since process start (cold/warm)
_plan_cache: dict[str, dict] = {}
_inflight = 0
_lock = threading.Lock()
_write_lock = threading.Lock()
LOOP_LAG = collections.deque(maxlen=500)   # (unix_time, lag_ms) from loop_lag_sampler


def enable(on: bool = True) -> None:
    global ENABLED
    ENABLED = on


class Trace:
    def __init__(self, request_id: str, query: str):
        self.id, self.query = request_id, query
        self.t0, self.cpu0 = time.perf_counter(), time.thread_time()
        self.arrival_unix = time.time()
        self.client_sent_ms = None            # set from X-Client-Sent header
        self.stages: list[dict] = []
        self.sql: list[dict] = []
        self.marks: dict[str, float] = {}
        self.shapes: collections.Counter = collections.Counter()
        self.counters: dict = {}
        self.inflight_at_start = 0
        self.threads_at_start = threading.active_count()


def current():
    return _current.get()


def start(request_id: str, query: str = ""):
    global _inflight
    if not ENABLED:
        return None
    trace = Trace(request_id, query)
    with _lock:
        _inflight += 1
        trace.inflight_at_start = _inflight
    _current.set(trace)
    return trace


def mark(name: str) -> None:
    """ms since request start. mark('handler_start') minus the middleware's arrival
    shows time spent waiting for a worker thread."""
    trace = current()
    if trace:
        trace.marks[name] = round((time.perf_counter() - trace.t0) * 1000, 2)


@contextmanager
def stage(name: str):
    trace = current()
    if trace is None:
        yield
        return
    with _lock:
        nth = _stage_calls.get(name, 0)
        _stage_calls[name] = nth + 1
    wall0, cpu0 = time.perf_counter(), time.thread_time()
    try:
        yield
    finally:
        wall = (time.perf_counter() - wall0) * 1000
        cpu = (time.thread_time() - cpu0) * 1000
        # wall >> cpu means the thread was waiting (lock, disk, GIL), not computing.
        trace.stages.append({"name": name, "wall_ms": round(wall, 2), "cpu_ms": round(cpu, 2),
                             "wait_ms": round(wall - cpu, 2), "nth_call": nth})


class TimedLock:
    """Drop-in for threading.Lock that records how long callers waited."""
    def __init__(self):
        self._lock = threading.Lock()

    def __enter__(self):
        t = time.perf_counter()
        self._lock.acquire()
        waited = (time.perf_counter() - t) * 1000
        trace = current()
        if trace:
            trace.counters["lock_wait_ms"] = round(trace.counters.get("lock_wait_ms", 0) + waited, 2)
        return self

    def __exit__(self, *exc):
        self._lock.release()


_LITERAL = [(re.compile(r"'(?:[^']|'')*'"), "?"), (re.compile(r"\b\d+(?:\.\d+)?\b"), "?"),
            (re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)"), "(?,...)"), (re.compile(r"\s+"), " ")]


def sql_shape(sql: str) -> str:
    for pattern, repl in _LITERAL:
        sql = pattern.sub(repl, sql)
    return sql.strip()[:200]


def count_statements(conn: sqlite3.Connection) -> None:
    """Count every statement a request runs, grouped by shape. A shape that runs
    dozens of times in one request is an N+1 pattern. Call once per connection."""
    def callback(statement: str):
        trace = current()
        if not trace:
            return
        stripped = statement.lstrip()
        # SQLite's FTS shadow tables issue many internal statements with a
        # leading SQL comment. They are not application N+1 queries.
        if stripped.startswith("--") or stripped.startswith("PRAGMA "):
            return
        trace.shapes[sql_shape(statement)] += 1
    conn.set_trace_callback(callback)


def plan_summary(conn, sql: str, params=()) -> dict:
    key = sql_shape(sql)
    if key in _plan_cache:
        return _plan_cache[key]
    try:
        details = [row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    except sqlite3.Error as exc:
        return {"error": str(exc)}
    result = {
        "plan": details,
        "scans": [d for d in details if d.startswith("SCAN") and "VIRTUAL TABLE" not in d],
        "fts": [d for d in details if "VIRTUAL TABLE" in d],
        "temp_sort": any("TEMP B-TREE" in d for d in details),
    }
    _plan_cache[key] = result
    return result


def run_sql(conn: sqlite3.Connection, sql: str, params=(), label: str = ""):
    """conn.execute(sql, params).fetchall() with timing, VM-step count and plan.
    Use at the main queries only; fetchall is included in the timing on purpose
    because SQLite does most of its work while rows are fetched."""
    trace = current()
    if trace is None:
        return conn.execute(sql, params).fetchall()
    steps = [0]

    def progress():
        steps[0] += 1
        return 0

    conn.set_progress_handler(progress, 1000)        # ~1 call per 1000 VM instructions
    wall0, cpu0 = time.perf_counter(), time.thread_time()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.set_progress_handler(None, 1000)
    wall = (time.perf_counter() - wall0) * 1000
    record = {"label": label, "sql": sql_shape(sql), "wall_ms": round(wall, 2),
              "cpu_ms": round((time.thread_time() - cpu0) * 1000, 2),
              "rows": len(rows), "vm_kops": steps[0]}
    if ALWAYS_PLAN:
        record["plan"] = plan_summary(conn, sql, params)
    trace.sql.append(record)
    return rows


def server_timing(trace) -> str:
    """Extra Server-Timing entries; append to the existing header."""
    if trace is None:
        return ""
    parts = [f"{s['name']};dur={s['wall_ms']}" for s in trace.stages]
    parts += [f"q_{q['label'] or i};dur={q['wall_ms']}" for i, q in enumerate(trace.sql)]
    return ", ".join(parts)


def finish(trace, extra: dict | None = None) -> dict | None:
    global _inflight
    if trace is None:
        return None
    with _lock:
        _inflight -= 1
    total = (time.perf_counter() - trace.t0) * 1000
    record = {
        "id": trace.id, "query": trace.query, "arrival_unix": trace.arrival_unix,
        "client_sent_ms": trace.client_sent_ms,
        "total_ms": round(total, 2), "cpu_ms": round((time.thread_time() - trace.cpu0) * 1000, 2),
        "marks": trace.marks, "stages": trace.stages, "sql": trace.sql,
        "statements": sum(trace.shapes.values()),
        "repeated_shapes": {k: v for k, v in trace.shapes.items() if v >= 5},
        "counters": trace.counters, "inflight_at_start": trace.inflight_at_start,
        "threads_at_start": trace.threads_at_start,
        "loop_lag_ms_max": max((lag for ts, lag in LOOP_LAG if ts >= trace.arrival_unix - 0.001), default=0),
    }
    if extra:
        record.update(extra)
    _write(record)
    return record


def _write(record: dict) -> None:
    """Append one JSON line, creating the trace directory if needed."""
    directory = os.path.dirname(os.path.abspath(TRACE_FILE))
    os.makedirs(directory, exist_ok=True)
    with _write_lock, open(TRACE_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def startup(info: dict) -> dict:
    """Write the one-time startup configuration snapshot to the trace file."""
    record = {"type": "startup", "ts": time.time(), **info}
    _write(record)
    return record


async def loop_lag_sampler(interval: float = 0.05, threshold_ms: float = 5.0):
    """Run as a background task. Lag well above 0 means something is blocking the
    event loop (sync work in an async path, or GIL pressure from worker threads)."""
    while True:
        t = time.perf_counter()
        await asyncio.sleep(interval)
        lag = (time.perf_counter() - t - interval) * 1000
        if lag >= threshold_ms:
            LOOP_LAG.append((time.time(), round(lag, 1)))
