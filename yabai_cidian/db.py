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
    """Search the dictionary.

    Matches the Chinese word (the simplified column; the query is converted to
    simplified first) and the English definitions. '*' matches zero or more
    characters. Ranking: exact Chinese word, then definitions that start with the
    query, then English whole-word matches (e.g. "country" before "countryside"),
    then shortest word and id.
    """
    query = query.strip()
    if not query:
        return []  # nothing to search for

    # Chinese: convert to simplified, and treat a plain query as a prefix search.
    chinese = _convert(query, _simplify_map())
    if "*" not in chinese:
        chinese += "*"
    pattern_chinese = _like_pattern(chinese)
    literal = chinese.replace("*", "")  # the "exact" word, ranked first

    # English: match anywhere in the definitions unless a "*" is given.
    english = query if "*" in query else f"*{query}*"
    pattern_english = _like_pattern(english)

    # Build the ORDER BY (it has its own parameters).
    order_parts = [
        "(simplified = ?) DESC",  # exact Chinese word
        "(english_normalized LIKE ? ESCAPE '\\') DESC",  # definition starts with query
    ]
    order_params = [literal, _like_pattern(f"{query}*")]
    if "*" not in query:
        # Whole-word boost via the FTS index (it is tokenized, so "country" does
        # not match "countryside"). Only for plain queries, not wildcards.
        order_parts.append(
            "(id IN (SELECT rowid FROM entries_fts "
            "WHERE entries_fts MATCH ?)) DESC"
        )
        order_params.append('"' + query.replace('"', '""') + '"')
    order_parts += ["length(simplified)", "id"]

    sql = (
        "SELECT simplified, traditional, pinyin, english, english_normalized "
        "FROM entries "
        "WHERE simplified LIKE ? ESCAPE '\\' "
        "   OR english_normalized LIKE ? ESCAPE '\\' "
        "ORDER BY " + ", ".join(order_parts) + " "
        "LIMIT ?"
    )
    params = [pattern_chinese, pattern_english, *order_params, limit]

    connection = connect()
    try:
        rows = connection.execute(sql, params).fetchall()
    finally:
        connection.close()  # always close, even if the query raised
    # Convert sqlite3.Row objects into plain dicts for the JSON response.
    return [dict(row) for row in rows]
