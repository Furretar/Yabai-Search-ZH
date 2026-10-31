"""Tests for scripts/build_test_dbs.py, the small stand-in databases.

The builder exists so optimisation runs can use tiny copies of every dictionary
instead of the ~1.4 GB of real data; these tests pin the schema-preserving copy
and the YABAI_DATA_DIR switch that points the app at the copies.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from scripts.build_test_dbs import build_all, copy_database

ROOT = Path(__file__).resolve().parents[1]

WORDS = ["apple", "banana", "cherry", "date", "elder", "fig", "grape", "kiwi"]


def _make_source(path: Path) -> None:
    """A miniature dictionary: a table, both kinds of fts5 table, and extras."""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE entries (id INTEGER PRIMARY KEY, word TEXT, gloss TEXT);
        CREATE TABLE senses (word TEXT, reading TEXT, text TEXT);
        CREATE VIRTUAL TABLE entries_fts USING fts5(
            gloss, content='entries', content_rowid='id'
        );
        CREATE VIRTUAL TABLE word_fts USING fts5(word);
        CREATE INDEX entries_word ON entries(word);
        CREATE VIEW words AS SELECT word FROM entries;
        CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN
            INSERT INTO entries_fts(rowid, gloss) VALUES (new.id, new.gloss);
        END;
        """
    )
    connection.executemany(
        "INSERT INTO entries (word, gloss) VALUES (?, ?)",
        [(word, f"a {word} gloss") for word in WORDS],
    )
    connection.executemany(
        "INSERT INTO senses (word, reading, text) VALUES (?, ?, ?)",
        [(word, "reading", f"sense of {word}") for word in WORDS],
    )
    connection.executemany(
        "INSERT INTO word_fts (word) VALUES (?)", [(word,) for word in WORDS]
    )
    connection.commit()
    connection.close()


def _names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    finally:
        connection.close()


def test_copy_keeps_schema_and_limits_rows(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    _make_source(source)
    target = tmp_path / "out" / "source.sqlite"

    counts = copy_database(source, target, rows=3)

    assert counts["entries"] == 3
    connection = sqlite3.connect(target)
    try:
        assert connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3
        # External-content fts was rebuilt from the copied content table.
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM entries_fts WHERE entries_fts MATCH 'apple'"
            ).fetchone()[0]
            == 1
        )
        # A standalone fts table copies its own rows.
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM word_fts WHERE word_fts MATCH 'apple'"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()
    # Indexes, views and triggers survive the copy.
    assert {"entries_word", "words", "entries_ai"} <= _names(target)


def test_build_all_skips_derived_and_empty_databases(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _make_source(data / "cedict.sqlite")
    (data / "search_index.sqlite").write_bytes(b"")  # derived, never copied
    (data / "wikt.sqlite").write_bytes(b"")  # truncated leftover file
    # A derived index that remembers the CEDICT it was built from.
    derived = sqlite3.connect(data / "head_grams.sqlite")
    derived.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('cedict_sha256', 'built-from-the-real-file');"
    )
    derived.close()

    built = build_all(data, tmp_path / "dummy", rows=2)

    assert set(built) == {
        "cedict.sqlite",
        "head_grams.sqlite",
        "sense_counts.sqlite",
    }
    assert (tmp_path / "dummy" / "cedict.sqlite").is_file()
    assert (tmp_path / "dummy" / "sense_counts.sqlite").is_file()
    assert not (tmp_path / "dummy" / "search_index.sqlite").exists()
    # The sense counts are rebuilt from the dummy CEDICT, not copied stale.
    counts = sqlite3.connect(tmp_path / "dummy" / "sense_counts.sqlite")
    try:
        assert counts.execute("SELECT COUNT(*) FROM senses").fetchone()[0] == 2
    finally:
        counts.close()
    # The copy is stamped with the dummy CEDICT, so the app sees it as current
    # and uses the fast path instead of falling back to a live scan.
    stub = sqlite3.connect(tmp_path / "dummy" / "head_grams.sqlite")
    try:
        stored = stub.execute(
            "SELECT value FROM meta WHERE key = 'cedict_sha256'"
        ).fetchone()[0]
    finally:
        stub.close()
    expected = hashlib.sha256(
        (tmp_path / "dummy" / "cedict.sqlite").read_bytes()
    ).hexdigest()
    assert stored == expected


def test_yabai_data_dir_points_the_app_at_the_dummy_folder(tmp_path: Path) -> None:
    """The env var must move every database path, not just cedict."""
    env = dict(os.environ)
    env["YABAI_DATA_DIR"] = str(tmp_path)
    # The stub keeps the import from needing the compiled opencc library.
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "tests" / "_stubs"), str(ROOT)])
    code = (
        "from yabai_search import db;"
        "print(db.DATA_DIR);"
        "print(db.DB_PATH);"
        "print(db.MOE_DB_PATH);"
        "print(db.SEARCH_INDEX_DB_PATH);"
        "assert all(p.parent == db.DATA_DIR for p in "
        "(db.DB_PATH, db.MOE_DB_PATH, db.SEARCH_INDEX_DB_PATH))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    lines = result.stdout.strip().splitlines()
    assert lines[0] == str(tmp_path)
    assert lines[1] == str(tmp_path / "cedict.sqlite")
    assert lines[3] == str(tmp_path / "search_index.sqlite")
