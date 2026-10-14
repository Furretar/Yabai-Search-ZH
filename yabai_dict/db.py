"""SQLite access for the dictionary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from yabai_dict.pinyin import to_toneless

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cedict.sqlite"


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def search(query: str, limit: int = 50) -> list[dict]:
    query = query.strip()
    if not query:
        return []

    like = f"%{query}%"
    conditions = [
        "simplified = ?",
        "traditional = ?",
        "simplified LIKE ?",
        "traditional LIKE ?",
        "pinyin_numeric LIKE ?",
        "english LIKE ?",
    ]
    params: list = [query, query, like, like, like, like]

    pinyin_key = to_toneless(query)
    if pinyin_key:
        conditions.append("pinyin_toneless = ?")
        conditions.append("pinyin_toneless LIKE ?")
        params.extend([pinyin_key, f"%{pinyin_key}%"])

    sql = (
        "SELECT traditional, simplified, pinyin, pinyin_numeric, english "
        "FROM entries WHERE " + " OR ".join(conditions) + " "
        "ORDER BY length(simplified) LIMIT ?"
    )
    params.append(limit)

    connection = connect()
    try:
        rows = connection.execute(sql, params).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]
