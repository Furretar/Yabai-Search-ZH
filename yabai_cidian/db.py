"""SQLite access for the dictionary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
    """Return entries whose word matches `query` exactly (simplified OR traditional)."""
    query = query.strip()
    if not query:
        return []  # nothing to search for

    # Exact match only for now: the word equals the simplified OR traditional form.
    # So searching "汉语" and "漢語" both find the same entry.
    sql = (
        "SELECT simplified, traditional, pinyin, english, english_normalized "
        "FROM entries WHERE simplified = ? OR traditional = ? "
        "ORDER BY id LIMIT ?"
    )

    connection = connect()
    try:
        rows = connection.execute(sql, (query, query, limit)).fetchall()
    finally:
        connection.close()  # always close, even if the query raised
    # Convert sqlite3.Row objects into plain dicts for the JSON response.
    return [dict(row) for row in rows]
