"""Differential tests for the indexed lookup paths.

The 5,000-term definition check is marked slow because it intentionally scans the
old path thousands of times. The headword checks run in the normal suite.
"""
from __future__ import annotations

import os
import random
import sqlite3

import pytest

from yabai_search import db

ROOT = db.DB_PATH.parent
pytestmark = pytest.mark.skipif(
    not db.DB_PATH.exists(), reason="CEDICT database not built"
)


def test_head_grams_matches_legacy(monkeypatch):
    queries = ["档案", "檔案", "A股", "C位", "一言"]
    monkeypatch.setenv("YABAI_HEADWORD_LEGACY", "1")
    old = {q: [e["simplified"] for e in db.search(q, limit=25)[0]] for q in queries}
    monkeypatch.delenv("YABAI_HEADWORD_LEGACY", raising=False)
    new = {q: [e["simplified"] for e in db.search(q, limit=25)[0]] for q in queries}
    assert new == old


def test_head_grams_wildcard_falls_back(monkeypatch):
    monkeypatch.setenv("YABAI_HEADWORD_LEGACY", "1")
    old = [e["simplified"] for e in db.search("档案*", limit=25)[0]]
    monkeypatch.delenv("YABAI_HEADWORD_LEGACY", raising=False)
    new = [e["simplified"] for e in db.search("档案*", limit=25)[0]]
    assert new == old


def test_fast_paths_active():
    status = db.fast_path_status()
    assert all(item["active"] for item in status.values()), status


def test_warm_caches_skips_the_expensive_headword_lists(monkeypatch):
    """Startup must not rebuild the "*" list or the pinyin rows on a phone."""
    names: list[str] = []
    monkeypatch.setattr(db, "_warm_step", lambda name, fn, *args: names.append(name))
    monkeypatch.setattr(db, "_start_page_cache_warm", lambda: None)
    monkeypatch.setattr(db, "_search_index_ok", lambda: True)
    monkeypatch.setenv("YABAI_WARM_PAGE_CACHE", "off")

    db.warm_caches()

    assert "all_headwords" not in names
    assert "source_pinyin_sorted" not in names
    assert "source_pinyin_rows" not in names
    # The cheap, always-useful caches are still warmed.
    assert "cedict_heads" in names


def test_page_cache_warm_respects_the_byte_budget(tmp_path, monkeypatch):
    """Only the hot files that fit in the budget are read."""
    first = tmp_path / "a.sqlite"
    second = tmp_path / "b.sqlite"
    first.write_bytes(b"x" * 200)
    second.write_bytes(b"x" * 200)
    monkeypatch.setattr(db, "_PAGE_CACHE_FILES", (first, second))
    monkeypatch.setattr(db, "_available_memory_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(db, "_WARM_STATE", dict(db._WARM_STATE))

    assert db._warm_page_cache_files(300) == 200
    assert db._WARM_STATE["skipped"] == ["b.sqlite"]


def test_page_cache_warm_skips_when_memory_is_low(tmp_path, monkeypatch):
    """A phone with little free RAM must not be filled with dictionary pages."""
    first = tmp_path / "a.sqlite"
    first.write_bytes(b"x" * 200)
    monkeypatch.setattr(db, "_PAGE_CACHE_FILES", (first,))
    monkeypatch.setattr(db, "_available_memory_bytes", lambda: 100 * 1024**2)
    monkeypatch.setattr(db, "_WARM_STATE", dict(db._WARM_STATE))

    assert db._warm_page_cache_files(None) == 0
    assert "available memory" in db._WARM_STATE["skipped"][0]


def test_batched_in_lists_are_chunked(monkeypatch):
    """A big result set must not exceed a phone's SQLite variable limit.

    The tablet's SQLite rejects more than ~32k bound variables; one "shi"
    search ranks over 10k rows, so the batched lookups split their IN lists.
    Chunking must not change which rows come back.
    """
    connection = db.connect()
    words = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT traditional FROM entries LIMIT 1200"
        )
    ]

    variant_rows = {}
    reading_counts = {}
    for size in (10, 5000):
        monkeypatch.setattr(db, "_IN_CHUNK_SIZE", size)
        # A form row can match words in two different chunks and come back
        # twice; _VariantIndex de-duplicates it, so compare the set of rows.
        variant_rows[size] = {
            tuple(row) for row in db._variant_form_rows(words)
        }
        reading_counts[size] = db._cedict_reading_counts(words)
    assert variant_rows[10] == variant_rows[5000]
    assert reading_counts[10] == reading_counts[5000]

    frequencies = {}
    for size in (10, 5000):
        monkeypatch.setattr(db, "_IN_CHUNK_SIZE", size)
        entries = [{"simplified": word, "traditional": word} for word in words]
        db._attach_freq(entries)
        frequencies[size] = {entry["simplified"]: entry["freq"] for entry in entries}
    assert frequencies[10] == frequencies[5000]


def test_gram_words_stale_metadata(monkeypatch, tmp_path):
    import sqlite3
    path = tmp_path / "gram_words.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES ('source:moe', 'not-the-hash')")
    connection.commit()
    connection.close()
    monkeypatch.setattr(db, "GRAM_WORDS_DB_PATH", path)
    db._gram_words_ok_cache = None
    assert db._gram_words_version_ok() is False


@pytest.mark.skipif(
    os.environ.get("RUN_SLOW_GOLDEN") != "1",
    reason="set RUN_SLOW_GOLDEN=1 to run the 5,000-term differential",
)
def test_definition_matcher_5000_terms():
    random.seed(20261027)
    connection = sqlite3.connect(db.DB_PATH)
    connection.execute("PRAGMA case_sensitive_like=ON")
    rows = connection.execute(
        "SELECT english_normalized FROM entries WHERE english_normalized <> '' LIMIT 2500"
    ).fetchall()
    terms = {
        "VCR", "M C", "tetragram", "tetragram of the",
        "Down's syndrome", "COVID-19", "3D printing",
    }
    while len(terms) < 5000:
        text = random.choice(rows)[0]
        if len(text) < 4:
            continue
        start = random.randrange(0, len(text) - 3)
        end = random.randrange(start + 3, min(len(text), start + 18) + 1)
        term = text[start:end].strip()
        if len(term) < 3:
            continue
        terms.add(term)
        terms.add(term.lower())
        terms.add(term.upper())
        terms.add(term.title())

    def old(term):
        return [
            row[0]
            for row in connection.execute(
                "SELECT rowid FROM entries_fts_trigram "
                "WHERE lower(english_normalized) LIKE ? ORDER BY rowid",
                ("%" + term.lower() + "%",),
            )
        ]

    def new(term):
        if not (
            len(term) >= 3
            and term.isascii()
            and not any(char in term for char in "%_\\")
        ):
            return old(term)
        match = 'english_normalized : "' + term.replace('"', '""') + '"'
        return [
            row[0]
            for row in connection.execute(
                "SELECT rowid FROM entries_fts_trigram "
                "WHERE english_normalized MATCH ? "
                "AND lower(english_normalized) LIKE ? ORDER BY rowid",
                (match, "%" + term.lower() + "%"),
            )
        ]

    for term in terms:
        assert new(term) == old(term), term
    connection.close()


def test_batched_head_readings_give_the_same_rows_as_one_query_per_headword(monkeypatch):
    """Looking source readings up in batches must not change which rows or readings come back.

    This is the side path that adds other dictionaries' words when the unified entries table is not in use
    (that table makes those words ordinary rows), so it is exercised with the unified table off.
    """
    from yabai_search import db

    monkeypatch.setattr(db, "_unified_ready_cache", False)
    def rows():
        return [
            (e["traditional"], e["simplified"], e["pinyin"], e.get("pinyin_label"))
            for e in db._source_only_entries("*乐*", limit=3000)
        ]

    batched = rows()
    monkeypatch.setenv("YABAI_HEAD_READINGS_LEGACY", "1")
    assert rows() == batched
    assert len(batched) > 50
