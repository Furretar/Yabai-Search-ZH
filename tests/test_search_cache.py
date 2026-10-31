"""Contracts for cacheable search responses and the client fetch plan."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

from yabai_search import db

DATA = Path(__file__).resolve().parents[1] / "data"
APP_JS = Path(__file__).resolve().parents[1] / "yabai_search" / "static" / "app.js"

pytestmark = pytest.mark.skipif(
    not (DATA / "cedict.sqlite").exists(), reason="CEDICT database not built"
)


def test_data_version_is_a_short_stable_fingerprint():
    """The data version is a 10-character hex fingerprint of the data files."""
    first = db.data_version()
    assert re.fullmatch(r"[0-9a-f]{10}", first)
    assert db.data_version() == first


def test_search_version_is_data_plus_code_fingerprint():
    """The version is stable across restarts but changes with code or flags."""
    from yabai_search import main as app_main

    first = app_main.search_version()
    assert re.fullmatch(r"[0-9a-f]{10}-[0-9a-f]{8}", first)
    assert app_main.search_version() == first


def test_code_fingerprint_tracks_files_and_env(tmp_path, monkeypatch):
    """Editing a module or a result-affecting flag changes the fingerprint."""
    from yabai_search import main as app_main

    module = tmp_path / "search.py"
    module.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(app_main, "BASE", tmp_path)
    monkeypatch.delenv("YABAI_UNCAPPED", raising=False)
    base = app_main._code_fingerprint()
    assert app_main._code_fingerprint() == base

    module.write_text("x = 2\n", encoding="utf-8")
    assert app_main._code_fingerprint() != base

    monkeypatch.setenv("YABAI_UNCAPPED", "0")
    assert app_main._code_fingerprint() != base


def test_page_groups_copy_does_not_mutate_cached_rows():
    """Paging a cached list must not add `group` to the cached rows."""
    from yabai_search import main as app_main

    rows = [
        {"simplified": "了", "traditional": "了"},
        {"simplified": "了", "traditional": "瞭"},
    ]
    page, total = app_main._page_groups_copy(rows, 0, 25)
    assert total == 1
    assert [row["traditional"] for row in page] == ["了", "瞭"]
    assert all("group" in row for row in page)
    assert all("group" not in row for row in rows)


def test_rank_cache_single_flight(monkeypatch):
    """Eight threads asking for the same key compute it exactly once."""
    from yabai_search import main as app_main

    monkeypatch.setattr(app_main, "_RANK_CACHE_MAX_ROWS", 1000)
    monkeypatch.setattr(app_main, "_RANK_CACHE_STORE_ROWS", 500)
    app_main._rank_cache.clear()
    app_main._rank_cache_events.clear()
    calls = []
    lock = threading.Lock()

    def compute():
        with lock:
            calls.append(1)
        time.sleep(0.05)
        return ([{"simplified": "水"}], [], False, False, {}, {})

    statuses = []

    def worker():
        _state, status = app_main._rank_cache_get_or_compute(
            ("single-flight",), compute
        )
        statuses.append(status)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1
    assert statuses.count("miss") == 1
    assert statuses.count("hit") == 7


def test_rank_cache_skips_oversized_lists(monkeypatch):
    """A list larger than half the budget is returned but not stored."""
    from yabai_search import main as app_main

    monkeypatch.setattr(app_main, "_RANK_CACHE_MAX_ROWS", 10)
    monkeypatch.setattr(app_main, "_RANK_CACHE_STORE_ROWS", 5)
    app_main._rank_cache.clear()
    big = ([{"i": i} for i in range(6)], [], False, False, {}, {})
    state, status = app_main._rank_cache_get_or_compute(("big",), lambda: big)
    assert status == "miss"
    assert state is big
    assert ("big",) not in app_main._rank_cache


def test_rank_cache_evicts_least_recently_used(monkeypatch):
    """The row budget, not the entry count, decides what is evicted."""
    from yabai_search import main as app_main

    monkeypatch.setattr(app_main, "_RANK_CACHE_MAX_ROWS", 6)
    monkeypatch.setattr(app_main, "_RANK_CACHE_STORE_ROWS", 6)
    app_main._rank_cache.clear()
    for key in ("a", "b", "c"):
        value = ([{"i": 0}, {"i": 1}], [], False, False, {}, {})
        app_main._rank_cache_get_or_compute((key,), lambda value=value: value)
    assert list(app_main._rank_cache) == [("a",), ("b",), ("c",)]
    evicted = ([{"i": 0}, {"i": 1}], [], False, False, {}, {})
    app_main._rank_cache_get_or_compute(("d",), lambda: evicted)
    assert list(app_main._rank_cache) == [("b",), ("c",), ("d",)]


def test_search_results_are_identical_with_the_rank_cache_off(client, monkeypatch):
    """Turning the cache off changes timing only, never the rows."""
    from yabai_search import main as app_main

    app_main._rank_cache.clear()
    on = client.get(
        "/api/search", params={"q": "water", "limit": 25, "offset": 0}
    ).json()
    monkeypatch.setattr(app_main, "_RANK_CACHE_ENABLED", False)
    off = client.get(
        "/api/search", params={"q": "water", "limit": 25, "offset": 0}
    ).json()
    for key in ("results", "fuzzy", "total", "pinyin_match", "english_match"):
        assert on[key] == off[key]


def test_search_api_is_cacheable_only_with_a_data_version(client):
    """A `dv` request may be cached; the same request without it may not."""
    response = client.get("/api/search", params={"q": "档案", "limit": 25, "dv": "test"})
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("public, max-age=86400")

    plain = client.get("/api/search", params={"q": "档案", "limit": 25})
    assert plain.status_code == 200
    assert "public" not in plain.headers.get("cache-control", "")


def test_errors_and_html_are_never_publicly_cached(client):
    """Validation errors and HTML pages stay out of shared caches."""
    bad = client.get("/api/search", params={"q": "档案", "limit": 0, "dv": "x"})
    assert bad.status_code == 422
    assert "public" not in bad.headers.get("cache-control", "")

    home = client.get("/")
    assert home.headers["cache-control"].startswith("no-store")
    assert 'name="data-version"' in home.text


def test_frontend_uses_the_data_version_and_parallel_tiers():
    """app.js reads the version, skips wasted tiers, and starts both together."""
    source = APP_JS.read_text(encoding="utf-8")
    assert 'meta[name="data-version"]' in source
    assert "DATA_VERSION" in source
    # One helper owns both tiers, and wildcard queries are detected client-side.
    assert "function fetchSearchPart(" in source
    assert "function completeInOneResponse(" in source
    # First page: the complete tier starts before the fast tier is awaited.
    assert 'fullPromise = fetchSearchPart("full", url);' in source
    # Three pages are warmed on each side of the current page.
    assert "const PREFETCH_RADIUS = 3;" in source
    assert "function prefetchAround(" in source
