"""SQLite access for the dictionary."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from yabai_chaci.pinyin import numeric_to_marked, to_toneless

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
    """Open a fresh SQLite connection whose rows behave like dicts.

    The MOE database is attached as "moe" (when present) so a query can also
    match Taiwan readings.
    """
    connection = sqlite3.connect(DB_PATH)
    # Row factory lets us read columns by name, e.g. row["pinyin"].
    connection.row_factory = sqlite3.Row
    if MOE_DB_PATH.exists():
        connection.execute("ATTACH DATABASE ? AS moe", (str(MOE_DB_PATH),))
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


def _word_taiwan(pinyin: str, options: list[str]) -> str | None:
    """Return the Taiwan reading for a whole word from its MOE readings.

    An exact MOE word match always wins. A single reading is used as-is (even if
    it differs from CEDICT). With several (polyphonic), prefer the one matching
    the entry's reading exactly, then by toneless form, then MOE's first.
    """
    if not options:
        return None
    if len(options) == 1:
        return options[0]
    lowered = pinyin.lower()
    for option in options:
        if option.lower() == lowered:
            return option
    target = to_toneless(pinyin)
    for option in options:
        if to_toneless(option) == target:
            return option
    return options[0]


def _char_taiwan(syllable: str, options: list[str]) -> str | None:
    """Return a character's Taiwan reading for the per-character fallback.

    If MOE lists a single reading for the character, use it as-is (even when it
    differs from the CEDICT syllable). With several readings, use one only if
    exactly one matches the syllable's toneless form.
    """
    if len(options) == 1:
        return options[0]
    target = to_toneless(syllable)
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
        replacement = _char_taiwan(syllable, char_map.get(char, []))
        if replacement:
            result.append(replacement)
            changed = True
        else:
            result.append(syllable)
    return " ".join(result) if changed else None


# CEDICT cross-references: "trad|simp[pinyin]" or "word[pinyin]".
_CROSS_REF_RE = re.compile(r"([^\s|\[\]:/]+)(?:\|([^\s|\[\]:/]+))?\[([^\]]+)\]")


def _cross_refs(text: str) -> list[tuple[str, str, str]]:
    """(traditional, simplified, marked pinyin) for each cross-reference."""
    return [
        (m.group(1), m.group(2) or m.group(1), numeric_to_marked(m.group(3)))
        for m in _CROSS_REF_RE.finditer(text or "")
    ]


def _attach_taiwan(entries: list[dict]) -> list[dict]:
    """Fill in Taiwan readings: each entry's own word and its cross-references.

    An exact whole-word MOE match always wins; only words MOE doesn't list fall
    back to per-character readings.
    """
    char_map = _moe_char_map()

    # The entry's own word.
    by_word = _moe_readings([entry["traditional"] for entry in entries])
    for entry in entries:
        taiwan = _word_taiwan(entry["pinyin"], by_word.get(entry["traditional"], []))
        if taiwan is None:
            taiwan = _derive_taiwan(entry["traditional"], entry["pinyin"], char_map)
        entry["pinyin_taiwan"] = taiwan

    # Words referenced inside the definitions (they render as furigana too).
    referenced = {}
    for entry in entries:
        for traditional, simplified, pinyin in _cross_refs(entry.get("english_normalized")):
            referenced.setdefault(simplified, (traditional, pinyin))
    ref_readings = _moe_readings([traditional for traditional, _ in referenced.values()])
    for entry in entries:
        taiwan_refs = {}
        for traditional, simplified, pinyin in _cross_refs(entry.get("english_normalized")):
            reading = _word_taiwan(pinyin, ref_readings.get(traditional, []))
            if reading is None:
                reading = _derive_taiwan(traditional, pinyin, char_map)
            if reading:
                taiwan_refs[simplified] = reading
        entry["taiwan_refs"] = taiwan_refs
    return entries


def lookup(word: str) -> list[dict]:
    """Return entries whose simplified or traditional form is exactly `word`."""
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT simplified, traditional, pinyin, english, english_normalized "
            "FROM entries WHERE simplified = ? OR traditional = ? ORDER BY id",
            (word, word),
        ).fetchall()
    finally:
        connection.close()
    return _attach_taiwan([dict(row) for row in rows])


def _has_english_match(query: str) -> bool:
    """True when `query` matches a definition as a whole word (via the FTS index).

    Used to tell a real English word (e.g. "word") apart from a pinyin reading.
    """
    connection = connect()
    try:
        row = connection.execute(
            "SELECT 1 FROM entries_fts WHERE entries_fts MATCH ? LIMIT 1",
            ('"' + query.replace('"', '""') + '"',),
        ).fetchone()
    finally:
        connection.close()
    return row is not None


def search(query: str, limit: int = 50) -> list[dict]:
    """Search the dictionary.

    A "quoted" query is an English phrase search (whole words, via the FTS
    index). Otherwise the query matches the Chinese word (simplified column; the
    query is converted to simplified first), the English definitions, and the
    toneless pinyin reading (e.g. "dangan" finds 单干 and 档案). '*' matches zero
    or more characters.
    """
    query = query.strip()
    if not query:
        return []  # nothing to search for

    # "Quoted" -> English phrase search only.
    if len(query) >= 2 and query.startswith('"') and query.endswith('"'):
        phrase = query[1:-1].strip()
        if not phrase:
            return []
        sql = (
            "SELECT simplified, traditional, pinyin, english, english_normalized "
            "FROM entries WHERE id IN ("
            "    SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?"
            ") ORDER BY length(simplified), id LIMIT ?"
        )
        params = ['"' + phrase.replace('"', '""') + '"', limit]
        connection = connect()
        try:
            rows = connection.execute(sql, params).fetchall()
        finally:
            connection.close()
        return _attach_taiwan([dict(row) for row in rows])

    # Pinyin with tone numbers (e.g. "bang1"): match the reading. Mainland first,
    # then Taiwan, then alternate pronunciations embedded in the definition.
    if re.fullmatch(r"[A-Za-zü:]+[1-5](?:\s+[A-Za-zü:]+[1-5])*", query):
        numeric = " ".join(syllable.lower() for syllable in query.split())
        marked = numeric_to_marked(numeric)
        mainland = f"% {numeric} %"  # pinyin_numeric wrapped in spaces
        taiwan = f"% {marked} %"  # MOE pinyin is tone-marked
        alt = f"%[{numeric}]%"  # CEDICT alternate reading, e.g. "[bang1]"
        if MOE_DB_PATH.exists():
            sql = (
                "SELECT simplified, traditional, pinyin, english, english_normalized "
                "FROM entries WHERE (' ' || pinyin_numeric || ' ') LIKE ? ESCAPE '\\' "
                " OR traditional IN (SELECT word FROM moe.moe "
                "    WHERE (' ' || pinyin || ' ') LIKE ? ESCAPE '\\') "
                " OR english_normalized LIKE ? ESCAPE '\\' "
                "ORDER BY "
                " ((' ' || pinyin_numeric || ' ') LIKE ? ESCAPE '\\') DESC, "
                " (traditional IN (SELECT word FROM moe.moe "
                "    WHERE (' ' || pinyin || ' ') LIKE ? ESCAPE '\\')) DESC, "
                " (english_normalized LIKE ? ESCAPE '\\') DESC, "
                " length(simplified), id LIMIT ?"
            )
            params = [mainland, taiwan, alt, mainland, taiwan, alt, limit]
        else:
            sql = (
                "SELECT simplified, traditional, pinyin, english, english_normalized "
                "FROM entries WHERE (' ' || pinyin_numeric || ' ') LIKE ? ESCAPE '\\' "
                " OR english_normalized LIKE ? ESCAPE '\\' "
                "ORDER BY "
                " ((' ' || pinyin_numeric || ' ') LIKE ? ESCAPE '\\') DESC, "
                " (english_normalized LIKE ? ESCAPE '\\') DESC, "
                " length(simplified), id LIMIT ?"
            )
            params = [mainland, alt, mainland, alt, limit]
        connection = connect()
        try:
            rows = connection.execute(sql, params).fetchall()
        finally:
            connection.close()
        return _attach_taiwan([dict(row) for row in rows])

    # Exact pinyin reading (e.g. "yabai" -> 牙白): if the query is a complete
    # Chinese pronunciation, show only those matches and skip English results.
    pinyin_key = to_toneless(query)
    if pinyin_key:
        sql = (
            "SELECT simplified, traditional, pinyin, english, english_normalized "
            "FROM entries WHERE pinyin_toneless = ? "
            "ORDER BY length(simplified), id LIMIT ?"
        )
        connection = connect()
        try:
            rows = connection.execute(sql, (pinyin_key, limit)).fetchall()
        finally:
            connection.close()
        # A latin query that is also a real English word (e.g. "word") should
        # search the definitions, not be read as a pinyin reading.
        if rows and not _has_english_match(query):
            return _attach_taiwan([dict(row) for row in rows])

    # Chinese: convert to simplified, and treat a plain query as a prefix search.
    chinese = _convert(query, _simplify_map())
    if "*" not in chinese:
        chinese += "*"
    pattern_chinese = _like_pattern(chinese)
    literal = chinese.replace("*", "")  # the "exact" word, ranked first

    # Only a query containing hanzi can match the Chinese word column; a latin
    # query is pinyin or English and must not match latin headwords like "word".
    is_chinese = any("\u3400" <= char <= "\u9fff" for char in literal)

    # English: match anywhere in the definitions unless a "*" is given.
    english = query if "*" in query else f"*{query}*"
    pattern_english = _like_pattern(english)

    # WHERE: the Chinese word (hanzi queries only) or the definitions.
    where_parts = ["english_normalized LIKE ? ESCAPE '\\'"]
    where_params = [pattern_english]
    if is_chinese:
        where_parts.insert(0, "simplified LIKE ? ESCAPE '\\'")
        where_params.insert(0, pattern_chinese)

    # ORDER BY tiers (with their own parameters).
    order_parts = []
    order_params = []
    if is_chinese:
        order_parts.append("(simplified = ?) DESC")  # exact Chinese word
        order_params.append(literal)
        order_parts.append("(simplified LIKE ? ESCAPE '\\') DESC")  # word column
        order_params.append(pattern_chinese)
    order_parts.append("(english_normalized LIKE ? ESCAPE '\\') DESC")
    order_params.append(_like_pattern(f"{query}*"))
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
        "FROM entries WHERE " + " OR ".join(where_parts) + " "
        "ORDER BY " + ", ".join(order_parts) + " LIMIT ?"
    )
    params = [*where_params, *order_params, limit]

    connection = connect()
    try:
        rows = connection.execute(sql, params).fetchall()
    finally:
        connection.close()  # always close, even if the query raised

    return _attach_taiwan([dict(row) for row in rows])
