"""SQLite access for the dictionary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Path to the generated database: <project root>/data/cedict.sqlite.
# parents[1] walks up from yabai_cidian/db.py to the project root.
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cedict.sqlite"

# Cache of the traditional -> simplified character map, built once from the
# dictionary's single-character entries. None until built.
_simplify_cache: dict[str, str] | None = None


def connect() -> sqlite3.Connection:
    """Open a fresh SQLite connection whose rows behave like dicts."""
    connection = sqlite3.connect(DB_PATH)
    # Row factory lets us read columns by name, e.g. row["pinyin"].
    connection.row_factory = sqlite3.Row
    return connection


def _simplify_map() -> dict[str, str]:
    """Return the traditional -> simplified character map, derived from the data.

    We use CEDICT's own single-character entries (where the traditional and
    simplified forms are exactly one character each) to learn the mapping. The
    result is cached after the first call.
    """
    global _simplify_cache
    if _simplify_cache is None:
        connection = connect()
        try:
            rows = connection.execute(
                "SELECT traditional, simplified FROM entries "
                "WHERE length(traditional) = 1 AND length(simplified) = 1 "
                "AND traditional <> simplified"
            ).fetchall()
        finally:
            connection.close()
        _simplify_cache = {row["traditional"]: row["simplified"] for row in rows}
    return _simplify_cache


def _convert(text: str, mapping: dict[str, str]) -> str:
    """Rewrite each character using `mapping`, leaving unknown characters as-is."""
    return "".join(mapping.get(char, char) for char in text)


def _like_pattern(text: str) -> str:
    """Turn a user query into a SQL LIKE pattern.

    '*' is the wildcard and becomes '%', which matches zero or more characters,
    so "*a*" also matches just "a". Literal '%', '_' and '\\' are escaped so they
    match themselves rather than acting as wildcards.
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.replace("*", "%")


def search(query: str, limit: int = 50) -> list[dict]:
    """Search the simplified dictionary.

    The query is converted to simplified characters first, then matched against
    the simplified column. Ranking: exact match first, then shortest, then id.
    Wildcards: '*' matches zero or more characters.
      - no '*'   -> prefix search: "国" finds 国, then 国家, 国民, ...
      - '*国'    -> suffix search: finds 国, then 中国, 美国, ...
      - '*国*'   -> contains
    """
    query = query.strip()
    if not query:
        return []  # nothing to search for

    # Convert the whole query to simplified before searching.
    query = _convert(query, _simplify_map())

    # A query with no "*" is a prefix search: add our own trailing wildcard.
    if "*" not in query:
        query += "*"

    pattern = _like_pattern(query)
    literal = query.replace("*", "")  # the "exact" word, ranked first

    sql = (
        "SELECT simplified, traditional, pinyin, english, english_normalized "
        "FROM entries "
        "WHERE simplified LIKE ? ESCAPE '\\' "
        "ORDER BY (simplified = ?) DESC, length(simplified), id "
        "LIMIT ?"
    )

    connection = connect()
    try:
        rows = connection.execute(sql, (pattern, literal, limit)).fetchall()
    finally:
        connection.close()  # always close, even if the query raised
    # Convert sqlite3.Row objects into plain dicts for the JSON response.
    return [dict(row) for row in rows]
