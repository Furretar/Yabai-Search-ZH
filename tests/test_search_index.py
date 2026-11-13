"""Tests for the prebuilt search index (generated-indexes-optimizations).

The index replaces the two lists that were rebuilt at every startup: the "*"
headword order and the source pinyin rows. These tests build one in a temporary
directory and check it agrees with the live builders it is derived from.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from yabai_search import db

pytestmark = pytest.mark.skipif(
    not db.DB_PATH.exists(), reason="CEDICT database not built"
)


@pytest.fixture(scope="module")
def index_file(tmp_path_factory):
    """Build one index for the whole module; it takes a few seconds."""
    path = tmp_path_factory.mktemp("search-index") / "search_index.sqlite"
    counts = db.build_search_index(path)
    assert counts["headwords"] > 100_000
    assert counts["pinyin"] > 10_000
    assert counts["dialect_labels"] > 0
    assert counts["dialect_pinyin"] > 0
    return path


@pytest.fixture(autouse=True)
def _reset_index_cache():
    """The readiness answer and the loaded headword list are process globals."""
    db._search_index_ok_cache = None
    db._dialect_index_cache = None
    db._dialect_index_loaded = False
    yield
    db._search_index_ok_cache = None
    db._dialect_index_cache = None
    db._dialect_index_loaded = False


def _use_index(monkeypatch, path):
    monkeypatch.setattr(db, "SEARCH_INDEX_DB_PATH", path)
    # Never let a test move the real data/search_index.sqlite on this machine.
    monkeypatch.setattr(
        db, "LEGACY_SEARCH_INDEX_DB_PATH", path.with_name("legacy-missing.sqlite")
    )
    monkeypatch.setattr(db, "_search_index_migrated", False)
    db._search_index_ok_cache = None


def test_legacy_index_is_moved_once(tmp_path, monkeypatch):
    """The pre-move top-level index replaces an older generated copy."""
    legacy = tmp_path / "data" / "search_index.sqlite"
    target = tmp_path / "generated-indexes-optimizations" / "search_index.sqlite"
    legacy.parent.mkdir()
    target.parent.mkdir()
    target.write_bytes(b"stale")
    legacy.write_bytes(b"index")
    # The top-level file is the one the previous release served; make it newer.
    os.utime(target, (1_000_000, 1_000_000))

    monkeypatch.setattr(db, "SEARCH_INDEX_DB_PATH", target)
    monkeypatch.setattr(db, "LEGACY_SEARCH_INDEX_DB_PATH", legacy)
    monkeypatch.setattr(db, "_search_index_migrated", False)

    db._migrate_search_index()

    assert target.read_bytes() == b"index"
    assert not legacy.exists()
    # The flag keeps a second startup from looking for the old file again.
    db._migrate_search_index()
    assert target.read_bytes() == b"index"


def test_index_round_trips_the_live_lists(index_file, monkeypatch):
    """Loading the index gives exactly the lists the live builders produce."""
    live_heads, live_groups, live_sels = db._all_headwords_cache
    live_sorted = list(db._source_pinyin_sorted()[1])

    _use_index(monkeypatch, index_file)
    db._all_headwords_cache = None
    heads = db._all_headwords_list()
    groups = db.all_headword_groups()

    assert len(heads) == len(live_heads)
    assert heads == live_heads
    assert groups == live_groups
    assert db._all_headwords_cache[2] == live_sels

    keys, rows = db._source_pinyin_sorted()[0], live_sorted
    expected_prefix = db._prefix_rows(keys, rows, "shi")
    assert db._source_pinyin_index_rows(prefix="shi") == expected_prefix

    expected_contains = [row for row in rows if "shi" in row[3]]
    assert db._source_pinyin_index_rows(like="%shi%") == expected_contains


def test_source_pinyin_with_index_matches_the_old_path(index_file, monkeypatch):
    """The indexed prefix/contains queries return the same entries as before."""
    _use_index(monkeypatch, index_file)
    db._source_pinyin_raw.cache_clear()
    indexed = {
        query: db._source_pinyin_raw(query, 20)
        for query in ("shi", "shi4", "*shi*")
    }

    monkeypatch.setenv("YABAI_SEARCH_INDEX", "0")
    db._search_index_ok_cache = None
    db._source_pinyin_raw.cache_clear()
    legacy = {
        query: db._source_pinyin_raw(query, 20)
        for query in ("shi", "shi4", "*shi*")
    }

    assert indexed == legacy


def test_stale_or_unknown_index_is_ignored(tmp_path, monkeypatch):
    """A file with the wrong signature or version must fall back, not crash."""
    path = tmp_path / "search_index.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        (("signature", "not-the-current-data"), ("version", "1")),
    )
    connection.commit()
    connection.close()

    _use_index(monkeypatch, path)
    assert db._search_index_ok() is False
    assert db._load_headwords_from_index() is None
    assert db._source_pinyin_index_rows(prefix="shi") is None


def test_signature_survives_a_copy(tmp_path, monkeypatch):
    """A copied source (new mtime, same bytes) must not make the index stale."""
    import os

    source = tmp_path / "source.sqlite"
    source.write_bytes(b"x" * 64)
    monkeypatch.setattr(db, "_SOURCE_QUERIES", [{"database": source}])
    monkeypatch.setattr(db, "FREQ_DB_PATH", tmp_path / "missing.sqlite")

    first = db._search_index_signature()
    os.utime(source, (1_000_000, 1_000_000))
    assert db._search_index_signature() == first


def test_index_disabled_by_environment(index_file, monkeypatch):
    """YABAI_SEARCH_INDEX=0 restores the old in-memory path."""
    _use_index(monkeypatch, index_file)
    monkeypatch.setenv("YABAI_SEARCH_INDEX", "0")
    assert db._search_index_ok() is False
    assert db._load_headwords_from_index() is None


def test_dialect_tables_match_the_source(index_file, monkeypatch):
    """The index carries the label and reading sets the scan uses."""
    _use_index(monkeypatch, index_file)
    labels, pinyin = db._dialect_index()
    expected_labels, expected_pinyin = db._dialect_index_rows()
    assert labels == expected_labels
    assert pinyin == set(expected_pinyin)


def test_dialect_early_out_never_hides_rows(index_file, monkeypatch):
    """When the index says "no rows", the real scan must agree."""
    _use_index(monkeypatch, index_file)
    db._dialect_index_loaded = False
    index = db._dialect_index()
    assert index is not None
    labels, pinyin = index
    queries = list(labels[:30])
    queries.extend(label[:3] for label in labels[:20] if len(label) >= 3)
    queries.extend(sorted(pinyin)[:30])
    queries.extend(["zzzz", "qqqq", "xyzzy", "notalabel", "abc"])
    for query in queries:
        rows = db._dialect_query(query, 200)
        if db._dialect_early_out(query):
            assert rows == (), query
