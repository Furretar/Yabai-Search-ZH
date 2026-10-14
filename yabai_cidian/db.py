"""SQLite access for the dictionary."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

from yabai_cidian.pinyin import to_toneless

# Path to the generated CEDICT database: <project root>/data/cedict.sqlite.
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cedict.sqlite"

# Path to the MOE dictionary (Taiwan readings), built by scripts/import_moe.py.
MOE_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "moe.sqlite"

# Cache of the traditional -> simplified character map, built once from the
# dictionary's single-character entries. None until built.
_simplify_cache: dict[str, str] | None = None

# Cache of MOE single-character readings: {character: [pinyin, ...]}.
_moe_char_cache: dict[str, list[str]] | None = None


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


def _moe_readings(words: list[str]) -> dict[str, list[str]]:
    """Return {word: [Taiwan pinyin, ...]} for `words` from the MOE database."""
    if not words or not MOE_DB_PATH.exists():
        return {}
    placeholders = ",".join("?" * len(words))
    connection = sqlite3.connect(MOE_DB_PATH)
    try:
        rows = connection.execute(
            f"SELECT DISTINCT word, pinyin FROM moe "
            f"WHERE word IN ({placeholders}) AND pinyin <> ''",
            words,
        ).fetchall()
    finally:
        connection.close()
    by_word: dict[str, list[str]] = defaultdict(list)
    for word, pinyin in rows:
        by_word[word].append(pinyin)
    return by_word


def _pick_taiwan(pinyin: str, options: list[str]) -> str | None:
    """Choose the MOE reading that matches `pinyin` (same toneless syllables).

    Returns None unless exactly one candidate matches — polyphonic characters
    are dropped rather than guessed (err toward missing a reading).
    """
    target = to_toneless(pinyin)
    matches = {option for option in options if to_toneless(option) == target}
    return matches.pop() if len(matches) == 1 else None


def _moe_char_map() -> dict[str, list[str]]:
    """Return {character: [Taiwan readings]} from MOE single-character entries."""
    global _moe_char_cache
    if _moe_char_cache is None:
        mapping: dict[str, list[str]] = defaultdict(list)
        if MOE_DB_PATH.exists():
            connection = sqlite3.connect(MOE_DB_PATH)
            try:
                rows = connection.execute(
                    "SELECT word, pinyin FROM moe "
                    "WHERE length(word) = 1 AND pinyin <> ''"
                ).fetchall()
            finally:
                connection.close()
            for word, pinyin in rows:
                mapping[word].append(pinyin)
        _moe_char_cache = mapping
    return _moe_char_cache


def _derive_taiwan(traditional: str, pinyin: str, char_map: dict[str, list[str]]) -> str | None:
    """Build a Taiwan reading character by character from MOE.

    Used for words the MOE dictionary doesn't list whole. Each character is
    swapped for its MOE reading only when that reading is unambiguous (exactly
    one matches the syllable). Returns None when nothing changed.
    """
    chars = list(traditional)
    syllables = pinyin.split()
    if len(chars) != len(syllables):
        return None
    changed = False
    result = []
    for char, syllable in zip(chars, syllables):
        replacement = _pick_taiwan(syllable, char_map.get(char, []))
        if replacement:
            result.append(replacement)
            changed = True
        else:
            result.append(syllable)
    return " ".join(result) if changed else None


def search(query: str, limit: int = 50) -> list[dict]:
    """Search the dictionary.

    Matches the Chinese word (the simplified column; the query is converted to
    simplified first) and the English definitions. '*' matches zero or more
    characters. Ranking: exact Chinese word, then word-column match, then
    definitions, then shortest word and id.
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
        "(simplified LIKE ? ESCAPE '\\') DESC",  # matches the Chinese word column
        "(english_normalized LIKE ? ESCAPE '\\') DESC",  # definition starts with query
    ]
    order_params = [literal, pattern_chinese, _like_pattern(f"{query}*")]
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

    entries = [dict(row) for row in rows]

    # Taiwan readings come from the MOE dictionary: prefer a whole-word match,
    # then fall back to per-character readings for words MOE doesn't list.
    by_word = _moe_readings([entry["traditional"] for entry in entries])
    char_map = _moe_char_map()
    for entry in entries:
        options = by_word.get(entry["traditional"], [])
        taiwan = _pick_taiwan(entry["pinyin"], options)
        if taiwan is None:
            taiwan = _derive_taiwan(entry["traditional"], entry["pinyin"], char_map)
        entry["pinyin_taiwan"] = taiwan
    return entries
