"""SQLite access for the dictionary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Import the pinyin normalizer so query-time and import-time keys agree.
from yabai_cidian.pinyin import to_toneless

# Path to the generated database: <project root>/data/cedict.sqlite.
# parents[1] walks up from yabai_cidian/db.py to the project root.
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cedict.sqlite"


def connect() -> sqlite3.Connection:
    """Open a fresh SQLite connection whose rows behave like dicts."""
    connection = sqlite3.connect(DB_PATH)
    # Row factory lets us read columns by name, e.g. row["pinyin"].
    connection.row_factory = sqlite3.Row
    return connection


def search(query: str, limit: int = 50) -> list[dict]:
    """Return entries matching `query` across hanzi, pinyin, and English."""
    query = query.strip()
    if not query:
        return []  # nothing to search for

    # LIKE pattern: "%x%" means "contains x".
    like = f"%{query}%"
    # Base conditions: exact or partial match on the Chinese columns and English.
    conditions = [
        "simplified = ?",
        "traditional = ?",
        "simplified LIKE ?",
        "traditional LIKE ?",
        "pinyin_numeric LIKE ?",
        "english LIKE ?",
    ]
    # One parameter per "?" above, in the same order as the conditions.
    params: list = [query, query, like, like, like, like]

    # Also match pinyin typed without tones (e.g. "nihao" -> "nǐ hǎo").
    # to_toneless returns "" for non-pinyin input, so we only add these two
    # conditions when there is a real pinyin key (otherwise they'd match everything).
    pinyin_key = to_toneless(query)
    if pinyin_key:
        conditions.append("pinyin_toneless = ?")
        conditions.append("pinyin_toneless LIKE ?")
        params.extend([pinyin_key, f"%{pinyin_key}%"])

    # Build the SQL. Conditions are OR-ed so any match counts. Results are
    # ordered shortest-first as a crude "most likely useful" heuristic.
    sql = (
        "SELECT traditional, simplified, pinyin, pinyin_numeric, english "
        "FROM entries WHERE " + " OR ".join(conditions) + " "
        "ORDER BY length(simplified) LIMIT ?"
    )
    params.append(limit)  # fill the LIMIT ? placeholder

    connection = connect()
    try:
        rows = connection.execute(sql, params).fetchall()
    finally:
        connection.close()  # always close, even if the query raised
    # Convert sqlite3.Row objects into plain dicts for the JSON response.
    return [dict(row) for row in rows]
