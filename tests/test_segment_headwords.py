"""The segmenter's headword list follows the dictionary registry."""

from __future__ import annotations

import sqlite3

import pytest

from yabai_search import db, segment


def _database(path, table, rows):
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"CREATE TABLE {table} (word TEXT, traditional TEXT)")
        connection.executemany(
            f"INSERT INTO {table} (word, traditional) VALUES (?, ?)", rows
        )
        connection.commit()
    finally:
        connection.close()


def test_source_headwords_reads_every_registry_source(tmp_path, monkeypatch):
    """Every readable registry entry contributes; broken/missing ones are skipped."""
    plain = tmp_path / "plain.sqlite"
    both = tmp_path / "both.sqlite"
    _database(plain, "words", [("甲甲", "甲甲"), ("单", "单")])
    _database(both, "words", [("乙乙", "乙乙"), ("丙丙", "丙丙")])

    monkeypatch.setattr(
        db,
        "_SOURCE_QUERIES",
        [
            {"anchor": "plain", "database": plain, "all_sql": "SELECT word FROM words"},
            {
                "anchor": "both",
                "database": both,
                "segment_sql": "SELECT word FROM words "
                               "UNION SELECT traditional FROM words",
            },
            {
                "anchor": "missing",
                "database": tmp_path / "missing.sqlite",
                "all_sql": "SELECT word FROM words",
            },
            {"anchor": "broken", "database": plain, "all_sql": "SELECT nope FROM words"},
            {"anchor": "no-sql", "database": plain},
        ],
    )

    # Single characters are dropped; the missing/broken sources do not fail the
    # whole list, and a source with neither query is ignored.
    assert db.source_headwords() == {"甲甲", "乙乙", "丙丙"}
    assert db.source_database_paths() == [
        plain,
        both,
        tmp_path / "missing.sqlite",
        plain,
        plain,
    ]


def test_source_headwords_can_include_single_characters(tmp_path, monkeypatch):
    plain = tmp_path / "plain.sqlite"
    _database(plain, "words", [("单", "单")])

    monkeypatch.setattr(
        db,
        "_SOURCE_QUERIES",
        [{"anchor": "plain", "database": plain, "all_sql": "SELECT word FROM words"}],
    )

    assert db.source_headwords() == set()
    # Both scripts are included even for a one-character headword.
    assert db.source_headwords(min_length=1) == {"单", "單"}


def test_segment_merges_a_registry_headword(monkeypatch):
    """A headword the segmenter knows is rejoined even if jieba splits it."""
    monkeypatch.setattr(segment, "_headword_cache", {"缩头缩脑"})
    monkeypatch.setattr(segment, "_max_len_cache", 4)
    monkeypatch.setattr(segment, "_ensure_loaded", lambda: None)
    monkeypatch.setattr(segment.jieba, "cut", lambda _text: iter(["缩头", "缩", "脑"]))

    assert segment.segment("缩头缩脑") == ["缩头缩脑"]


def test_userdict_skips_words_with_whitespace(tmp_path, monkeypatch):
    """jieba splits a user-dict line on whitespace, so those words cannot be written."""
    monkeypatch.setattr(segment, "_headword_cache", {"甲甲", "bi bu车"})
    monkeypatch.setattr(segment, "USERDICT_PATH", tmp_path / "userdict.txt")
    monkeypatch.setattr(segment, "USERDICT_VERSION_PATH", tmp_path / "userdict.version")

    assert segment.build_userdict() == 1
    assert (tmp_path / "userdict.txt").read_text(encoding="utf-8") == "甲甲 20000\n"
    assert (tmp_path / "userdict.version").read_text(
        encoding="utf-8"
    ) == f"{segment.USERDICT_VERSION}\n"


@pytest.mark.skipif(not db.MOE_DB_PATH.exists(), reason="MOE database not built")
def test_source_headwords_normalizes_a_traditional_moe_headword(monkeypatch):
    """MOE stores 縮頭縮腦, but a simplified query must still find it.

    Baidu is removed from the registry so this cannot pass on Baidu's
    simplified copy: the simplified form has to come from OpenCC.
    """
    sources = [
        source for source in db._SOURCE_QUERIES if source["anchor"] != "baidu"
    ]
    monkeypatch.setattr(db, "_SOURCE_QUERIES", sources)

    words = db.source_headwords()

    assert "縮頭縮腦" in words
    assert "缩头缩脑" in words
