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
