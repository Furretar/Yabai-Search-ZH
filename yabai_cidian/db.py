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


def search(query: str, limit: int = 50) -> list[dict]:
    """Return entries matching `query`, tolerating mixed simplified/traditional."""
    query = query.strip()
    if not query:
        return []  # nothing to search for

    # Rewrite the query into a pure simplified form and a pure traditional form,
    # then match each against the matching column. This way a mixed query like
    # "汉語" (simp + trad) still finds the entry, and pure queries keep working.
    trad_to_simp, simp_to_trad = _script_maps()
    as_simplified = _convert(query, trad_to_simp)
    as_traditional = _convert(query, simp_to_trad)

    sql = (
        "SELECT simplified, traditional, pinyin, english, english_normalized "
        "FROM entries WHERE simplified = ? OR traditional = ? "
        "ORDER BY id LIMIT ?"
    )

    connection = connect()
    try:
        rows = connection.execute(
            sql, (as_simplified, as_traditional, limit)
        ).fetchall()
    finally:
        connection.close()  # always close, even if the query raised
    # Convert sqlite3.Row objects into plain dicts for the JSON response.
    return [dict(row) for row in rows]
