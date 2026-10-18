"""SQLite access for the dictionary."""

from __future__ import annotations

import os
import re
import threading
import sqlite3
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from opencc import OpenCC

from yabai_chaci.overrides import (
    HIDE_FORMS,
    IDS_IMAGES,
    PREFERRED_READINGS,
    TAIWAN_READINGS,
)
from yabai_chaci.pinyin import numeric_to_marked, split_pinyin, to_numeric, to_toneless

# --- Simplified <-> Traditional conversion (OpenCC) -------------------------
# OpenCC converts with word/phrase dictionaries, so it handles the one-to-many
# cases a per-character map cannot get right (头发 -> 頭髮, 干净 -> 乾淨, 皇后
# stays 皇后). Building a converter is cheap but not free, so cache one per
# config name.
_OPENCC: dict[str, OpenCC] = {}


def _opencc(config: str) -> OpenCC:
    """Return a cached OpenCC converter for a config name (e.g. 's2t')."""
    if config not in _OPENCC:
        _OPENCC[config] = OpenCC(config)
    return _OPENCC[config]


@lru_cache(maxsize=200_000)
def to_traditional(text: str) -> str:
    """Convert Chinese text to traditional characters (simplified -> traditional)."""
    return _opencc("s2t").convert(text) if text else text


@lru_cache(maxsize=200_000)
def to_simplified(text: str) -> str:
    """Convert Chinese text to simplified characters (traditional -> simplified)."""
    return _opencc("t2s").convert(text) if text else text

# Path to the generated CEDICT database: <project root>/data/cedict.sqlite.
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cedict.sqlite"

# Path to the MOE dictionary (Taiwan readings), built by scripts/import_moe.py.
MOE_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "moe.sqlite"

# Path to the Wiktionary headword list (kaikki.org/Wiktextract), built by
# scripts/import_wiktionary.py.
WIKTIONARY_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "wiktionary.sqlite"

# Path to the SUBTLEX-CH word frequencies (Cai & Brysbaert, 2010), built by
# scripts/import_subtlex.py.
SUBTLEX_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "subtlex.sqlite"

# Path to the definition word list (scripts/import_english_words.py): the whole
# words that appear in any dictionary's definitions, used for the English-search
# hint.
ENGLISH_WORDS_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "english_words.sqlite"

# Path to the definition gram index (scripts/import_definition_grams.py): Han
# unigrams/bigrams used to skip definition scans that cannot match.
DEFINITION_GRAMS_DB_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "definition_grams.sqlite"
)

# Path to the Han unigram/bigram inverted index (scripts/import_gram_words.py):
# turns 1-2 character CJK queries into one lookup instead of a table scan.
GRAM_WORDS_DB_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "gram_words.sqlite"
)

# --- debug log -------------------------------------------------------------
# Every search records the SQL statements it runs and how long each took, so
# data/search.log (the last three searches) explains slow queries. On by
# default; set YABAI_SQL_LOG=0 to turn the instrumentation off.
SQL_LOG = os.environ.get("YABAI_SQL_LOG", "1") != "0"

# Per-thread, so concurrent requests cannot mix their statements.
_log_state = threading.local()


def start_log() -> None:
    """Begin capturing this thread's SQL statements with timings."""
    _log_state.statements = [] if SQL_LOG else None


def end_log() -> list[str]:
    """Stop capturing and return the captured, timed statements."""
    statements = getattr(_log_state, "statements", None) or []
    _log_state.statements = None
    return statements


def _log_sql(sql: str, started: float, rows: int | None) -> None:
    """Append one timed statement to this thread's log (if logging is on)."""
    statements = getattr(_log_state, "statements", None)
    if statements is None:
        return
    duration = (time.perf_counter() - started) * 1000
    count = "?" if rows is None else str(rows)
    statements.append(f"{duration:8.2f} ms rows={count} | {' '.join(sql.split())}")


class _TimedCursor(sqlite3.Cursor):
    """A cursor that records each statement's duration and row count."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sql = ""
        self._started = 0.0
        self._logged = True

    def execute(self, sql, parameters=()):
        self._sql = sql
        self._started = time.perf_counter()
        self._logged = False
        return super().execute(sql, parameters)

    def _emit(self, rows):
        if not self._logged:
            self._logged = True
            _log_sql(self._sql, self._started, rows)

    def fetchall(self):
        rows = super().fetchall()
        self._emit(len(rows))
        return rows

    def fetchone(self):
        row = super().fetchone()
        self._emit(0 if row is None else 1)
        return row

    def __iter__(self):
        count = 0
        try:
            while True:
                row = super().fetchone()
                if row is None:
                    break
                count += 1
                yield row
        finally:
            self._emit(count)


# One pooled connection per database path, kept for the process lifetime:
# read-only, with a page cache, so a search does not reopen files (or re-ATTACH
# MOE/SUBTLEX) on every call. close() is a no-op, so the existing
# `finally: connection.close()` calls stay harmless. A restart is already
# required after re-imports, so holding the old file handle is fine.
class _PooledConnection(sqlite3.Connection):
    def close(self) -> None:
        pass

    if SQL_LOG:
        # Route every statement through the timed cursor so the per-query log
        # records durations. With logging off the base methods are used.
        def cursor(self, factory=_TimedCursor):
            return super().cursor(factory=factory)

        def execute(self, sql, parameters=()):
            return self.cursor().execute(sql, parameters)

        def executemany(self, sql, seq_of_parameters):
            cursor = self.cursor()
            cursor._sql = sql
            cursor._started = time.perf_counter()
            cursor._logged = False
            cursor.executemany(sql, seq_of_parameters)
            cursor._emit(None)
            return cursor


_connection_pool: dict[str, sqlite3.Connection] = {}
_connection_lock = threading.Lock()


def _connect(database) -> sqlite3.Connection:
    """Return the pooled, read-only connection for a database file."""
    key = str(Path(database).resolve())
    connection = _connection_pool.get(key)
    if connection is not None:
        return connection
    with _connection_lock:
        connection = _connection_pool.get(key)
        if connection is None:
            connection = sqlite3.connect(
                f"file:{key}?mode=ro",
                uri=True,
                check_same_thread=False,
                factory=_PooledConnection,
            )
            connection.execute("PRAGMA mmap_size=268435456")
            connection.execute("PRAGMA cache_size=-16384")
            connection.execute("PRAGMA temp_store=MEMORY")
            _connection_pool[key] = connection
    return connection

# Which dictionaries contain a headword - drives the source chips, the
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
# CEDICT also stores the reading each sense belongs to, so its definitions can be
# grouped per reading (for polyphonic words).
_CEDICT_SENSES_SQL = "SELECT word, position, text, reading FROM senses WHERE word IN ({ph})"
_CLASSIFIERS_SQL = (
    "SELECT word, traditional, simplified, codes, note FROM classifiers "
    "WHERE word IN ({ph})"
)
# Variant forms ("see X" entries) - only some sources have a `forms` table. Each
# row maps a variant to its main form.
_FORMS_SQL = (
    "SELECT variant_trad, variant_simp, main_trad, main_simp, pinyin FROM forms "
    "WHERE variant_trad IN ({ph}) OR main_trad IN ({ph})"
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
        "sense_sql": _CEDICT_SENSES_SQL,
        "cls_sql": _CLASSIFIERS_SQL,
        "forms_sql": _FORMS_SQL,
        "pinyin_sql": "SELECT pinyin FROM entries "
                      "WHERE ? IN (traditional, simplified) LIMIT 1",
        "count_sql": "SELECT COUNT(*) FROM entries",
    },
    {
        "label": "國語辭典簡編本",
        "anchor": "moe",
        "lang": "taiwan",
        "database": MOE_DB_PATH,
        "sql": "SELECT DISTINCT word FROM moe WHERE word IN ({ph})",
        "like_sql": "SELECT DISTINCT word FROM moe "
                    "WHERE word LIKE ? ESCAPE '\\' ORDER BY word",
        "all_sql": "SELECT DISTINCT word FROM moe WHERE word <> ''",
        "repeat": 1,
        "sense_sql": _SENSES_SQL,
        "cls_sql": _CLASSIFIERS_SQL,
        "pinyin_sql": "SELECT pinyin FROM moe WHERE word = ? AND pinyin <> '' LIMIT 1",
        "pinyin_batch_sql": "SELECT word, pinyin FROM moe "
                            "WHERE word IN ({ph}) AND pinyin <> ''",
        "count_sql": "SELECT COUNT(*) FROM moe",
    },
    {
        "label": "Wiktionary",
        "anchor": "wiktionary",
        "lang": "english",
        "database": WIKTIONARY_DB_PATH,
        "sql": "SELECT DISTINCT word FROM wiktionary "
               "WHERE word IN ({ph}) AND gloss IS NOT NULL",
        # Membership without the gloss filter: a headword with no definition can
        # still get its source chip when it is the only result. A reading-only
        # row (no headword/gloss) still means Wiktionary lists the word.
        "heads_sql": "SELECT word FROM ("
                     "SELECT word FROM wiktionary UNION SELECT word FROM readings"
                     ") WHERE word IN ({ph})",
        "like_sql": "SELECT DISTINCT word FROM wiktionary "
                    "WHERE word LIKE ? ESCAPE '\\' AND gloss IS NOT NULL "
                    "ORDER BY word",
        # The same headword search through the trigram index, for queries of
        # three or more characters (the index covers the word column too).
        "like_sql_fts": "SELECT DISTINCT word FROM wiktionary_fts "
                        "WHERE word LIKE ? AND gloss IS NOT NULL "
                        "ORDER BY word",
        "all_sql": "SELECT DISTINCT word FROM wiktionary WHERE word <> ''",
        "repeat": 1,
        "pinyin_sql": "SELECT pinyin FROM readings WHERE word = ? "
                      "AND kind = 'mandarin' AND pinyin <> '' ORDER BY id LIMIT 1",
        "pinyin_batch_sql": "SELECT word, pinyin FROM readings WHERE word IN ({ph}) "
                            "AND kind = 'mandarin' AND pinyin <> '' ORDER BY id",
        "pinyin_fallback_sql": "SELECT pinyin, label FROM readings "
                               "WHERE word = ? AND kind <> 'mandarin' "
                               "AND pinyin <> '' "
                               "ORDER BY CASE kind "
                               "  WHEN 'wu' THEN 0 WHEN 'xiang' THEN 1 "
                               "  WHEN 'sichuanese' THEN 2 WHEN 'gan' THEN 3 "
                               "  ELSE 4 END, id "
                               "LIMIT 1",
        "pinyin_fallback_batch_sql": "SELECT word, pinyin, label FROM readings "
                                     "WHERE word IN ({ph}) "
                                     "AND kind <> 'mandarin' AND pinyin <> '' "
                                     "ORDER BY CASE kind "
                                     "  WHEN 'wu' THEN 0 WHEN 'xiang' THEN 1 "
                                     "  WHEN 'sichuanese' THEN 2 WHEN 'gan' THEN 3 "
                                     "  ELSE 4 END, id",
        "split_pinyin": True,
        "sense_sql": _WIKTIONARY_SENSES_SQL,
        "cls_sql": _CLASSIFIERS_SQL,
        "count_sql": "SELECT COUNT(DISTINCT word) FROM wiktionary",
    },
]

# sources() is served on / and /about; its COUNT(DISTINCT ...) queries are the
# only cost there. Cache the result, invalidated when any source file changes.
_source_cache: tuple[tuple, list[dict]] | None = None


def sources() -> list[dict]:
    """Each source's label, About-page anchor and last-updated date.

    The label/anchor drive the source chips and definition headers; `updated` is
    read from the source database's `meta` table (written by its importer) so the
    About page can show when the data was last refreshed. `updated` is None when
    the database isn't present yet.
    """
    global _source_cache
    stamp = tuple(
        (
            str(source["database"]),
            source["database"].stat().st_mtime if source["database"].exists() else None,
        )
        for source in _SOURCE_QUERIES
    )
    if _source_cache is not None and _source_cache[0] == stamp:
        return _source_cache[1]
    info = []
    for source in _SOURCE_QUERIES:
        database = source["database"]
        updated = None
        entries = None
        if database.exists():
            try:
                connection = _connect(database)
            except sqlite3.Error:
                connection = None
            if connection is not None:
                try:
                    row = connection.execute(
                        "SELECT value FROM meta WHERE key = 'updated'"
                    ).fetchone()
                    updated = row[0] if row else None
                    # Live count, so it reflects the data after any re-import.
                    if source.get("count_sql"):
                        entries = connection.execute(source["count_sql"]).fetchone()[0]
                except sqlite3.Error:
                    pass
                finally:
                    connection.close()
        info.append(
            {
                "label": source["label"],
                "anchor": source["anchor"],
                "updated": updated,
                "entries": entries,
            }
        )
    _source_cache = (stamp, info)
    return info


def frequency_source() -> dict | None:
    """Info about the SUBTLEX-CH frequency list (or None if it isn't imported)."""
    if not SUBTLEX_DB_PATH.exists():
        return None
    updated = None
    entries = None
    try:
        connection = _connect(SUBTLEX_DB_PATH)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'updated'").fetchone()
        updated = row[0] if row else None
        entries = connection.execute("SELECT COUNT(*) FROM freq").fetchone()[0]
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    return {"label": "SUBTLEX-CH", "anchor": "subtlex", "updated": updated, "entries": entries}


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


@lru_cache(maxsize=100_000)
def _script_forms(word: str) -> tuple[str, ...]:
    """`word` plus its traditional and simplified variants (via OpenCC).

    Lets a traditional query (e.g. 馬) find a simplified entry (马) and vice
    versa.
    """
    return tuple(sorted({word, to_simplified(word), to_traditional(word)}))


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
                "traditional": to_traditional(head),
                "simplified": to_simplified(head),
                "pinyin": pinyin,
                "senses": senses[:_MAX_DEFS],
                "classifiers": classifiers[:_MAX_DEFS],
            }
        )
    return matches

# Cache of MOE single-character readings: {character: [pinyin, ...]}.
_moe_char_cache: dict[str, list[str]] | None = None

# Cache of {toneless reading: {word, ...}} from MOE, for Taiwan pinyin search.
_moe_toneless_cache: dict[str, set[str]] | None = None

# Cache of {zhuyin syllable: numeric pinyin}, learned from MOE. None until built.
_zhuyin_cache: dict[str, str] | None = None

# Cache of {word: number of distinct CEDICT readings}. None until built.
_reading_count_cache: dict[str, int] | None = None

# Words that never take a Taiwan reading - the word-level MOE reading does not
# fit their CEDICT sense. Add more here as needed.
_TAIWAN_EXCEPTIONS = {"啊"}


def connect() -> sqlite3.Connection:
    """The pooled CEDICT connection, with MOE/SUBTLEX attached once.

    Rows behave like dicts; the MOE database is attached as "moe" (when
    present) so a query can also match Taiwan readings.
    """
    connection = _connect(DB_PATH)
    # Row factory lets us read columns by name, e.g. row["pinyin"].
    connection.row_factory = sqlite3.Row
    with _connection_lock:
        attached = {row[1] for row in connection.execute("PRAGMA database_list")}
        if "moe" not in attached and MOE_DB_PATH.exists():
            connection.execute("ATTACH DATABASE ? AS moe", (str(MOE_DB_PATH),))
        if "subtlex" not in attached and SUBTLEX_DB_PATH.exists():
            connection.execute("ATTACH DATABASE ? AS subtlex", (str(SUBTLEX_DB_PATH),))
    return connection


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
                (source.get("heads_sql") or source["sql"]).format(ph=placeholders),
                tuple(words) * source["repeat"],
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


def _attach_freq(entries: list[dict]) -> list[dict]:
    """Attach each entry's SUBTLEX-CH frequency (rank / count / per-million).

    Matched by the simplified form, then the traditional form; None when the word
    isn't in SUBTLEX-CH.
    """
    words = sorted(
        {entry["simplified"] for entry in entries}
        | {entry["traditional"] for entry in entries}
    )
    for entry in entries:
        entry["freq"] = None
    if not words or not SUBTLEX_DB_PATH.exists():
        return entries
    placeholders = ",".join("?" * len(words))
    connection = _connect(SUBTLEX_DB_PATH)
    try:
        rows = connection.execute(
            f"SELECT word, rank, count, per_million FROM freq WHERE word IN ({placeholders})",
            tuple(words),
        ).fetchall()
    finally:
        connection.close()
    by_word = {
        word: {"rank": rank, "count": count, "per_million": per_million}
        for word, rank, count, per_million in rows
    }
    for entry in entries:
        entry["freq"] = by_word.get(entry["simplified"]) or by_word.get(entry["traditional"])
    return entries


# Cap on the number of definitions shown per source, per headword (keeps a common
# word from dumping dozens of Wiktionary glosses).
_MAX_DEFS = 12


# Inline pronunciation notes, e.g. "(Taiwan pr. [gong1])" or "; also pr. [x]".
# The UI shows these as separate detail rows, so drop them from the sense text.
_PRON_NOTE_RE = re.compile(
    r"\s*\((?:[^()]*?\s+)?pr\.\s*\[[^\]]+\]\)"
    r"|[;\s]+(?:[^;()]*?\s+)?pr\.\s*\[[^\]]+\]"
)


def _strip_pron_notes(text: str) -> str:
    """Remove inline pronunciation notes that the UI shows as separate rows."""
    cleaned = _PRON_NOTE_RE.sub("", text)
    return re.sub(r"\(\s*\)", "", cleaned).strip(" ;")


def _classifier_readings(forms: set[str]) -> dict[str, str]:
    """Best CEDICT reading for each classifier form (most senses wins).

    Classifier rows carry no reading of their own, so the furigana above a
    classifier character comes from that word's main CEDICT entry.
    """
    if not forms:
        return {}
    values = sorted(forms)
    placeholders = ",".join("?" * len(values))
    query = (
        "SELECT simplified, traditional, pinyin, english FROM entries "
        f"WHERE simplified IN ({placeholders}) OR traditional IN ({placeholders}) "
        # Prefer the reading whose own definition is about classifiers (e.g. 只
        # zhī, not zhǐ), then the reading with the most senses.
        "ORDER BY (instr(lower(english), 'classifier') > 0) DESC, "
        "(SELECT COUNT(*) FROM senses s "
        " WHERE s.word = entries.simplified AND s.reading = entries.pinyin) DESC, "
        "entries.id"
    )
    connection = connect()
    try:
        rows = connection.execute(query, tuple(values) * 2).fetchall()
    finally:
        connection.close()
    readings: dict[str, str] = {}
    for simplified, traditional, pinyin, _english in rows:
        if not pinyin:
            continue
        for form in (simplified, traditional):
            if form and form not in readings:
                readings[form] = pinyin
    return readings


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
    # Classifier characters, collected so they can all get a furigana reading.
    classifier_forms: set[str] = set()
    for source in _SOURCE_QUERIES:
        anchor = source["anchor"]
        senses[anchor] = {}
        classifiers[anchor] = {}
        if not source["database"].exists():
            continue
        connection = _connect(source["database"])
        try:
            for row in connection.execute(source["sense_sql"].format(ph=placeholders), params):
                word, text = row[0], row[2]
                reading = row[3] if len(row) > 3 else ""  # CEDICT only
                if text and text.strip():
                    value = text.strip()
                    if anchor == "cedict":
                        value = _strip_pron_notes(value)
                        # A classifier inside a sense is written "(CL:棵[ke1])";
                        # spell out the label for the numbered definition.
                        value = value.replace("CL:", "Classifier:")
                    senses[anchor].setdefault(word, []).append((reading or "", value))
            for word, trad, simp, codes, note in connection.execute(
                source["cls_sql"].format(ph=placeholders), params
            ):
                if trad:
                    classifier_forms.add(trad)
                if simp:
                    classifier_forms.add(simp)
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

    # Readings for the classifier characters (furigana above each classifier).
    classifier_readings = _classifier_readings(classifier_forms)
    for per_word in classifiers.values():
        for clss in per_word.values():
            for cls in clss:
                cls["pinyin_traditional"] = classifier_readings.get(
                    cls["traditional"] or "", ""
                )
                cls["pinyin_simplified"] = classifier_readings.get(
                    cls["simplified"] or "", ""
                )

    for entry in entries:
        defs = []
        for source in _SOURCE_QUERIES:
            anchor = source["anchor"]
            values = for_entry(senses[anchor], entry)  # list of (reading, text)
            clss = for_entry(classifiers[anchor], entry)
            if values or clss:
                # Group the senses by reading (CEDICT); other sources carry none.
                groups: list[dict] = []
                index: dict[str, list[str]] = {}
                for reading, text in values:
                    key = reading or ""
                    if key not in index:
                        index[key] = []
                        groups.append({"pinyin": key, "senses": index[key]})
                    index[key].append(text)
                for group in groups:
                    group["senses"] = group["senses"][:_MAX_DEFS]
                # Show the pronunciation with the most senses first (e.g. 術
                # shù before zhú). Python's sort is stable, so equal counts keep
                # the dictionary's own order.
                groups.sort(key=lambda group: len(group["senses"]), reverse=True)
                defs.append(
                    {
                        "anchor": anchor,
                        "label": source["label"],
                        "lang": source["lang"],
                        "groups": groups,
                        "defs": [text for _reading, text in values[:_MAX_DEFS]],
                        "classifiers": clss[:_MAX_DEFS],
                    }
                )
        entry["definitions"] = defs
        # Variant forms. `main` is the redirect target when this entry is a
        # "see X" / "variant of X" entry pointing at a *different* form (self-references
        # like 血[xiě] -> 血[xuè] are ignored here). `forms` lists the variants that
        # point at this entry, shown as "Other forms". Matching is by the
        # traditional form, which stays distinct even when simplified collide
        # (軟 vs 輭, both simplified 软).
        trad = entry["traditional"]
        entry["main"] = next(
            (row[2] for row in form_rows_all if row[0] == trad and row[2] != trad),
            None,
        )
        if trad in HIDE_FORMS or entry["simplified"] in HIDE_FORMS:
            entry["forms"] = []
        else:
            entry["forms"] = [
                {"traditional": row[0], "simplified": row[1] or row[0], "pinyin": row[4] or ""}
                for row in form_rows_all
                if row[2] == trad
            ]
    return entries


# Pinyin tone-marked vowels, used to recognise a reading in a definition. A
# parenthetical English note ("colloquial") has none, so it is ignored.
_PINYIN_TONE_MARKS = "āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜĀÁǍÀĒÉĚÈĪÍǏÌŌÓǑÒŪÚǓÙǕǗǙǛ"

# A reading written after a Chinese word, e.g. "alternative form of 零次元
# (língcìyuán)". Group 1 is the text inside the parentheses.
_GLOSS_READING_RE = re.compile(
    r"[\u3400-\u9fff]\s*\(([^()]*[" + _PINYIN_TONE_MARKS + r"][^()]*)\)"
)

# What a pinyin reading may contain (letters, tone marks, spaces, apostrophes).
_PINYIN_ONLY_RE = re.compile(
    r"^[A-Za-züÜ" + _PINYIN_TONE_MARKS + r"\s'\-–]+$"
)

# Toneless pinyin syllables seen in CEDICT, built once and reused so a run-together
# reading can be split without a hard-coded syllable table.
_cedict_syllable_cache: set[str] | None = None


def _cedict_syllables() -> set[str]:
    """Every toneless pinyin syllable CEDICT uses, cached."""
    global _cedict_syllable_cache
    if _cedict_syllable_cache is None:
        connection = connect()
        try:
            rows = connection.execute("SELECT pinyin FROM entries").fetchall()
        finally:
            connection.close()
        _cedict_syllable_cache = {
            to_toneless(token)
            for (pinyin,) in rows
            for token in (pinyin or "").split()
        } - {""}
    return _cedict_syllable_cache


@lru_cache(maxsize=50_000)
def _split_pinyin(text: str) -> str:
    """Space out a run-together reading, e.g. 'língcìyuán' -> 'líng cì yuán'.

    Uses the syllables CEDICT actually contains, so the importer can precompute
    exactly the same spacing (see scripts/import_wiktionary.py).
    """
    return split_pinyin(text, _cedict_syllables())


@lru_cache(maxsize=50_000)
def _gloss_reading(text: str) -> str:
    """Extract a spaced pinyin reading from a gloss, or "" when there is none.

    Handles "alternative form of 零次元 (língcìyuán)" and a translation after the
    reading, as in "… 三國演義 (Sānguó Yǎnyì, “The Romance …”)". Only tone-marked
    parentheticals qualify, so English notes are not mistaken for readings.
    """
    match = _GLOSS_READING_RE.search(text or "")
    if not match:
        return ""
    # Keep the reading before a comma/semicolon: the rest is a translation.
    # Surrounding quotation marks are not part of the reading (“kōu”).
    reading = re.split(r"[,，;；]", match.group(1), maxsplit=1)[0].strip()
    reading = reading.strip("“”‘’\"")
    if not _PINYIN_ONLY_RE.match(reading):
        return ""
    return _split_pinyin(reading)


# A referenced word plus a parenthetical reading, e.g. "十月 (shíyuè)". A word
# may be written as "traditional／simplified" (e.g. 三國演義／三国演义).
_GLOSS_WORD_RE = re.compile(
    r"([0-9\u3400-\u9fff]+)(?:\s*[／/]\s*([0-9\u3400-\u9fff]+))?\s*\(([^()]*)\)"
)


@lru_cache(maxsize=50_000)
def _annotate_gloss(text: str) -> str:
    """Rewrite "word (reading)" as "word[reading]" for the frontend.

    Wiktionary definitions write a referenced word's reading in parentheses,
    e.g. "alternative form of 十月 (shíyuè)". The bracket form is what CC-CEDICT
    cross-references already use, so the UI can show furigana for it. A
    "traditional／simplified" pair becomes CEDICT's "traditional|simplified".
    """
    def replace(match: re.Match) -> str:
        first, second, content = match.group(1), match.group(2), match.group(3)
        parts = re.split(r"[,，;；]", content, maxsplit=1)
        reading = parts[0].strip().strip("“”‘’\"")
        # Only a tone-marked pinyin reading qualifies, not an English note.
        if not re.search(f"[{_PINYIN_TONE_MARKS}]", reading):
            return match.group(0)
        if not _PINYIN_ONLY_RE.match(reading):
            return match.group(0)
        numeric = to_numeric(_split_pinyin(reading))
        if second:
            # The glosses write "trad／simp"; swap only if that is reversed.
            trad, simp = (second, first) if to_simplified(second) == first else (first, second)
            word = f"{trad}|{simp}"
        else:
            word = first
        # Keep a translation that followed the reading, e.g. "(shíyuè, “October”)".
        extra = f" ({parts[1].strip()})" if len(parts) > 1 else ""
        return f"{word}[{numeric}]{extra}"

    return _GLOSS_WORD_RE.sub(replace, text)


def _compose_pinyin(word: str) -> str:
    """Build a reading from the per-character MOE readings, when unambiguous.

    Used only when no source gives the whole word a reading (e.g. a Wiktionary
    headword). If any character has zero or several readings, the result would be
    a guess, so "" is returned instead.
    """
    char_map = _moe_char_map()
    syllables: list[str] = []
    for char in word:
        options = char_map.get(char, [])
        if len(options) != 1:
            return ""
        syllables.append(options[0])
    return " ".join(syllables)


def _attach_source_pinyin(entries: list[dict]) -> list[dict]:
    """Fill an empty pinyin from the sources' own readings, in one batch.

    A `*` (all-headwords) page builds minimal rows for headwords CEDICT lacks,
    so those start without a reading. Look the readings up in the source
    databases (e.g. Wiktionary's readings table) instead of leaving them blank.
    """
    words = sorted(
        {entry["traditional"] for entry in entries if not entry.get("pinyin")}
        | {entry["simplified"] for entry in entries if not entry.get("pinyin")}
    )
    if not words:
        return entries
    placeholders = ",".join("?" * len(words))
    found: dict[str, tuple[str, bool, bool, str]] = {}

    def query(source: dict, key: str) -> list[tuple]:
        """Run a batch reading query for this source (empty when unavailable)."""
        sql = source.get(key)
        if not sql or not source["database"].exists():
            return []
        connection = _connect(source["database"])
        try:
            return connection.execute(
                sql.format(ph=placeholders), tuple(words)
            ).fetchall()
        finally:
            connection.close()

    for source in _SOURCE_QUERIES:
        split = bool(source.get("split_pinyin"))
        for word, pinyin in query(source, "pinyin_batch_sql"):
            if not pinyin:
                continue
            current = found.get(word)
            # Prefer a reading without digits (the spelled-out form).
            if current is None or (
                re.search(r"\d", current[0]) and not re.search(r"\d", pinyin)
            ):
                found[word] = (pinyin, split, False, "")

    # Words with no Mandarin reading get a dialect reading (Wu/Shanghainese,
    # then Xiang), left uncolored, with the dialect's name as a label.
    for source in _SOURCE_QUERIES:
        for word, pinyin, label in query(source, "pinyin_fallback_batch_sql"):
            if pinyin and word not in found:
                found[word] = (pinyin, False, True, label or "")

    for entry in entries:
        if entry.get("pinyin"):
            continue
        word = entry["traditional"]
        if word not in found:
            word = entry["simplified"]
        if word not in found:
            continue
        pinyin, split, no_tone, label = found[word]
        entry["pinyin"] = _split_pinyin(pinyin) if split else pinyin
        if no_tone:
            entry["pinyin_no_tone"] = True
            if label:
                entry["pinyin_label"] = label
    return entries


def _ids_images_for(word: str) -> list[str]:
    """Image URLs for an IDS headword (whole, or split into known components)."""
    keys = sorted(IDS_IMAGES, key=len, reverse=True)
    images: list[str] = []
    rest = word
    while rest:
        for key in keys:
            if rest.startswith(key):
                images.append(IDS_IMAGES[key])
                rest = rest[len(key) :]
                break
        else:
            return []
    return images


# CC-CEDICT's own Taiwan reading note, e.g. "Taiwan pr. [xia4 hai2]".
_TAIWAN_PR_RE = re.compile(r"(?:Taiwan|Taiwanese)\s+pr\.\s*\[([^\]]+)\]")


def _cedict_taiwan_reading(text: str) -> str:
    """The marked pinyin from a CEDICT "Taiwan pr. [...]" note, or ""."""
    match = _TAIWAN_PR_RE.search(text or "")
    if not match:
        return ""
    # CEDICT may run the syllables together ("xia4hai2"), so split on each
    # letter run + optional tone digit before marking.
    syllables = re.findall(r"[A-Za-zü:]+[0-9]?", match.group(1))
    return numeric_to_marked(" ".join(syllables))


def _decorate(entries: list[dict]) -> list[dict]:
    """Fill in Taiwan readings, source chips and per-dictionary definitions.

    A whole-word MOE reading is only used when the word has a single CEDICT
    reading - for polyphonic words the MOE reading is keyed by the word, not the
    sense, so it would clobber the entry's specific reading (e.g. 啊's five
    readings collapsing to one, or 呵[ā] becoming hē). Everything else falls back
    to per-character readings, which only swap a character when unambiguous.
    Words in `_TAIWAN_EXCEPTIONS` are skipped entirely.
    """
    # Attach the chips, definitions and frequency first: an entry with no pinyin
    # of its own can borrow the reading from a gloss like
    # "alternative form of 零次元 (língcìyuán)".
    _attach_sources(entries)
    _attach_definitions(entries)
    _attach_freq(entries)
    _attach_source_pinyin(entries)
    for entry in entries:
        if entry.get("pinyin"):
            continue
        for definition in entry.get("definitions", []):
            for sense in definition.get("defs", []):
                reading = _gloss_reading(sense)
                if reading:
                    entry["pinyin"] = reading
                    break
            if entry.get("pinyin"):
                break
        if not entry.get("pinyin"):
            # Last resort: compose the reading from single-character readings.
            entry["pinyin"] = _compose_pinyin(entry["traditional"])

    # Annotate "word (reading)" inside the definitions for the UI (furigana).
    # Done after the reading lookup above, which still needs the plain text.
    for entry in entries:
        for definition in entry.get("definitions", []):
            definition["defs"] = [
                _annotate_gloss(text) for text in definition.get("defs", [])
            ]
            for group in definition.get("groups", []):
                group["senses"] = [
                    _annotate_gloss(text) for text in group.get("senses", [])
                ]

    char_map = _moe_char_map()
    counts = _reading_counts()

    def taiwan_for(word: str, pinyin: str, options: list[str]) -> str | None:
        return _taiwan_reading(word, pinyin, options, counts, char_map)

    # The entry's own word (a manual override in overrides.py takes precedence).
    by_word = _moe_readings([entry["traditional"] for entry in entries])
    for entry in entries:
        override = TAIWAN_READINGS.get(entry["traditional"]) or TAIWAN_READINGS.get(
            entry["simplified"]
        )
        options = by_word.get(entry["traditional"], [])
        if override:
            taiwan = override
        elif options:
            taiwan = taiwan_for(entry["traditional"], entry["pinyin"], options)
        else:
            # No MOE entry: CC-CEDICT may carry its own "Taiwan pr." note.
            taiwan = _cedict_taiwan_reading(
                entry.get("english_normalized", "")
            ) or taiwan_for(entry["traditional"], entry["pinyin"], options)
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
            reading = taiwan_for(traditional, pinyin, ref_readings.get(traditional, []))
            if reading:
                taiwan_refs[simplified] = reading
        entry["taiwan_refs"] = taiwan_refs

    # IDS headwords have no Unicode glyph; use their scraped Commons images.
    for entry in entries:
        for word in (entry.get("traditional", ""), entry.get("simplified", "")):
            if any("\u2ff0" <= char <= "\u2fff" for char in word):
                images = _ids_images_for(word)
                if images:
                    entry["ids_images"] = images
                break

    return entries


@lru_cache(maxsize=8192)
def lookup(word: str) -> list[dict]:
    """Return entries whose simplified or traditional form is exactly `word`.

    Entries are ordered so the reading with the most senses (normally the most
    common one) comes first - e.g. 都[dōu] before 都[dū] and 都[Dū] - because the
    parsed input shows the first entry's reading. Cached: callers only read the
    returned entries (annotate, variant expansion), never mutate them.
    """
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT simplified, traditional, pinyin, english, english_normalized "
            "FROM entries WHERE simplified = ? OR traditional = ? "
            "ORDER BY (SELECT COUNT(*) FROM senses s "
            "          WHERE s.word = entries.simplified "
            "          AND s.reading = entries.pinyin) DESC, "
            "entries.id",
            (word, word),
        ).fetchall()
    finally:
        connection.close()
    entries = _decorate([dict(row) for row in rows])
    # A manual preference (overrides.py) wins over the sense-count order above,
    # so the parsed input shows the reading we want (e.g. 了 -> le, not liǎo).
    preferred = (
        PREFERRED_READINGS.get(word)
        or PREFERRED_READINGS.get(to_simplified(word))
        or PREFERRED_READINGS.get(to_traditional(word))
    )
    if preferred:
        target = to_toneless(preferred)
        entries.sort(key=lambda entry: to_toneless(entry["pinyin"]) != target)
    return entries


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


_freq_rank_ready_cache: bool | None = None


def _freq_rank_ready() -> bool:
    """True when entries.freq_rank was filled at import time."""
    global _freq_rank_ready_cache
    if _freq_rank_ready_cache is None:
        connection = connect()
        try:
            column = connection.execute(
                "SELECT 1 FROM pragma_table_info('entries') WHERE name = 'freq_rank'"
            ).fetchone()
            flag = connection.execute(
                "SELECT value FROM meta WHERE key = 'freq_rank'"
            ).fetchone()
        finally:
            connection.close()
        _freq_rank_ready_cache = bool(column) and flag is not None and flag[0] == "1"
    return _freq_rank_ready_cache


_has_alt_prons_cache: bool | None = None


def _note_or() -> str:
    """The SQL for "or the alternate-pronunciation note matches".

    Uses the precomputed alt_prons table; falls back to scanning the definition
    text for a database built before the table existed.
    """
    if _has_alt_prons():
        return "id IN (SELECT entry_id FROM alt_prons WHERE text LIKE ? ESCAPE '\\')"
    return "english_normalized LIKE ? ESCAPE '\\'"


def _has_alt_prons() -> bool:
    """True when the CEDICT database has the alt_prons table."""
    global _has_alt_prons_cache
    if _has_alt_prons_cache is None:
        connection = connect()
        try:
            _has_alt_prons_cache = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='alt_prons'"
                ).fetchone()
                is not None
            )
        finally:
            connection.close()
    return _has_alt_prons_cache


def _freq_order() -> list[str]:
    """ORDER BY terms that put the most frequent words first (SUBTLEX-CH).

    Empty when SUBTLEX isn't imported. Words missing from SUBTLEX sort last.
    Uses the freq_rank column precomputed at import when it is available, and
    the live subquery otherwise.
    """
    if not SUBTLEX_DB_PATH.exists():
        return []
    if _freq_rank_ready():
        return ["freq_rank IS NULL", "freq_rank"]
    rank = "(SELECT rank FROM subtlex.freq WHERE subtlex.freq.word = entries.simplified)"
    return [f"({rank} IS NULL)", rank]


# Every CEDICT headword (traditional and simplified), built once and reused so
# "_source_only_entries" can skip words the normal search already covers.
_cedict_heads_cache: set[str] | None = None


def _cedict_heads() -> set[str]:
    """Every distinct CEDICT headword, traditional and simplified, cached."""
    global _cedict_heads_cache
    if _cedict_heads_cache is None:
        connection = connect()
        try:
            rows = connection.execute(
                "SELECT traditional FROM entries "
                "UNION SELECT simplified FROM entries"
            ).fetchall()
        finally:
            connection.close()
        _cedict_heads_cache = {row[0] for row in rows}
    return _cedict_heads_cache


# How the UI prioritizes sources: English (CC-CEDICT, Wiktionary) before
# Mainland, then Taiwan. Used to order the secondary-source results.
_LANG_ORDER = {"english": 0, "mainland": 1, "taiwan": 2}
_ANCHOR_ORDER = ["cedict", "wiktionary", "moe"]


def _source_rank(source: dict) -> tuple[int, int]:
    """Sort key matching the result order the UI uses (English before Taiwan)."""
    anchor = source["anchor"]
    return (
        _LANG_ORDER.get(source["lang"], 9),
        _ANCHOR_ORDER.index(anchor) if anchor in _ANCHOR_ORDER else 99,
    )


def _source_only_entries(
    query: str, limit: int = 10, exact: bool = False, allow_no_gloss: bool = False
) -> list[dict]:
    """Headwords that only the secondary sources (or SUBTLEX) have.

    Search results normally come from CEDICT alone. This finds matching headwords
    in the other source databases plus the frequency list, so a word CEDICT lacks
    is still searchable. The rows are CEDICT-shaped (simplified/traditional/
    pinyin/english*) so `_decorate` can attach their definitions, chips and
    frequency like any other entry.

    A normal search uses the same matching rules as the CEDICT search: a plain
    query matches anywhere in the word and an explicit "*" is honoured as typed.
    `exact=True` matches the whole word only, which is what the parsed-input
    lookup needs.
    """
    text = (query or "").strip()
    if not text:
        return []
    all_stars = not text.strip("*").strip()  # "*" (or "**") means every headword
    if all_stars and exact:
        return []
    forms = _script_forms(text)
    placeholders = ",".join("?" * len(forms))
    params = tuple(forms)
    # (form, LIKE pattern) pairs, one per script variant, for a normal
    # (non-exact) search.
    pairs = [
        (form, _like_pattern(form if "*" in text else f"*{form}*"))
        for form in forms
    ]
    patterns = [pattern for _form, pattern in pairs]

    # Words CEDICT already covers are handled by the normal search.
    known = _cedict_heads()

    heads: list[str] = []
    pinyin_by_head: dict[str, str] = {}
    label_by_head: dict[str, str] = {}
    no_tone_by_head: set[str] = set()
    seen: set[str] = set()

    def add(
        head: str, pinyin: str = "", no_tone: bool = False, label: str = ""
    ) -> bool:
        """Record a headword unless CEDICT/another source already has it.

        Returns True once `limit` headwords have been collected.
        """
        key = to_simplified(head)
        if not head or head in known or key in known or key in seen:
            return len(heads) >= limit
        seen.add(key)
        heads.append(head)
        if pinyin:
            pinyin_by_head[head] = pinyin
            if no_tone:
                no_tone_by_head.add(head)
                if label:
                    label_by_head[head] = label
        return len(heads) >= limit

    for source in sorted(_SOURCE_QUERIES, key=_source_rank):
        if source["anchor"] == "cedict" or not source["database"].exists():
            continue
        database = _connect(source["database"])
        try:
            if all_stars:
                if not source.get("all_sql"):
                    continue
                rows = database.execute(source["all_sql"]).fetchall()
            elif allow_no_gloss:
                # Exact match including headwords without a definition (only used
                # when the normal search found nothing at all).
                rows = database.execute(
                    (source.get("heads_sql") or source["sql"]).format(ph=placeholders),
                    params * source["repeat"],
                ).fetchall()
            elif exact:
                rows = database.execute(
                    source["sql"].format(ph=placeholders), params * source["repeat"]
                ).fetchall()
            else:
                # Match the query like the CEDICT search does (see patterns above).
                rows = []
                seen_rows: set[str] = set()
                use_fts = source.get("like_sql_fts") is not None and _has_wiktionary_fts()
                head_source = {"wiktionary": "wikt_head", "moe": "moe_head"}.get(
                    source["anchor"], ""
                )
                for form, pattern in pairs:
                    candidates = (
                        _gram_candidates(head_source, form) if head_source else None
                    )
                    if candidates is not None:
                        # Exact for 1-2 Han-character headwords, already sorted
                        # by word like the LIKE query's ORDER BY.
                        for head in candidates:
                            if head not in seen_rows:
                                seen_rows.add(head)
                                rows.append((head,))
                        continue
                    if use_fts and len(form) >= 3 and _simple_form(form):
                        sql = source["like_sql_fts"]
                        query_pattern = f"%{form}%"
                    else:
                        sql = source["like_sql"]
                        query_pattern = pattern
                    for (head,) in database.execute(sql, (query_pattern,)):
                        if head not in seen_rows:
                            seen_rows.add(head)
                            rows.append((head,))
            for (head,) in rows:
                # CEDICT (or an earlier source) already covers this head: skip it
                # before running any reading lookups. Equivalent to add()'s check,
                # but avoids OpenCC/pinyin queries for heads we would drop anyway.
                key = to_simplified(head)
                if not head or head in known or key in known or key in seen:
                    continue
                pinyin = ""
                if source.get("pinyin_sql"):
                    # Try the headword and its other script form: the spelled-out
                    # reading may live on the other one (e.g. 20國集團 lists
                    # "20 Guó Jítuán" while 20国集团 also has "Èrshí Guó Jítuán").
                    candidates = []
                    for form in _script_forms(head):
                        row = database.execute(source["pinyin_sql"], (form,)).fetchone()
                        if row and row[0]:
                            candidates.append(row[0])
                    # A reading with digits is not really a reading; prefer one
                    # that spells them out when available.
                    pinyin = next(
                        (c for c in candidates if not re.search(r"\d", c)),
                        candidates[0] if candidates else "",
                    )
                if pinyin and source.get("split_pinyin"):
                    # Wiktionary runs some syllables together ("lǎoshǔshǐ").
                    pinyin = _split_pinyin(pinyin)
                no_tone = False
                label = ""
                if not pinyin and source.get("pinyin_fallback_sql"):
                    # No Mandarin reading: fall back to a dialect reading (Wu,
                    # Shanghainese, Xiang), shown as-is and left uncoloured.
                    for form in _script_forms(head):
                        row = database.execute(
                            source["pinyin_fallback_sql"], (form,)
                        ).fetchone()
                        if row and row[0]:
                            pinyin = row[0]
                            label = row[1] or ""
                            no_tone = True
                            break
                if add(head, pinyin, no_tone, label):
                    break
        finally:
            database.close()
        if len(heads) >= limit:
            break

    # The frequency list can contribute headwords too (it has no definitions).
    if len(heads) < limit and SUBTLEX_DB_PATH.exists():
        database = _connect(SUBTLEX_DB_PATH)
        try:
            if all_stars:
                rows = database.execute("SELECT word FROM freq ORDER BY rank").fetchall()
            elif exact:
                rows = database.execute(
                    f"SELECT word FROM freq WHERE word IN ({placeholders}) ORDER BY rank",
                    params,
                ).fetchall()
            else:
                rows = []
                seen_rows = set()
                for pattern in patterns:
                    for (head,) in database.execute(
                        "SELECT word FROM freq WHERE word LIKE ? ESCAPE '\\' "
                        "ORDER BY rank",
                        (pattern,),
                    ):
                        if head not in seen_rows:
                            seen_rows.add(head)
                            rows.append((head,))
        finally:
            database.close()
        for (head,) in rows:
            if add(head):
                break

    return [
        {
            "simplified": to_simplified(head),
            "traditional": to_traditional(head),
            "pinyin": pinyin_by_head.get(head, ""),
            "pinyin_no_tone": head in no_tone_by_head,
            "pinyin_label": label_by_head.get(head, ""),
            "english": "",
            "english_normalized": "",
        }
        for head in heads
    ]


def _source_pinyin_entries(query: str, limit: int = 20) -> list[dict]:
    """Source-only entries whose reading matches a pinyin query.

    CEDICT pinyin search only looks at its own entries; this adds Wiktionary's
    readings (and MOE's) so a word like 剾 (Wiktionary-only, kōu) is found too.
    Returns undecorated rows for the caller to decorate.
    """
    text = (query or "").strip()
    if not text:
        return []
    parsed = _parse_pinyin_query(text)
    if parsed:
        letters, tones, boundaries = parsed
    else:
        letters = to_toneless(text)
        tones, boundaries = {}, set()
    if not letters:
        return []
    pattern = _like_pattern(f"*{letters}*")
    known = _cedict_heads()
    # (rank key, word, marked reading, simplified word)
    found: list[tuple[tuple, str, str, str]] = []

    def consider(
        word: str,
        marked: str,
        numeric: str,
        simplified: str,
        rank: int | None = None,
    ) -> None:
        if simplified in known:
            return
        if parsed:
            rank = _reading_rank(numeric, letters, tones, boundaries)
            if rank is None:
                return
        elif rank is None:
            rank = 0 if to_toneless(marked) == letters else 1
        found.append(((rank, len(marked), word), word, marked, simplified))

    if WIKTIONARY_DB_PATH.exists():
        connection = _connect(WIKTIONARY_DB_PATH)
        try:
            # readings_extra holds only Mandarin readings CEDICT does not cover,
            # so the common search never touches the full readings table.
            extra = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='readings_extra'"
            ).fetchone()
            if extra:
                table = "readings_extra"
                where = "pinyin_toneless LIKE ? ESCAPE '\\'"
            else:
                table = "readings"
                where = ("kind = 'mandarin' AND is_cedict = 0 "
                         "AND pinyin_toneless LIKE ? ESCAPE '\\'")
            columns = ("id, word, pinyin, pinyin_toneless, pinyin_spaced, "
                       "pinyin_numeric, simplified")

            def process(row: tuple) -> None:
                _id, word, pinyin, toneless, spaced, numeric, simplified = row
                # Fall back to query-time work for a database built before the
                # precomputed columns existed.
                marked = spaced or _split_pinyin(pinyin)
                numeric = numeric or to_numeric(marked)
                simplified = simplified or to_simplified(word)
                rank = None if parsed else (0 if toneless == letters else 1)
                consider(word, marked, numeric, simplified, rank)

            if parsed:
                # Tone/boundary ranking depends on the query, so keep it in
                # Python (on the precomputed columns). Rank 0/1 rows must start
                # with the letters: fetch those first, then the "contains" rows
                # only when the page is not full yet.
                phases = ((f"{letters}%", None), (pattern, f"{letters}%"))
                for pat, exclude in phases:
                    if exclude is None:
                        q = (f"SELECT {columns} FROM {table} "
                             "WHERE pinyin_toneless LIKE ? ESCAPE '\\'")
                        params = (pat,)
                    else:
                        q = (f"SELECT {columns} FROM {table} "
                             "WHERE pinyin_toneless LIKE ? ESCAPE '\\' "
                             "AND pinyin_toneless NOT LIKE ? ESCAPE '\\'")
                        params = (pat, exclude)
                    for row in connection.execute(q, params).fetchall():
                        process(row)
                    if len(found) >= limit:
                        break
            elif extra:
                # readings_extra is already deduped at import with the same
                # winner rule, so rank and limit directly.
                q = (
                    f"SELECT {columns} FROM {table} WHERE {where} "
                    "ORDER BY (pinyin_toneless = ?) DESC, "
                    "length(pinyin_spaced), word, id LIMIT ?"
                )
                for row in connection.execute(
                    q, (pattern, letters, limit)
                ).fetchall():
                    process(row)
            else:
                # Old database without readings_extra: dedupe in SQL.
                q = (
                    f"SELECT {columns} FROM ("
                    f"  SELECT {columns}, ROW_NUMBER() OVER ("
                    "     PARTITION BY simplified, pinyin_spaced ORDER BY word, id"
                    "   ) AS rn "
                    f"  FROM {table} WHERE {where}"
                    ") WHERE rn = 1 "
                    "ORDER BY (pinyin_toneless = ?) DESC, "
                    "length(pinyin_spaced), word, id LIMIT ?"
                )
                for row in connection.execute(
                    q, (pattern, letters, limit)
                ).fetchall():
                    process(row)
        finally:
            connection.close()

    found.sort(key=lambda item: item[0])
    seen: set[tuple[str, str]] = set()
    entries: list[dict] = []
    for _rank, word, marked, simplified in found:
        key = (simplified, marked)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "simplified": simplified,
                "traditional": to_traditional(word),
                "pinyin": marked,
                "english": "",
                "english_normalized": "",
            }
        )
        if len(entries) >= limit:
            break
    return entries


def _source_gloss_entries(query: str, limit: int = 20) -> list[dict]:
    """Wiktionary headwords whose English gloss matches `query` (undecorated)."""
    text = (query or "").strip()
    if not text or has_cjk(text) or not WIKTIONARY_DB_PATH.exists():
        return []
    pattern = _like_pattern(text if "*" in text else f"*{text}*")
    connection = _connect(WIKTIONARY_DB_PATH)
    try:
        # A three-or-more character query uses the trigram index (which covers
        # the gloss column) and keeps the table's rowid order for the LIMIT.
        if "*" not in text and len(text) >= 3 and _has_wiktionary_fts():
            rows = connection.execute(
                "SELECT word FROM wiktionary_fts WHERE gloss LIKE ? "
                "ORDER BY rowid LIMIT ?",
                (f"%{text}%", limit * 3),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT DISTINCT word FROM wiktionary "
                "WHERE gloss IS NOT NULL AND gloss LIKE ? ESCAPE '\\' LIMIT ?",
                (pattern, limit * 3),
            ).fetchall()
    finally:
        connection.close()
    known = _cedict_heads()
    seen: set[str] = set()
    entries: list[dict] = []
    for (word,) in rows:
        simplified = to_simplified(word)
        if simplified in known or simplified in seen:
            continue
        seen.add(simplified)
        entries.append(
            {
                "simplified": simplified,
                "traditional": to_traditional(word),
                "pinyin": "",
                "english": "",
                "english_normalized": "",
            }
        )
        if len(entries) >= limit:
            break
    return entries


_definition_grams_cache: set[str] | None = None


def _definition_grams() -> set[str]:
    """Han unigrams/bigrams seen in any definition (empty when not built)."""
    global _definition_grams_cache
    if _definition_grams_cache is None:
        grams: set[str] = set()
        if DEFINITION_GRAMS_DB_PATH.exists():
            connection = _connect(DEFINITION_GRAMS_DB_PATH)
            try:
                grams = {row[0] for row in connection.execute("SELECT gram FROM grams")}
            finally:
                connection.close()
        _definition_grams_cache = grams
    return _definition_grams_cache


def _is_han(char: str) -> bool:
    """True for a Han ideograph, including the supplementary planes."""
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x323AF
    )


def _simple_form(form: str) -> bool:
    """True when the form has no LIKE/FTS wildcard characters."""
    return not any(char in form for char in "*%_")


def _definition_possible(form: str) -> bool:
    """False when a Han gram of `form` is absent from every definition.

    A missing unigram/bigram means the substring cannot occur, so the expensive
    definition scans can be skipped. A false positive just falls through to the
    scan, so this never loses a result.
    """
    grams = _definition_grams()
    if not grams or not _simple_form(form):
        return True
    run: list[str] = []
    for char in form + " ":  # the space flushes the final run
        if _is_han(char):
            run.append(char)
            continue
        for index, current in enumerate(run):
            if current not in grams:
                return False
            if index + 1 < len(run) and (current + run[index + 1]) not in grams:
                return False
        run.clear()
    return True


_GRAM_CANDIDATE_CAP = 4000


def _gram_candidates(source: str, form: str) -> list[str] | None:
    """Words containing a 1-2 Han-character `form`, or None to fall back.

    The inverted index turns a 1-2 character query into one lookup. None means
    the index is unavailable, the form is not a pure Han 1-2 character string,
    or there are too many candidates for a SQL IN list; the caller then uses the
    normal scan. An empty list means the form occurs nowhere.
    """
    if not (
        GRAM_WORDS_DB_PATH.exists()
        and 1 <= len(form) <= 2
        and all(_is_han(char) for char in form)
    ):
        return None
    connection = _connect(GRAM_WORDS_DB_PATH)
    try:
        rows = connection.execute(
            "SELECT word FROM gram_words WHERE source = ? AND gram = ? "
            "ORDER BY word LIMIT ?",
            (source, form, _GRAM_CANDIDATE_CAP + 1),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return []
    if len(rows) > _GRAM_CANDIDATE_CAP:
        return None
    return [row[0] for row in rows]


_moe_fts_cache: bool | None = None


def _has_moe_fts() -> bool:
    """True when the MOE database has the trigram senses_fts table."""
    global _moe_fts_cache
    if _moe_fts_cache is None:
        connection = _connect(MOE_DB_PATH)
        try:
            _moe_fts_cache = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='senses_fts'"
                ).fetchone()
                is not None
            )
        finally:
            connection.close()
    return _moe_fts_cache


_wiktionary_fts_cache: bool | None = None


def _has_wiktionary_fts() -> bool:
    """True when the Wiktionary database has the trigram wiktionary_fts table."""
    global _wiktionary_fts_cache
    if _wiktionary_fts_cache is None:
        connection = _connect(WIKTIONARY_DB_PATH)
        try:
            _wiktionary_fts_cache = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='wiktionary_fts'"
                ).fetchone()
                is not None
            )
        finally:
            connection.close()
    return _wiktionary_fts_cache


def _source_definition_entries(query: str, limit: int = 20) -> list[dict]:
    """Headwords whose definition text contains `query` (quoted search).

    Unlike `_source_gloss_entries`, this also searches MOE's Chinese definitions,
    so quoting a Chinese phrase (e.g. "一種中藥。") finds the word it defines.
    Returns undecorated rows for the caller to decorate.
    """
    text = (query or "").strip()
    if not text:
        return []
    pairs = [(form, _like_pattern(f"*{form}*")) for form in _script_forms(text)]
    # Exact negative filter: if no script form's Han grams all occur in the
    # definitions, the scans cannot find anything.
    if not any(_definition_possible(form) for form, _pattern in pairs):
        return []
    seen: set[str] = set()
    entries: list[dict] = []

    def add(word: str) -> None:
        key = to_simplified(word)
        if not word or key in seen:
            return
        seen.add(key)
        entries.append(
            {
                "simplified": key,
                "traditional": word,
                "pinyin": "",
                "english": "",
                "english_normalized": "",
            }
        )

    if MOE_DB_PATH.exists():
        connection = _connect(MOE_DB_PATH)
        try:
            use_fts = _has_moe_fts()
            for form, pattern in pairs:
                candidates = _gram_candidates("moe_sense", form)
                if candidates is not None:
                    if not candidates:
                        continue
                    holes = ",".join("?" * len(candidates))
                    sql = (
                        f"SELECT word FROM senses WHERE word IN ({holes}) "
                        "AND text LIKE ? ESCAPE '\\' GROUP BY word "
                        "ORDER BY word LIMIT ?"
                    )
                    params = (*candidates, pattern, limit)
                elif use_fts and len(form) >= 3 and _simple_form(form):
                    # The trigram index makes LIKE use the index; no ESCAPE.
                    sql = (
                        "SELECT DISTINCT word FROM senses_fts "
                        "WHERE text LIKE ? LIMIT ?"
                    )
                    params = (f"%{form}%", limit)
                else:
                    sql = (
                        "SELECT DISTINCT word FROM senses "
                        "WHERE text LIKE ? ESCAPE '\\' LIMIT ?"
                    )
                    params = (pattern, limit)
                for (word,) in connection.execute(sql, params):
                    add(word)
        finally:
            connection.close()
    if WIKTIONARY_DB_PATH.exists():
        connection = _connect(WIKTIONARY_DB_PATH)
        try:
            use_fts = _has_wiktionary_fts()
            for form, pattern in pairs:
                candidates = _gram_candidates("wikt_gloss", form)
                if candidates is not None:
                    if not candidates:
                        continue
                    holes = ",".join("?" * len(candidates))
                    sql = (
                        f"SELECT word FROM wiktionary WHERE word IN ({holes}) "
                        "AND gloss IS NOT NULL AND gloss LIKE ? ESCAPE '\\' "
                        "GROUP BY word ORDER BY word LIMIT ?"
                    )
                    params = (*candidates, pattern, limit)
                elif use_fts and len(form) >= 3 and _simple_form(form):
                    sql = (
                        "SELECT DISTINCT word FROM wiktionary_fts "
                        "WHERE gloss LIKE ? LIMIT ?"
                    )
                    params = (f"%{form}%", limit)
                else:
                    sql = (
                        "SELECT DISTINCT word FROM wiktionary "
                        "WHERE gloss IS NOT NULL "
                        "AND gloss LIKE ? ESCAPE '\\' LIMIT ?"
                    )
                    params = (pattern, limit)
                for (word,) in connection.execute(sql, params):
                    add(word)
        finally:
            connection.close()
    return entries[:limit]


# The note shown under entries that use IDS descriptors (no Unicode glyph).
_IDS_NOTE = "This entry contains characters that cannot be rendered with Unicode."


# The IDS result is the same for every query, so scan for ⿰ once and reuse it.
_ids_cache: list[dict] | None = None


def _ids_entries() -> list[dict]:
    """Every entry whose headword or definition uses an IDS (⿰) character."""
    global _ids_cache
    if _ids_cache is not None:
        # Hand out copies: the caller decorates these dicts in place.
        return [dict(entry) for entry in _ids_cache]
    entries: list[dict] = []
    seen: set[str] = set()

    def add(word: str) -> None:
        key = to_simplified(word)
        if not word or key in seen:
            return
        seen.add(key)
        entries.append(
            {
                "simplified": key,
                "traditional": word,
                "pinyin": "",
                "english": "",
                "english_normalized": "",
            }
        )

    if WIKTIONARY_DB_PATH.exists():
        connection = _connect(WIKTIONARY_DB_PATH)
        try:
            rows = connection.execute(
                "SELECT DISTINCT word FROM wiktionary "
                "WHERE word LIKE '%⿰%' OR (gloss IS NOT NULL AND gloss LIKE '%⿰%')"
            ).fetchall()
        finally:
            connection.close()
        for (word,) in rows:
            add(word)
    if MOE_DB_PATH.exists():
        connection = _connect(MOE_DB_PATH)
        try:
            rows = connection.execute(
                "SELECT DISTINCT word FROM senses WHERE text LIKE '%⿰%'"
            ).fetchall()
        finally:
            connection.close()
        for (word,) in rows:
            add(word)
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT simplified, traditional FROM entries "
            "WHERE simplified LIKE '%⿰%' OR traditional LIKE '%⿰%' "
            "OR english LIKE '%⿰%'"
        ).fetchall()
    finally:
        connection.close()
    for simplified, traditional in rows:
        add(traditional or simplified)
    _ids_cache = entries
    return [dict(entry) for entry in entries]


def _word_entries(words: list[str], limit: int = 50) -> list[dict]:
    """Minimal entries for a list of headwords, de-duplicated (undecorated)."""
    unique = sorted({word for word in words if word})
    pinyin_by_word: dict[str, str] = {}
    if unique:
        placeholders = ",".join("?" * len(unique))
        connection = connect()
        try:
            rows = connection.execute(
                f"SELECT traditional, simplified, pinyin FROM entries "
                f"WHERE traditional IN ({placeholders}) "
                f"OR simplified IN ({placeholders})",
                unique * 2,
            ).fetchall()
        finally:
            connection.close()
        for traditional, simplified, pinyin in rows:
            pinyin_by_word.setdefault(traditional, pinyin)
            pinyin_by_word.setdefault(simplified, pinyin)

    entries: list[dict] = []
    seen: set[str] = set()
    for word in words:
        key = to_simplified(word)
        if not word or key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "simplified": key,
                "traditional": word,
                "pinyin": pinyin_by_word.get(word, ""),
                "english": "",
                "english_normalized": "",
            }
        )
        if len(entries) >= limit:
            break
    return entries


def _note_entries(rows: list[tuple], limit: int) -> list[dict]:
    """Entries carrying the CEDICT text the frontend parses for notes."""
    entries: list[dict] = []
    seen: set[str] = set()
    for simplified, traditional, pinyin, normalized in rows:
        key = to_simplified(simplified or traditional)
        if not key or key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "simplified": key,
                "traditional": traditional or simplified,
                "pinyin": pinyin or "",
                "english": "",
                "english_normalized": normalized or "",
            }
        )
        if len(entries) >= limit:
            break
    return entries


def _entries_with_taiwan_pron(limit: int = 50) -> list[dict]:
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT simplified, traditional, pinyin, english_normalized FROM entries "
            "WHERE english_normalized LIKE '%Taiwan pr. [%' "
            "OR english_normalized LIKE '%Taiwanese pr. [%' LIMIT ?",
            (limit * 3,),
        ).fetchall()
    finally:
        connection.close()
    return _note_entries(rows, limit)


def _entries_with_alt_pron(limit: int = 50) -> list[dict]:
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT simplified, traditional, pinyin, english_normalized FROM entries "
            "WHERE (english_normalized LIKE '%pr. [%' "
            "OR english_normalized LIKE '%pronounced [%') "
            "AND english_normalized NOT LIKE '%Taiwan pr. [%' "
            "AND english_normalized NOT LIKE '%Taiwanese pr. [%' LIMIT ?",
            (limit * 3,),
        ).fetchall()
    finally:
        connection.close()
    return _note_entries(rows, limit)


def _entries_with_forms(limit: int = 50) -> list[dict]:
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT DISTINCT main_trad, main_simp FROM forms LIMIT ?", (limit * 3,)
        ).fetchall()
    finally:
        connection.close()
    return _word_entries([row[0] or row[1] for row in rows], limit)


def _entries_with_classifiers(limit: int = 50) -> list[dict]:
    words: list[str] = []
    for path in (DB_PATH, MOE_DB_PATH, WIKTIONARY_DB_PATH):
        if not path.exists():
            continue
        connection = _connect(path)
        try:
            words.extend(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT word FROM classifiers LIMIT ?", (limit * 2,)
                )
            )
        finally:
            connection.close()
    return _word_entries(words, limit)


def _entries_with_frequency(limit: int = 50) -> list[dict]:
    if not SUBTLEX_DB_PATH.exists():
        return []
    connection = _connect(SUBTLEX_DB_PATH)
    try:
        rows = connection.execute(
            "SELECT word FROM freq ORDER BY rank LIMIT ?", (limit * 2,)
        ).fetchall()
    finally:
        connection.close()
    return _word_entries([row[0] for row in rows], limit)


def _extra_label_entries(phrase: str, limit: int) -> list[dict] | None:
    """Entries for a quoted phrase that names an app-added section label."""
    text = phrase.strip().lower().rstrip(": ")
    if text in _IDS_NOTE.lower():
        return _ids_entries()
    labels = (
        ("taiwan pronunciation", _entries_with_taiwan_pron),
        ("also pronounced", _entries_with_alt_pron),
        ("other forms", _entries_with_forms),
        ("classifier", _entries_with_classifiers),
        ("frequency", _entries_with_frequency),
    )
    for label, fetch in labels:
        if text == label or (len(text) >= 3 and (
            label.startswith(text) or text.startswith(label)
        )):
            return fetch(limit)
    return None


def _source_matches(query: str, limit: int = 20) -> list[dict]:
    """Every source-only match for a query: headword, pinyin and English gloss."""
    text = (query or "").strip()
    if not text:
        return []
    entries = list(_source_only_entries(query, limit=limit))
    if has_cjk(text):
        # Searching a Chinese phrase also matches source definitions (e.g. the
        # IDS characters named inside a Wiktionary gloss).
        entries.extend(_source_definition_entries(query, limit=limit))
    else:
        entries.extend(_source_pinyin_entries(query, limit=limit))
        entries.extend(_source_gloss_entries(query, limit=limit))
    seen: set[str] = set()
    unique: list[dict] = []
    for entry in entries:
        key = entry["simplified"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


# How many results a "list everything" (*) page shows.
ALL_PAGE_SIZE = 50

# Cache of every distinct simplified headword across all sources, sorted.
_all_headwords_cache: list[str] | None = None


def _all_headwords_list() -> list[str]:
    """Every distinct simplified headword across all sources, sorted and cached.

    Simplified forms are collected (via OpenCC) so traditional/simplified pairs
    collapse to one entry - otherwise a page of 100 raw headwords would display
    fewer rows after the pairs are merged.
    """
    global _all_headwords_cache
    if _all_headwords_cache is not None:
        return _all_headwords_cache
    connection = connect()
    try:
        parts = [
            "SELECT simplified AS headword FROM entries",
            "SELECT traditional FROM entries",
        ]
        # Which headwords actually have a reading. Ones that do not are sorted to
        # the end, so they only appear on the last pages of a "*" search.
        has_reading: set[str] = {
            row[0]
            for row in connection.execute(
                "SELECT simplified FROM entries WHERE pinyin <> ''"
            )
        }
        # Which headwords actually have a definition (so ones that do not can be
        # listed after every other entry).
        def_words: set[str] = {
            row[0] for row in connection.execute("SELECT DISTINCT word FROM senses")
        }
        if MOE_DB_PATH.exists():
            # Only MOE words that actually have a sense (definition).
            parts.append("SELECT DISTINCT word FROM moe.senses WHERE text <> ''")
            has_reading.update(
                to_simplified(row[0])
                for row in connection.execute(
                    "SELECT word FROM moe.moe WHERE pinyin <> ''"
                )
            )
            def_words.update(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT word FROM moe.senses WHERE text <> ''"
                )
            )
        if WIKTIONARY_DB_PATH.exists():
            connection.execute("ATTACH DATABASE ? AS wikt", (str(WIKTIONARY_DB_PATH),))
            # A Wiktionary headword with no gloss has nothing to show; leave it
            # out (the frequency list has no definitions either, so it is not a
            # headword source here).
            parts.append(
                "SELECT DISTINCT word FROM wikt.wiktionary WHERE gloss IS NOT NULL"
            )
            has_reading.update(
                to_simplified(row[0])
                for row in connection.execute(
                    "SELECT word FROM wikt.readings WHERE pinyin <> ''"
                )
            )
            def_words.update(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT word FROM wikt.wiktionary "
                    "WHERE gloss IS NOT NULL"
                )
            )
        union = " UNION ".join(parts)
        rows = connection.execute(f"SELECT DISTINCT headword FROM ({union})").fetchall()
    finally:
        connection.close()
    seen: set[str] = set()
    heads: list[str] = []
    for (head,) in rows:
        key = to_simplified(head)
        if key and key not in seen:
            seen.add(key)
            heads.append(key)
    # Definition-bearing headwords first (with a reading before without), then
    # IDS headwords (⿰, which fonts may not render), then definition-less ones.
    def order(head: str) -> tuple:
        if "⿰" in head:
            return (2, head)
        has_def = head in def_words or to_traditional(head) in def_words
        has_read = head in has_reading
        if has_def:
            return (0 if has_read else 1, head)
        return (3 if has_read else 4, head)

    heads.sort(key=order)
    _all_headwords_cache = heads
    return heads


def all_headwords(offset: int = 0, limit: int = ALL_PAGE_SIZE) -> tuple[list[str], int]:
    """One page of every distinct headword across all sources, plus the total.

    Used by the "*" search. Headwords are ordered by Unicode so paging is stable.
    """
    heads = _all_headwords_list()
    return heads[offset : offset + limit], len(heads)


def entries_for_headwords(headwords: list[str]) -> list[dict]:
    """Build CEDICT-shaped entries for a page of headwords.

    A headword CEDICT knows uses its real row; one only the other sources have
    gets a minimal row, so `_decorate` can still attach its definitions, chips
    and frequency.
    """
    if not headwords:
        return []
    placeholders = ",".join("?" * len(headwords))
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT simplified, traditional, pinyin, english, english_normalized "
            f"FROM entries WHERE simplified IN ({placeholders}) "
            f"OR traditional IN ({placeholders})",
            headwords + headwords,
        ).fetchall()
    finally:
        connection.close()

    by_word: dict[str, dict] = {}
    for row in rows:
        entry = dict(row)
        by_word.setdefault(entry["simplified"], entry)
        by_word.setdefault(entry["traditional"], entry)

    entries: list[dict] = []
    seen: set[str] = set()
    for head in headwords:
        key = to_simplified(head)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            by_word.get(head)
            or {
                "simplified": key,
                "traditional": to_traditional(head),
                "pinyin": "",
                "english": "",
                "english_normalized": "",
            }
        )
    return _decorate(entries)


def source_entries(word: str) -> list[dict]:
    """Source-only entries for a word that CEDICT lacks (used by annotate).

    Returns at most one decorated CEDICT-shaped row (e.g. from MOE) so the parsed
    input can show its reading and be clickable, even though it has no CEDICT
    entry. Non-Chinese tokens (pinyin, punctuation, "*") get nothing.
    """
    if not has_cjk(word):
        return []
    return _decorate(_source_only_entries(word, limit=1, exact=True))


def search(
    query: str, limit: int = 50, taiwan: bool = False, part: str = "all"
) -> tuple[list[dict], bool]:
    """Search the dictionary.

    A "quoted" query is an English phrase search (whole words, via the FTS
    index). Otherwise the query matches the Chinese word (simplified column; the
    query is converted to simplified first), the English definitions, and the
    toneless pinyin reading (e.g. "dangan" finds 单干 and 档案). With `taiwan`
    set, the pinyin reading also matches Taiwan (MOE) readings. '*' matches zero
    or more characters.

    `part="primary"` returns only the fast CEDICT tier and `part="secondary"`
    only the slow source-only tier, so the UI can render results progressively;
    the combined list is exactly the same either way.
    """
    query = query.strip()
    if not query:
        return [], False  # nothing to search for

    # "Quoted" -> English phrase search only.
    if len(query) >= 2 and query.startswith('"') and query.endswith('"'):
        phrase = query[1:-1].strip()
        if not phrase:
            return [], False
        # The note and section labels are generated by the app, not stored in a
        # dictionary: a quoted phrase from one of them returns those entries.
        extra = _extra_label_entries(phrase, limit)
        if extra is not None:
            return _decorate(extra), False
        order = ", ".join(_freq_order() + ["length(simplified)", "id"])
        sql = (
            "SELECT simplified, traditional, pinyin, english, english_normalized "
            "FROM entries WHERE id IN ("
            "    SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?"
            f") ORDER BY {order} LIMIT ?"
        )
        params = ['"' + phrase.replace('"', '""') + '"', limit]
        connection = connect()
        try:
            rows = connection.execute(sql, params).fetchall()
        finally:
            connection.close()
        fts_rows = [dict(row) for row in rows]
        # A CEDICT FTS hit and a source definition hit can be the same word.
        fts_keys: set[str] = set()
        fts_unique: list[dict] = []
        for entry in fts_rows:
            key = entry["simplified"]
            if key not in fts_keys:
                fts_keys.add(key)
                fts_unique.append(entry)
        source_rows = [
            entry
            for entry in _source_definition_entries(phrase, limit=limit)
            if entry["simplified"] not in fts_keys
        ]
        decorated = _decorate(fts_unique + source_rows)
        if part == "primary":
            return decorated[: len(fts_unique)], False
        return decorated, False

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
        # Rank 0 and 1 require the reading to start with the query's letters, so
        # fetch those rows first and only run the "contains" scan when they do
        # not fill the page. With the Taiwan toggle the rank uses the Taiwan
        # reading, so the prefix set is not a superset: fetch once, as before.
        note = f"pr. [{query}"
        note_pattern = _like_pattern(f"*{note}*")
        prefix = _like_pattern(f"{letters}*")
        contains = _like_pattern(f"*{letters}*")
        counts = _reading_counts()
        char_map = _moe_char_map()
        connection = connect()
        try:
            ranked: list[tuple] = []
            seen_ids: set[int] = set()
            phases = [(prefix, None)]
            if not taiwan:
                phases.append((contains, prefix))
            for pattern, exclude in phases:
                if exclude is None:
                    where = "pinyin_toneless LIKE ? ESCAPE '\\'"
                    params = (note, pattern, note_pattern)
                else:
                    where = (
                        "pinyin_toneless LIKE ? ESCAPE '\\' "
                        "AND pinyin_toneless NOT LIKE ? ESCAPE '\\'"
                    )
                    params = (note, pattern, exclude, note_pattern)
                rows = connection.execute(
                    "SELECT id, simplified, traditional, pinyin, pinyin_numeric, "
                    "instr(english_normalized, ?) > 0 AS alt FROM entries "
                    f"WHERE {where} OR {_note_or()}",
                    params,
                ).fetchall()
                words_by = (
                    _moe_readings([row["traditional"] for row in rows])
                    if taiwan
                    else {}
                )
                for row in rows:
                    if row["id"] in seen_ids:
                        continue
                    seen_ids.add(row["id"])
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
                    if rank is None and row["alt"]:
                        rank = 0
                    if rank is not None:
                        ranked.append((rank, len(row["simplified"]), row["id"]))
                if not taiwan and len(ranked) >= limit:
                    break
            ranked.sort(key=lambda item: item[:3])
            top_ids = [item[2] for item in ranked[:limit]]
            full: dict[int, dict] = {}
            if top_ids:
                holes = ",".join("?" * len(top_ids))
                for row in connection.execute(
                    "SELECT id, simplified, traditional, pinyin, pinyin_numeric, "
                    "english, english_normalized FROM entries WHERE id IN "
                    f"({holes})",
                    tuple(top_ids),
                ):
                    full[row["id"]] = dict(row)
        finally:
            connection.close()
        top = []
        for rank, _length, _id in ranked[:limit]:
            row = full[_id]
            row["_search_rank"] = rank
            top.append(row)
        # Decorate the CEDICT rows and the source-only rows in one pass: each
        # decorate call opens many connections.
        source_rows = [] if part == "primary" else _source_pinyin_entries(query, limit=limit)
        decorated = _decorate(top + source_rows)
        result = decorated[: len(top)]
        # Within the relevance group (whole reading, starts with, contains), put
        # the most frequent words first; words with no frequency go last.
        result.sort(
            key=lambda entry: (
                entry.get("_search_rank", 99),
                entry.get("freq") is None,
                entry["freq"]["rank"] if entry.get("freq") else 0,
            )
        )
        for entry in result:
            entry.pop("_search_rank", None)
        if part == "primary":
            return result, True
        result.extend(decorated[len(top):])
        return result, True

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
            freq_order = ", ".join(_freq_order())
            freq_clause = f", {freq_order}" if freq_order else ""
            note_pattern = _like_pattern(f"*pr. [{pinyin_key}*")
            rows = connection.execute(
                "SELECT simplified, traditional, pinyin, english, english_normalized "
                "FROM entries WHERE pinyin_toneless LIKE ? ESCAPE '\\' "
                f"OR {_note_or()} "
                "ORDER BY (pinyin_toneless = ?) DESC, "
                "(english_normalized LIKE ? ESCAPE '\\') DESC"
                f"{freq_clause}, length(simplified), id LIMIT ?",
                (pattern, note_pattern, pinyin_key, note_pattern, limit),
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
            source_rows = [] if part == "primary" else _source_pinyin_entries(query, limit=limit)
            decorated = _decorate(entries + source_rows)
            if part == "primary":
                return decorated[: len(entries)], True
            return decorated, True

    # Chinese: convert to simplified. A plain query matches anywhere in the word,
    # ranked exact first, then words starting with the query, then the rest.
    chinese = to_simplified(query)
    literal = chinese.replace("*", "")  # the "exact" word, ranked first

    # Only a query containing hanzi can match the Chinese word column; a latin
    # query is pinyin or English and must not match latin headwords like "word".
    is_chinese = has_cjk(literal)

    if "*" in query:
        # Explicit wildcard (e.g. "*字"): honour it as typed.
        pattern_chinese = _like_pattern(chinese)
        prefix_chinese = pattern_chinese
    else:
        pattern_chinese = _like_pattern(f"*{chinese}*")  # contains
        prefix_chinese = _like_pattern(f"{chinese}*")    # starts with

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
        order_parts.append("(simplified LIKE ? ESCAPE '\\') DESC")  # starts with
        order_params.append(prefix_chinese)
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
    order_parts += _freq_order() + ["length(simplified)", "id"]

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

    entries = [dict(row) for row in rows]
    # Also surface headwords that only the secondary sources or the frequency
    # list have, so a word CEDICT lacks is still findable.
    source_rows = [] if part == "primary" else _source_matches(query, limit=20)
    decorated = _decorate(entries + source_rows)
    if part == "primary":
        return decorated[: len(entries)], False
    if not decorated:
        # Nothing matched at all: allow a headword that only exists without a
        # definition (e.g. 18禁), so an exact search still shows it with its
        # source link instead of an empty page.
        return _decorate(
            _source_only_entries(query, limit=10, exact=True, allow_no_gloss=True)
        ), False
    return decorated, False



# Trim set for English-word tokens; must match scripts/import_english_words.py.
_ENGLISH_WORD_STRIP = " \t\r\n!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~“”‘’「」【】《》…·—–~"


def has_english_match(phrase: str) -> bool:
    """True when the quoted English-search hint applies to `phrase`.

    The hint is shown only when the query is a whole word from the definitions
    (scripts/import_english_words.py builds the word list), or when it names one
    of the app's section labels - never for an arbitrary substring of a
    definition.
    """
    phrase = phrase.strip()
    extra = _extra_label_entries(phrase, 1)
    if extra is not None:
        return bool(extra)
    tokens = []
    for token in phrase.split():
        word = token.strip(_ENGLISH_WORD_STRIP).lower()
        if word:
            tokens.append(word)
    if not tokens:
        return False
    if not ENGLISH_WORDS_DB_PATH.exists():
        # Fall back to the word-based FTS check before the word DB is built.
        connection = connect()
        try:
            hit = connection.execute(
                "SELECT 1 FROM entries_fts WHERE entries_fts MATCH ? LIMIT 1",
                ('"' + phrase.replace('"', '""') + '"',),
            ).fetchone()
        finally:
            connection.close()
        return bool(hit)
    connection = _connect(ENGLISH_WORDS_DB_PATH)
    try:
        for word in tokens:
            hit = connection.execute(
                "SELECT 1 FROM words WHERE word = ? LIMIT 1", (word,)
            ).fetchone()
            if not hit:
                return False
    finally:
        connection.close()
    return True


def warm_caches() -> None:
    """Build the per-process caches before the first search.

    These only change when a dictionary is re-imported, but each reads a large
    table or builds a converter. Running them at startup keeps the first request
    fast, including the multi-second first "*" listing and the first bopomofo
    query.
    """
    _opencc("t2s")
    _opencc("s2t")
    _cedict_heads()
    _cedict_syllables()
    if MOE_DB_PATH.exists():
        _moe_toneless_map()
        _moe_char_map()
    _reading_counts()
    _ids_entries()
    _all_headwords_list()
    _zhuyin_map()
    _definition_grams()
