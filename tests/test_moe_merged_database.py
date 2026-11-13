"""Both MOE dictionaries live in one SQLite file (data/dictionary-databases/moe-taiwan/moe.sqlite).

The concise dictionary keeps the plain table names (moe, senses, ...); the
comprehensive one has the same tables with a "rev_" prefix. These tests use tiny
files, so they do not need the real data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from yabai_search import db


def make_file(path: Path, rows: dict[str, list[tuple[str, str]]]) -> None:
    """Create a merged-style file: one (word, pinyin) table per name in `rows`."""
    connection = sqlite3.connect(path)
    for table, values in rows.items():
        connection.execute(f"CREATE TABLE {table} (word TEXT, pinyin TEXT)")
        connection.executemany(f"INSERT INTO {table} VALUES (?, ?)", values)
    connection.commit()
    connection.close()


@pytest.fixture
def moe_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "moe.sqlite"
    monkeypatch.setattr(db, "MOE_DB_PATH", path)
    monkeypatch.setattr(db, "_moe_prefix_cache", None)
    return path


def test_prefixes_follow_the_tables_present(tmp_path, monkeypatch) -> None:
    # Two separate files: the app opens each database once and read-only, so a
    # table added later to the same file would not be seen (a re-import replaces
    # the whole file instead).
    concise_only = tmp_path / "concise.sqlite"
    both = tmp_path / "both.sqlite"
    make_file(concise_only, {"moe": [("血", "xuè")]})
    make_file(both, {"moe": [("血", "xuè")], "rev_moe": [("血", "xiě")]})

    monkeypatch.setattr(db, "MOE_DB_PATH", tmp_path / "missing.sqlite")
    monkeypatch.setattr(db, "_moe_prefix_cache", None)
    assert db._moe_prefixes() == []  # no file at all

    monkeypatch.setattr(db, "MOE_DB_PATH", concise_only)
    assert db._moe_prefixes() == [""]

    monkeypatch.setattr(db, "MOE_DB_PATH", both)
    assert db._moe_prefixes() == ["", "rev_"]


def test_comprehensive_dictionary_is_absent_in_a_file_from_before_the_merge(
    moe_file,
) -> None:
    """A host with new code but an old concise-only file must skip the revised source."""
    make_file(moe_file, {"moe": [("血", "xuè")]})
    concise = {"anchor": "moe", "database": moe_file}
    revised = {"anchor": "moe_revised", "database": moe_file}
    assert db._source_present(concise)
    assert not db._source_present(revised)


def test_a_missing_file_means_no_source(moe_file) -> None:
    assert not db._source_present({"anchor": "moe", "database": moe_file})
    assert not db._source_present({"anchor": "moe_revised", "database": moe_file})


def test_query_runs_on_each_dictionary_concise_first(moe_file) -> None:
    make_file(
        moe_file,
        {"moe": [("血", "xuè")], "rev_moe": [("血", "xiě"), ("血液", "xuè yè")]},
    )
    rows = db._moe_query("SELECT word, pinyin FROM @moe ORDER BY word")
    assert rows == [("血", "xuè"), ("血", "xiě"), ("血液", "xuè yè")]


def test_readings_come_from_both_dictionaries(moe_file) -> None:
    make_file(
        moe_file,
        {"moe": [("血", "xuè")], "rev_moe": [("血", "xiě"), ("血", "xuè")]},
    )
    assert db._moe_readings(["血"]) == {"血": ["xuè", "xiě"]}


def test_both_sources_use_the_same_file_with_prefixed_tables() -> None:
    concise = next(item for item in db._SOURCE_QUERIES if item["anchor"] == "moe")
    revised = next(item for item in db._SOURCE_QUERIES if item["anchor"] == "moe_revised")
    assert concise["database"] == revised["database"] == db.MOE_DB_PATH
    assert "FROM moe " in concise["pinyin_rows_sql"]
    assert "FROM rev_moe " in revised["pinyin_rows_sql"]
    assert "rev_senses_fts" in revised["head_fts_sql"]
    assert "rev_classifiers" in revised["cls_sql"]
    # Each dictionary has its own date in the shared meta table.
    assert concise.get("updated_key") is None
    assert revised["updated_key"] == "rev_updated"
