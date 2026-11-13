"""Tests for the rolling search log (archives/search.log).

The log exists so a slow search can be explained after the fact: each entry
keeps the query, its duration, the result count and the SQL statements with
per-statement timings. Only the most recent searches are kept.
"""

from __future__ import annotations

import time

import pytest

from yabai_search import db, services as main

@pytest.fixture(autouse=True)
def _clean_recent_searches():
    """The deque lives for the process, so do not leak entries between tests."""
    main._recent_searches.clear()
    yield
    main._recent_searches.clear()


def _log_to(tmp_path, monkeypatch, host="tablet"):
    log = tmp_path / "search.log"
    monkeypatch.setattr(main, "_SEARCH_LOG_PATH", log)
    monkeypatch.setattr(db, "SQL_LOG", True)
    monkeypatch.setenv("YABAI_SERVED_BY", host)
    return log


def test_log_keeps_only_the_last_five_searches(tmp_path, monkeypatch):
    log = _log_to(tmp_path, monkeypatch)

    for number in range(6):
        main._record_search(f"query{number}", 1.5, number, [f"SELECT {number};"])

    text = log.read_text(encoding="utf-8")
    assert "query0" not in text
    for number in (1, 2, 3, 4, 5):
        assert f"'query{number}'" in text
    # Each kept search contributes exactly one SQL line.
    assert text.count("SELECT ") == 5


def test_entry_names_the_serving_copy(tmp_path, monkeypatch):
    """The log must say which host answered (laptop, tablet, dev)."""
    log = _log_to(tmp_path, monkeypatch, host="laptop")

    main._record_search("xue", 1.5, 12, [])

    assert "[laptop] 'xue'" in log.read_text(encoding="utf-8")


def test_host_falls_back_to_unknown_when_unset(tmp_path, monkeypatch):
    log = _log_to(tmp_path, monkeypatch)
    monkeypatch.delenv("YABAI_SERVED_BY", raising=False)

    main._record_search("xue", 1.5, 12, [])

    assert "[unknown] 'xue'" in log.read_text(encoding="utf-8")


def test_preliminary_primary_request_is_not_logged(tmp_path, monkeypatch):
    """The fast tier repeats in the complete request, so only log that one."""
    log = _log_to(tmp_path, monkeypatch)

    main._record_search("knee", 1.0, 1, ["SELECT 1;"], part="primary")

    assert not log.exists()


def test_logging_can_be_switched_off(tmp_path, monkeypatch):
    log = _log_to(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "SQL_LOG", False)

    main._record_search("quiet", 1.0, 1, ["SELECT 1;"])

    assert not log.exists()


def test_a_real_search_writes_query_and_timing(tmp_path, monkeypatch, search):
    """End to end: /api/search fills the log through _record_search."""
    log = _log_to(tmp_path, monkeypatch)

    search("xue")

    text = log.read_text(encoding="utf-8")
    assert "[tablet] 'xue'" in text
    assert "results in" in text


def test_readings_extra_check_is_cached(monkeypatch):
    """The sqlite_master probe runs once, not on every search."""

    class FakeConnection:
        def __init__(self):
            self.calls = []

        def execute(self, sql, *args):
            self.calls.append(sql)
            return self

        def fetchone(self):
            return (1,)

        def close(self):
            pass

    connection = FakeConnection()
    monkeypatch.setattr(db, "_connect", lambda path: connection)
    db._readings_extra_cache = None
    try:
        db._has_readings_extra()
        db._has_readings_extra()
    finally:
        db._readings_extra_cache = None
    assert sum("readings_extra" in sql for sql in connection.calls) == 1


def test_compact_sql_collapses_placeholder_runs():
    """Long placeholder lists become ?*N, and whitespace stays collapsed."""
    db._compact_sql.cache_clear()
    assert db._compact_sql("SELECT ?, ?, ?  FROM t") == "SELECT ?*3 FROM t"
    many = "SELECT " + ", ".join("?" * 300) + " FROM t"
    assert db._compact_sql(many) == "SELECT ?*300 FROM t"


def test_compact_sql_is_memoized():
    """Repeated SQL texts are normalized once."""
    db._compact_sql.cache_clear()
    first = db._compact_sql("SELECT 1  FROM t")
    before = db._compact_sql.cache_info().hits
    assert db._compact_sql("SELECT 1  FROM t") == first
    assert db._compact_sql.cache_info().hits == before + 1


def test_sql_capture_records_statement_text_and_rows(monkeypatch):
    """db.start_log/_log_sql/end_log produce the lines _record_search stores.

    The cursor wiring is chosen when db is imported (see _PooledConnection), so
    it cannot be switched on in-process; this covers the capture itself.
    """
    monkeypatch.setattr(db, "SQL_LOG", True)

    db.start_log()
    db._log_sql("SELECT 1  FROM t", time.perf_counter(), 2)
    statements = db.end_log()

    assert len(statements) == 1
    assert "ms rows=2" in statements[0]
    # Whitespace is collapsed so one statement stays on one log line.
    assert "| SELECT 1 FROM t" in statements[0]
