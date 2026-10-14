"""SQLite access for the dictionary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Path to the generated database: <project root>/data/cedict.sqlite.
# parents[1] walks up from yabai_cidian/db.py to the project root.
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cedict.sqlite"

# Cache of character-level script maps, built once from single-character entries.
# Holds (traditional -> simplified, simplified -> traditional). None until built.
_script_cache: tuple[dict[str, str], dict[str, str]] | None = None


def connect() -> sqlite3.Connection:
    """Open a fresh SQLite connection whose rows behave like dicts."""
    connection = sqlite3.connect(DB_PATH)
    # Row factory lets us read columns by name, e.g. row["pinyin"].
    connection.row_factory = sqlite3.Row
    return connection


def _script_maps() -> tuple[dict[str, str], dict[str, str]]:
    """Return character maps (trad->simp, simp->trad), derived from the data.

    We use CEDICT's own single-character entries, where the traditional and
    simplified forms are exactly one character each, to learn the mapping. The
    result is cached after the first call.
    """
    global _script_cache
    if _script_cache is None:
        connection = connect()
        try:
            rows = connection.execute(
                "SELECT traditional, simplified FROM entries "
                "WHERE length(traditional) = 1 AND length(simplified) = 1 "
                "AND traditional <> simplified"
            ).fetchall()
        finally:
            connection.close()
        trad_to_simp = {row["traditional"]: row["simplified"] for row in rows}
        simp_to_trad = {row["simplified"]: row["traditional"] for row in rows}
        _script_cache = (trad_to_simp, simp_to_trad)
    return _script_cache


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

    Ranking: exact matches first, then the rest (shortest word first, then id).
    Wildcards: '*' matches zero or more characters.
      - no '*'   -> prefix search: "国" finds 国, then 国家, 国民, ...
      - '*X'     -> suffix search: "*国" finds 国, then 中国, 美国, ...
      - '*X*'    -> contains
    Simplified and traditional may be mixed in the query.
    """
    query = query.strip()
    if not query:
        return []  # nothing to search for

    trad_to_simp, simp_to_trad = _script_maps()

    # A query with no "*" is a prefix search: add our own trailing wildcard.
    # (Script conversion below leaves "*" untouched.)
    if "*" not in query:
        query += "*"

    # LIKE patterns in pure simplified and pure traditional form.
    pattern_simplified = _like_pattern(_convert(query, trad_to_simp))
    pattern_traditional = _like_pattern(_convert(query, simp_to_trad))

    # The "exact" word is the query with its wildcards removed; those rows sort
    # first. e.g. "*国" still puts 国 itself at the top.
    literal = query.replace("*", "")
    exact_simplified = _convert(literal, trad_to_simp)
    exact_traditional = _convert(literal, simp_to_trad)

    sql = (
        "SELECT simplified, traditional, pinyin, english, english_normalized "
        "FROM entries "
        "WHERE simplified LIKE ? ESCAPE '\\' "
        "   OR traditional LIKE ? ESCAPE '\\' "
        "ORDER BY (simplified = ? OR traditional = ?) DESC, "
        "         length(simplified), id "
        "LIMIT ?"
    )

    connection = connect()
    try:
        rows = connection.execute(
            sql,
            (
                pattern_simplified,
                pattern_traditional,
                exact_simplified,
                exact_traditional,
                limit,
            ),
        ).fetchall()
    finally:
        connection.close()  # always close, even if the query raised
    # Convert sqlite3.Row objects into plain dicts for the JSON response.
    return [dict(row) for row in rows]
