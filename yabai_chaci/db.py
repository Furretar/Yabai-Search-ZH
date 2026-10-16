"""SQLite access for the dictionary."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from yabai_chaci.pinyin import numeric_to_marked, to_numeric, to_toneless

# Path to the generated CEDICT database: <project root>/data/cedict.sqlite.
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cedict.sqlite"

# Path to the MOE dictionary (Taiwan readings), built by scripts/import_moe.py.
MOE_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "moe.sqlite"

# Path to the Wiktionary headword list (kaikki.org/Wiktextract), built by
# scripts/import_wiktionary.py.
WIKTIONARY_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "wiktionary.sqlite"

# --- debug log -------------------------------------------------------------
# While `start_log()` is active, every SQL statement run on a connection is
# captured (SQLite's trace callback) so the app can log the last few searches.
_ACTIVE_LOG: list[str] | None = None


def start_log() -> None:
    """Begin capturing the SQL statements executed from now on."""
    global _ACTIVE_LOG
    _ACTIVE_LOG = []


def end_log() -> list[str]:
    """Stop capturing and return the captured SQL statements."""
    global _ACTIVE_LOG
    statements = _ACTIVE_LOG or []
    _ACTIVE_LOG = None
    return statements


def _trace(statement: str) -> None:
    """SQLite trace callback: record each statement while logging is active."""
    if _ACTIVE_LOG is not None:
        _ACTIVE_LOG.append(statement)


def _connect(database) -> sqlite3.Connection:
    """Open a SQLite connection with the debug trace callback attached."""
    connection = sqlite3.connect(database)
    connection.set_trace_callback(_trace)
    return connection

# Which dictionaries contain a headword — drives the source chips, the
# per-dictionary definitions and the classifiers shown under a result. Each dict:
#   label      display name (chip / definition header)
#   anchor     id of its section on the About page
#   database   its SQLite file
#   sql        returns one column of headwords; the `IN (...)` word list appears
#              "repeat" times, so a query over two columns sets repeat=2
#   sense_sql  returns (word, position, text) numbered senses for the headword
#   cls_sql    returns (word, traditional, simplified, codes, note) classifiers
# Every source database has `senses` and `classifiers` tables of the same shape
# (see the importers), so these queries are identical across sources.
_SENSES_SQL = "SELECT word, position, text FROM senses WHERE word IN ({ph}) ORDER BY position"
_CLASSIFIERS_SQL = (
    "SELECT word, traditional, simplified, codes, note FROM classifiers "
    "WHERE word IN ({ph})"
)
# Variant forms ("see X" entries) — only some sources have a `forms` table. Each
# row maps a variant to its main form.
_FORMS_SQL = (
    "SELECT variant, traditional, main, pinyin FROM forms "
    "WHERE variant IN ({ph}) OR main IN ({ph})"
)
# Wiktionary stores its senses as glosses in the `wiktionary` table, not `senses`.
_WIKTIONARY_SENSES_SQL = (
    "SELECT word, 0 AS position, gloss AS text FROM wiktionary "
    "WHERE word IN ({ph}) AND gloss IS NOT NULL"
)

_SOURCE_QUERIES = [
    {
        "label": "CC-CEDICT",
        "anchor": "cedict",
        "lang": "english",
        "database": DB_PATH,
        "sql": "SELECT DISTINCT traditional AS word FROM entries WHERE traditional IN ({ph})"
               " UNION SELECT DISTINCT simplified FROM entries WHERE simplified IN ({ph})",
        "repeat": 2,
        "sense_sql": _SENSES_SQL,
        "cls_sql": _CLASSIFIERS_SQL,
        "forms_sql": _FORMS_SQL,
        "pinyin_sql": "SELECT pinyin FROM entries "
                      "WHERE ? IN (traditional, simplified) LIMIT 1",
    },
    {
        "label": "國語辭典簡編本",
        "anchor": "moe",
        "lang": "taiwan",
        "database": MOE_DB_PATH,
        "sql": "SELECT DISTINCT word FROM moe WHERE word IN ({ph})",
        "repeat": 1,
        "sense_sql": _SENSES_SQL,
        "cls_sql": _CLASSIFIERS_SQL,
        "pinyin_sql": "SELECT pinyin FROM moe WHERE word = ? AND pinyin <> '' LIMIT 1",
    },
    {
        "label": "Wiktionary",
        "anchor": "wiktionary",
        "lang": "english",
        "database": WIKTIONARY_DB_PATH,
        "sql": "SELECT DISTINCT word FROM wiktionary WHERE word IN ({ph})",
        "repeat": 1,
        "sense_sql": _WIKTIONARY_SENSES_SQL,
        "cls_sql": _CLASSIFIERS_SQL,
    },
]

def sources() -> list[dict]:
    """Each source's label, About-page anchor and last-updated date.

    The label/anchor drive the source chips and definition headers; `updated` is
    read from the source database's `meta` table (written by its importer) so the
    About page can show when the data was last refreshed. `updated` is None when
    the database isn't present yet.
    """
    info = []
    for source in _SOURCE_QUERIES:
        database = source["database"]
        updated = None
        if database.exists():
            try:
                connection = _connect(database)
                try:
                    row = connection.execute(
                        "SELECT value FROM meta WHERE key = 'updated'"
                    ).fetchone()
                    updated = row[0] if row else None
                finally:
                    connection.close()
            except sqlite3.Error:
                updated = None
        info.append(
            {"label": source["label"], "anchor": source["anchor"], "updated": updated}
        )
    return info


def dictionaries() -> list[dict]:
    """Every dictionary with its key, label, anchor and language group.

    Drives the Dictionaries panel (English / Mainland / Taiwan columns).
    """
    return [
        {
            "key": source["anchor"],
            "label": source["label"],
            "anchor": source["anchor"],
            "lang": source["lang"],
        }
        for source in _SOURCE_QUERIES
    ]


def _script_forms(word: str) -> list[str]:
    """`word` plus its traditional and simplified variants.

    Lets a traditional query (e.g. 馬) find a simplified entry (马) and vice
    versa. The character maps are derived from CEDICT's single-character entries.
    """
    simplify = _simplify_map()
    to_traditional = {simp: trad for trad, simp in simplify.items()}
    return sorted(
        {
            word,
            "".join(simplify.get(char, char) for char in word),  # -> simplified
            "".join(to_traditional.get(char, char) for char in word),  # -> traditional
        }
    )


def dictionary_matches(word: str) -> list[dict]:
    """Definitions for `word` from every dictionary, in the original language.

    Backs the "Matches in other dictionaries" section. Matching is
    script-insensitive (see _script_forms); each result carries both the
    traditional and simplified forms of the matched headword so the UI can show
    it in the chosen script.
    """
    word = (word or "").strip()
    if not word:
        return []
    forms = _script_forms(word)
    placeholders = ",".join("?" * len(forms))
    simplify = _simplify_map()
    to_traditional = {simp: trad for trad, simp in simplify.items()}
    matches = []
    for source in _SOURCE_QUERIES:
        if not source["database"].exists():
            continue
        connection = _connect(source["database"])
        try:
            rows = connection.execute(
                source["sql"].format(ph=placeholders),
                tuple(forms) * source["repeat"],
            ).fetchall()
            found = [row[0] for row in rows]
            if not found:
                continue
            head = word if word in found else found[0]
            senses = [
                text
                for _w, _p, text in connection.execute(
                    source["sense_sql"].format(ph="?"), (head,)
                )
                if text and text.strip()
            ]
            classifiers = [
                {"traditional": t, "simplified": s, "codes": c or "", "note": n or ""}
                for _w, t, s, c, n in connection.execute(
                    source["cls_sql"].format(ph="?"), (head,)
                )
            ]
            # The reading (tone-marked pinyin), so the UI can show furigana.
            pinyin = ""
            if source.get("pinyin_sql"):
                row = connection.execute(source["pinyin_sql"], (head,)).fetchone()
                pinyin = (row[0] or "") if row else ""
        finally:
            connection.close()
        if not senses and not classifiers:
            continue  # the word is listed but has nothing to show
        matches.append(
            {
                "key": source["anchor"],
                "label": source["label"],
                "anchor": source["anchor"],
                "lang": source["lang"],
                "traditional": "".join(to_traditional.get(char, char) for char in head),
                "simplified": "".join(simplify.get(char, char) for char in head),
                "pinyin": pinyin,
                "senses": senses[:_MAX_DEFS],
                "classifiers": classifiers[:_MAX_DEFS],
            }
        )
    return matches

# Cache of the traditional -> simplified character map, built once from the
# dictionary's single-character entries. None until built.
_simplify_cache: dict[str, str] | None = None

# Cache of MOE single-character readings: {character: [pinyin, ...]}.
_moe_char_cache: dict[str, list[str]] | None = None

# Cache of {toneless reading: {word, ...}} from MOE, for Taiwan pinyin search.
_moe_toneless_cache: dict[str, set[str]] | None = None

# Cache of {zhuyin syllable: numeric pinyin}, learned from MOE. None until built.
_zhuyin_cache: dict[str, str] | None = None

# Cache of {word: number of distinct CEDICT readings}. None until built.
_reading_count_cache: dict[str, int] | None = None

# Words that never take a Taiwan reading — the word-level MOE reading does not
# fit their CEDICT sense. Add more here as needed.
_TAIWAN_EXCEPTIONS = {"啊"}


def connect() -> sqlite3.Connection:
    """Open a fresh SQLite connection whose rows behave like dicts.

    The MOE database is attached as "moe" (when present) so a query can also
    match Taiwan readings.
    """
    connection = _connect(DB_PATH)
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


def _reading_counts() -> dict[str, int]:
    """Return {traditional: number of distinct CEDICT readings}, cached.

    Used to detect polyphonic words, whose specific reading must not be replaced
    by the word-level MOE reading.
    """
    global _reading_count_cache
    if _reading_count_cache is None:
        connection = connect()
        try:
            rows = connection.execute(
                "SELECT traditional, COUNT(DISTINCT pinyin) AS n "
                "FROM entries GROUP BY traditional"
            ).fetchall()
        finally:
            connection.close()
        _reading_count_cache = {row["traditional"]: row["n"] for row in rows}
    return _reading_count_cache


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
    connection = _connect(MOE_DB_PATH)
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
            connection = _connect(MOE_DB_PATH)
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


def _moe_toneless_map() -> dict[str, set[str]]:
    """Return {toneless reading: words} from MOE, cached.

    Lets a pinyin search match Taiwan readings (e.g. "lese" -> 垃圾).
    """
    global _moe_toneless_cache
    if _moe_toneless_cache is None:
        mapping: dict[str, set[str]] = defaultdict(set)
        if MOE_DB_PATH.exists():
            connection = _connect(MOE_DB_PATH)
            try:
                rows = connection.execute(
                    "SELECT DISTINCT word, pinyin FROM moe WHERE pinyin <> ''"
                ).fetchall()
            finally:
                connection.close()
            for word, pinyin in rows:
                key = to_toneless(pinyin)
                if key:
                    mapping[key].add(word)
        _moe_toneless_cache = mapping
    return _moe_toneless_cache


def _zhuyin_map() -> dict[str, str]:
    """Return {zhuyin syllable: numeric pinyin}, learned from MOE, cached.

    MOE stores each word's zhuyin and pinyin aligned syllable by syllable, so we
    can learn the mapping (e.g. "ㄍㄢˋ" -> "gan4") without a hand-written table.
    """
    global _zhuyin_cache
    if _zhuyin_cache is None:
        mapping: dict[str, str] = {}
        if MOE_DB_PATH.exists():
            connection = _connect(MOE_DB_PATH)
            try:
                rows = connection.execute(
                    "SELECT DISTINCT zhuyin, pinyin FROM moe "
                    "WHERE zhuyin <> '' AND pinyin <> ''"
                ).fetchall()
            finally:
                connection.close()
            for zhuyin, pinyin in rows:
                zhuyin_syllables = zhuyin.split()
                pinyin_syllables = pinyin.split()
                if len(zhuyin_syllables) != len(pinyin_syllables):
                    continue
                for z, p in zip(zhuyin_syllables, pinyin_syllables):
                    mapping.setdefault(z, to_numeric(p))
        _zhuyin_cache = mapping
    return _zhuyin_cache


def _zhuyin_to_numeric(text: str) -> str | None:
    """Convert a bopomofo query to space-separated numeric pinyin, or None.

    A bopomofo run isn't delimited, so we split it greedily by longest match
    against the known syllables (e.g. "ㄉㄢㄍㄢˋ" -> "dan1 gan4").
    """
    mapping = _zhuyin_map()
    syllables = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace() or text[i] in "'’":
            i += 1
            continue
        match = None
        for end in range(min(i + 4, n), i, -1):
            if text[i:end] in mapping:
                match = text[i:end]
                break
        if match is None:
            return None
        syllables.append(mapping[match])
        i += len(match)
    return " ".join(syllables) if syllables else None


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
    counts = _reading_counts()
    changed = False
    result = []
    for char, syllable in zip(chars, syllables):
        # Don't guess for a polyphonic character: its MOE reading may belong to a
        # different sense (e.g. 腌 āng vs the 腌 ā in 腌臜).
        replacement = None
        if counts.get(char, 1) == 1:
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


def _taiwan_reading(
    word: str, pinyin: str, options: list[str], counts: dict[str, int], char_map: dict
) -> str | None:
    """Return a word's Taiwan reading (marked pinyin), or None.

    Words in `_TAIWAN_EXCEPTIONS` get none. A monophonic word may use its
    whole-word MOE reading; a polyphonic word keeps its specific CEDICT reading
    (the MOE reading is keyed by word, not sense). Everything else falls back to
    per-character derivation.
    """
    if word in _TAIWAN_EXCEPTIONS:
        return None
    if counts.get(word, 1) == 1:
        reading = _word_taiwan(pinyin, options)
        if reading is not None:
            return reading
    return _derive_taiwan(word, pinyin, char_map)


def _attach_sources(entries: list[dict]) -> list[dict]:
    """Set entry["sources"] to the labels of dictionaries that list the headword.

    Membership is read from each dictionary's own storage (see _SOURCE_QUERIES),
    so adding a dictionary there is all it takes to give its headwords a chip.
    Every entry starts with an empty list.
    """
    words = sorted(
        {entry["traditional"] for entry in entries}
        | {entry["simplified"] for entry in entries}
    )
    for entry in entries:
        entry["sources"] = []
    if not words:
        return entries
    placeholders = ",".join("?" * len(words))
    for source in _SOURCE_QUERIES:
        database = source["database"]
        if not database.exists():
            continue
        connection = _connect(database)
        try:
            rows = connection.execute(
                source["sql"].format(ph=placeholders), tuple(words) * source["repeat"]
            ).fetchall()
        finally:
            connection.close()
        found = {row[0] for row in rows}
        for entry in entries:
            if entry["traditional"] in found or entry["simplified"] in found:
                entry["sources"].append(
                    {"label": source["label"], "anchor": source["anchor"]}
                )
    return entries


# Cap on the number of definitions shown per source, per headword (keeps a common
# word from dumping dozens of Wiktionary glosses).
_MAX_DEFS = 12


def _attach_definitions(entries: list[dict]) -> list[dict]:
    """Set entry["definitions"] to each dictionary's senses and classifiers.

    A list, in registry order, of {"anchor", "label", "defs", "classifiers"} for
    every source that has something for the headword. `defs` is the numbered
    senses; `classifiers` is a list of {traditional, simplified, codes, note}.
    """
    words = sorted(
        {entry["traditional"] for entry in entries}
        | {entry["simplified"] for entry in entries}
    )
    for entry in entries:
        entry["definitions"] = []
    if not words:
        return entries
    placeholders = ",".join("?" * len(words))
    params = tuple(words)

    # Fetch every source's senses, classifiers and forms once for the result set.
    senses: dict[str, dict[str, list[str]]] = {}
    classifiers: dict[str, dict[str, list[dict]]] = {}
    form_rows_all: list[tuple] = []
    for source in _SOURCE_QUERIES:
        anchor = source["anchor"]
        senses[anchor] = {}
        classifiers[anchor] = {}
        if not source["database"].exists():
            continue
        connection = _connect(source["database"])
        try:
            for word, _position, text in connection.execute(
                source["sense_sql"].format(ph=placeholders), params
            ):
                if text and text.strip():
                    senses[anchor].setdefault(word, []).append(text.strip())
            for word, trad, simp, codes, note in connection.execute(
                source["cls_sql"].format(ph=placeholders), params
            ):
                classifiers[anchor].setdefault(word, []).append(
                    {
                        "traditional": trad,
                        "simplified": simp,
                        "codes": codes or "",
                        "note": note or "",
                    }
                )
            if source.get("forms_sql"):
                form_rows_all.extend(
                    connection.execute(
                        source["forms_sql"].format(ph=placeholders), params * 2
                    ).fetchall()
                )
        finally:
            connection.close()

    def for_entry(mapping: dict, entry: dict) -> list:
        """The value for an entry, looked up by traditional then simplified."""
        return mapping.get(entry["traditional"]) or mapping.get(entry["simplified"]) or []

    for entry in entries:
        defs = []
        for source in _SOURCE_QUERIES:
            anchor = source["anchor"]
            values = for_entry(senses[anchor], entry)
            clss = for_entry(classifiers[anchor], entry)
            if values or clss:
                defs.append(
                    {
                        "anchor": anchor,
                        "label": source["label"],
                        "lang": source["lang"],
                        "defs": values[:_MAX_DEFS],
                        "classifiers": clss[:_MAX_DEFS],
                    }
                )
        entry["definitions"] = defs
        # Variant forms: this entry's main form (if it is a "see X" variant), and
        # the variants that point at this entry (shown as "Other forms").
        words = {entry["traditional"], entry["simplified"]}
        entry["main"] = next((row[2] for row in form_rows_all if row[0] in words), None)
        entry["forms"] = [
            {"traditional": row[1], "simplified": row[0], "pinyin": row[3] or ""}
            for row in form_rows_all
            if row[2] in words
        ]
    return entries


def _decorate(entries: list[dict]) -> list[dict]:
    """Fill in Taiwan readings, source chips and per-dictionary definitions.

    A whole-word MOE reading is only used when the word has a single CEDICT
    reading — for polyphonic words the MOE reading is keyed by the word, not the
    sense, so it would clobber the entry's specific reading (e.g. 啊's five
    readings collapsing to one, or 呵[ā] becoming hē). Everything else falls back
    to per-character readings, which only swap a character when unambiguous.
    Words in `_TAIWAN_EXCEPTIONS` are skipped entirely.
    """
    char_map = _moe_char_map()
    counts = _reading_counts()

    def taiwan_for(word: str, pinyin: str, options: list[str]) -> str | None:
        return _taiwan_reading(word, pinyin, options, counts, char_map)

    # The entry's own word.
    by_word = _moe_readings([entry["traditional"] for entry in entries])
    for entry in entries:
        entry["pinyin_taiwan"] = taiwan_for(
            entry["traditional"], entry["pinyin"], by_word.get(entry["traditional"], [])
        )

    # Words referenced inside the definitions (they render as furigana too).
    referenced = {}
    for entry in entries:
        for traditional, simplified, pinyin in _cross_refs(entry.get("english_normalized")):
            referenced.setdefault(simplified, (traditional, pinyin))
    ref_readings = _moe_readings([traditional for traditional, _ in referenced.values()])
    for entry in entries:
        taiwan_refs = {}
        for traditional, simplified, pinyin in _cross_refs(entry.get("english_normalized")):
            reading = taiwan_for(traditional, pinyin, ref_readings.get(traditional, []))
            if reading:
                taiwan_refs[simplified] = reading
        entry["taiwan_refs"] = taiwan_refs

    # Add the source chips and per-dictionary definitions last, once every entry
    # dict is fully built.
    _attach_sources(entries)
    _attach_definitions(entries)
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
    return _decorate([dict(row) for row in rows])


def has_cjk(text: str) -> bool:
    """True when `text` contains a CJK ideograph.

    Used to tell a real Chinese word apart from a latin headword (CEDICT has a
    few latin headwords, e.g. "word" whose "pinyin" is just the letters).
    """
    return any("\u3400" <= char <= "\u9fff" for char in text)


def _parse_pinyin_query(query: str) -> tuple[str, dict[int, str], set[int]] | None:
    """Parse a tonal pinyin query into (letters, {offset: tone}, bound offsets).

    Returns None unless the query is pinyin with at least one tone digit (so
    plain "dangan" is handled elsewhere). A tone digit sets a required tone *and*
    marks a syllable break; an apostrophe or space marks a break; a syllable with
    no digit is left free to match any tone.
    """
    letters: list[str] = []
    tones: dict[int, str] = {}
    boundaries: set[int] = set()
    for char in query.lower():
        if char in "12345":
            tones[len(letters)] = char
            boundaries.add(len(letters))
        elif char in "'’ \t":
            boundaries.add(len(letters))
        elif char == "*":
            continue  # a wildcard doesn't constrain the tonal matcher
        elif "a" <= char <= "z" or char == "ü":
            letters.append(char)
        else:
            return None
    if not tones or not letters:
        return None
    return "".join(letters), tones, boundaries


def _reading_rank(
    pinyin_numeric: str, letters: str, tones: dict[int, str], boundaries: set[int]
) -> int | None:
    """Rank how a reading contains the query, or None if it does not match.

    0 = the whole reading, 1 = the reading starts with it, 2 = it merely contains
    it. Emptiness-free: every specified tone and forced break must land on a
    syllable boundary of the entry's own reading (so "dan'gan" won't match 档案).
    """
    parts = [
        (syllable[:-1], syllable[-1]) if syllable[-1:].isdigit() else (syllable, "")
        for syllable in pinyin_numeric.split()
    ]
    constraints = set(tones) | boundaries
    for start in range(len(parts)):
        pos = 0  # letters matched so far
        idx = start
        ok = True
        while pos < len(letters) and idx < len(parts):
            word, tone = parts[idx]
            # a required break strictly inside this syllable is unsatisfiable
            if any(pos < offset < pos + len(word) for offset in constraints):
                ok = False
                break
            pos += len(word)
            if pos in tones and tones[pos] != tone:
                ok = False
                break
            idx += 1
        if not ok or pos != len(letters):
            continue
        # the consumed syllables must spell exactly the query's letters
        if "".join(part[0] for part in parts[start:idx]) != letters:
            continue
        # a required break/tone past the end can never line up
        if any(offset > len(letters) for offset in constraints):
            continue
        if start == 0:
            return 0 if idx == len(parts) else 1
        return 2
    return None


def tonal_variant(query: str) -> str | None:
    """Return the toneless pinyin letters of a tonal/bopomofo query, else None.

    Used to offer a tone-insensitive "fuzzy" search when a tonal query finds
    nothing (e.g. "shi2shi" -> "shishi").
    """
    text = query.strip()
    if any(0x3100 <= ord(char) <= 0x312F for char in text):
        text = _zhuyin_to_numeric(text) or ""
    parsed = _parse_pinyin_query(text)
    return parsed[0] if parsed else None


def search(query: str, limit: int = 50, taiwan: bool = False) -> list[dict]:
    """Search the dictionary.

    A "quoted" query is an English phrase search (whole words, via the FTS
    index). Otherwise the query matches the Chinese word (simplified column; the
    query is converted to simplified first), the English definitions, and the
    toneless pinyin reading (e.g. "dangan" finds 单干 and 档案). With `taiwan`
    set, the pinyin reading also matches Taiwan (MOE) readings. '*' matches zero
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
        return _decorate([dict(row) for row in rows])

    # Bopomofo / zhuyin (e.g. "ㄉㄢㄍㄢˋ"): convert to numeric pinyin first so
    # the tone-number branch below handles it.
    if any(0x3100 <= ord(char) <= 0x312F for char in query):
        converted = _zhuyin_to_numeric(query)
        if converted:
            query = converted

    # Pinyin with tone numbers (e.g. "shi4", "shi2shi", "ban4gongshi4"): match the
    # reading, honouring a tone where one is given and leaving a tone-less
    # syllable free to match any tone. Whole readings rank first, then readings
    # that begin with the query, then readings that merely contain it.
    parsed = _parse_pinyin_query(query)
    if parsed:
        letters, tones, boundaries = parsed
        connection = connect()
        try:
            rows = connection.execute(
                "SELECT id, simplified, traditional, pinyin, pinyin_numeric, "
                "english, english_normalized FROM entries "
                "WHERE pinyin_toneless LIKE ? ESCAPE '\\'",
                (_like_pattern(f"*{letters}*"),),
            ).fetchall()
        finally:
            connection.close()
        ranked = []
        # With the Taiwan toggle on, match each word's Taiwan reading where one
        # exists, so "xi1" no longer matches 息怒 (Taiwan xí nù = xi2 nu4).
        words_by = _moe_readings([row["traditional"] for row in rows]) if taiwan else {}
        counts = _reading_counts()
        char_map = _moe_char_map()
        for row in rows:
            numeric = row["pinyin_numeric"]
            if taiwan:
                taiwan_reading = _taiwan_reading(
                    row["traditional"],
                    row["pinyin"],
                    words_by.get(row["traditional"], []),
                    counts,
                    char_map,
                )
                if taiwan_reading is not None:
                    numeric = to_numeric(taiwan_reading)
            rank = _reading_rank(numeric, letters, tones, boundaries)
            if rank is not None:
                ranked.append((rank, len(row["simplified"]), row["id"], dict(row)))
        ranked.sort(key=lambda item: item[:3])
        return _decorate([item[3] for item in ranked[:limit]])

    # Exact pinyin reading (e.g. "yabai" -> 牙白): if the query is a complete
    # Chinese pronunciation, show only those matches and skip English results.
    # A '*' makes it a toneless pattern ("*xinu*" -> 喜怒哀樂); an apostrophe (or
    # space) pins syllable boundaries ("dan'gan" -> 单干, not 档案).
    wildcard = "*" in query
    segments = [to_toneless(part) for part in re.split(r"['’\s]+", query) if part]
    pinyin_key = to_toneless(query)
    if pinyin_key:
        # A bare query matches the whole reading or a word that starts with it,
        # with exact matches first ("xinu" and "xinu*" behave the same). A '*'
        # makes it a pattern ("*xinu*" also matches words that merely contain it).
        pattern = (
            _like_pattern("*".join(to_toneless(part) for part in query.split("*")))
            if wildcard
            else _like_pattern(f"{pinyin_key}*")
        )
        connection = connect()
        try:
            rows = connection.execute(
                "SELECT simplified, traditional, pinyin, english, english_normalized "
                "FROM entries WHERE pinyin_toneless LIKE ? ESCAPE '\\' "
                "ORDER BY (pinyin_toneless = ?) DESC, length(simplified), id LIMIT ?",
                (pattern, pinyin_key, limit),
            ).fetchall()
            # With the Taiwan toggle on, also match MOE (Taiwan) readings, so a
            # query like "lese" finds 垃圾.
            if taiwan:
                words = sorted(_moe_toneless_map().get(pinyin_key, ()))
                if words:
                    holes = ",".join("?" * len(words))
                    rows += connection.execute(
                        "SELECT simplified, traditional, pinyin, english, english_normalized "
                        f"FROM entries WHERE traditional IN ({holes}) "
                        "ORDER BY length(simplified), id LIMIT ?",
                        (*words, limit),
                    ).fetchall()
        finally:
            connection.close()
        # Only real (hanzi) words count; skip latin headwords like the slang
        # entry "word", so such a query falls through to English. De-duplicate,
        # since an entry can match both CEDICT and MOE.
        seen = set()
        entries = []
        for row in rows:
            if not has_cjk(row["simplified"]):
                continue
            # A pinned segmentation must match the entry's own syllable split.
            if len(segments) > 1 and not wildcard:
                syllables = [to_toneless(part) for part in row["pinyin"].split()]
                if syllables != segments:
                    continue
            key = (row["traditional"], row["pinyin"])
            if key not in seen:
                seen.add(key)
                entries.append(dict(row))
        if entries:
            return _decorate(entries[:limit])

    # Chinese: convert to simplified, and treat a plain query as a prefix search.
    chinese = _convert(query, _simplify_map())
    if "*" not in chinese:
        chinese += "*"
    pattern_chinese = _like_pattern(chinese)
    literal = chinese.replace("*", "")  # the "exact" word, ranked first

    # Only a query containing hanzi can match the Chinese word column; a latin
    # query is pinyin or English and must not match latin headwords like "word".
    is_chinese = has_cjk(literal)

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

    return _decorate([dict(row) for row in rows])
