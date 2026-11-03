"""SQLite access for the dictionary."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import sqlite3
import sys
import time
import unicodedata
from bisect import bisect_left
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Collection, Iterable
from functools import lru_cache
from pathlib import Path

from opencc import OpenCC

from yabai_search import perf_debug as pd
from yabai_search import relevance
from yabai_search.overrides import (
    HIDE_FORMS,
    IDS_IMAGES,
    PREFERRED_READINGS,
    TAIWAN_READINGS,
    normalize_english,
)
from yabai_search.pinyin import (
    _MARK_TO_BASE,
    is_standard_pinyin,
    numeric_to_marked,
    split_pinyin,
    to_numeric,
    to_toneless,
)


def reading_key(pinyin: str | None) -> str:
    """A tone-aware identity for one reading: 'zhī' / 'zhi1' / 'Zhi1' -> 'zhi1'.

    `to_toneless` deliberately throws the tone away (it is the search key), so it
    cannot tell 只 zhī from 只 zhǐ. Anything that decides which reading a row
    belongs to (variant redirects, "Other forms", preferred readings, which
    definition group to show) must compare these keys instead. Tone-marked and
    numeric spellings of the same reading give the same key; '' means "unknown".
    """
    text = (pinyin or "").strip().lower()
    if not text:
        return ""
    text = text.replace("u:", "ü")
    if any(char.isdigit() for char in text):  # already numeric ("zhi1")
        return " ".join(text.replace("v", "ü").split())
    return to_numeric(text).replace("v", "ü")


# CEDICT cross-reference senses ("variant of 隻|只[zhi1]", "old variant of ...")
# say where to look, not what a word means. They must not count when choosing
# the representative reading of a headword, or a reading that is mostly
# cross-references outranks the real one. (SQL: use with `s.text`/`text`.)
_XREF_PREFIXES = (
    "variant of ",
    "old variant of ",
    "archaic variant of ",
    "erhua variant of ",
    "also written ",
)
_XREF_SQL = " AND ".join(
    f"lower(ltrim(text)) NOT LIKE '{prefix}%'" for prefix in _XREF_PREFIXES
)


def _preferred_key(pinyin: str | None) -> str:
    """Comparison key for a preferred reading (tone-aware)."""
    return reading_key(pinyin)


# "variant of 隻|只[zhi1]" -> groups ("隻", "只"): the traditional and simplified
# form being pointed at (the simplified part is optional).
_XREF_TARGET_RE = re.compile(
    r"^(?:old |archaic |erhua )?variant of ([^\s|\[;,]+)(?:\|([^\s\[;,]+))?"
)


def _owned_by_entry(senses: list[tuple[str, str]], entry: dict) -> list[tuple[str, str]]:
    """Of a shared sense pool, the senses written on this entry's own CEDICT row.

    CEDICT senses are keyed by the simplified word, so 隻 [zhī] "classifier…" and
    秖 [zhī] "grain that has begun to ripen" sit in one pool. A sense belongs to
    the entry whose definition text contains it as a whole item (items are
    separated by "/" or "; "). Returns the input unchanged when nothing matches,
    so a format surprise can only fail to split, never hide a definition.
    """
    english = entry.get("english_normalized") or entry.get("english") or ""
    if not english:
        return senses
    text = normalize_english(_strip_pron_notes(english)).replace("CL:", "Classifier:")
    haystacks = [f"{sep}{text}{sep}" for sep in ("/", "; ")]
    owned = [
        value
        for value in senses
        if any(
            f"{sep}{value[1]}{sep}" in hay
            for sep, hay in zip(("/", "; "), haystacks)
        )
    ]
    return owned or senses


def _drop_self_references(
    values: list[tuple[str, str]], own_words: set[str]
) -> list[tuple[str, str]]:
    """Remove "variant of <this very word>" senses from a headword's own block.

    CEDICT stores 只's redirect row ("variant of 隻|只[zhi1]") and 祇's ("variant
    of 只[zhi3]") as senses of the shared simplified word 只. Shown on 隻 or 只
    itself, they read as the word being a variant of itself; the variants are
    already listed under "Other forms". A reading whose senses would all vanish
    keeps them (a real variant row, viewed on its own page, has nothing else).
    """

    def points_at_self(text: str) -> bool:
        match = _XREF_TARGET_RE.match((text or "").strip())
        return bool(match) and any(g in own_words for g in match.groups() if g)

    droppable = [points_at_self(text) for _reading, text in values]
    kept_readings = {
        reading_key(reading)
        for (reading, _text), drop in zip(values, droppable)
        if not drop
    }
    return [
        value
        for value, drop in zip(values, droppable)
        if not drop or reading_key(value[0]) not in kept_readings
    ]


def is_xref_sense(text: str | None) -> bool:
    """True for a CEDICT "variant of X" style sense (see `_XREF_PREFIXES`)."""
    return (text or "").strip().lower().startswith(_XREF_PREFIXES)


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


def script_variants(words: Iterable[str]) -> set[str]:
    """`words` plus their OpenCC simplified and traditional spellings.

    Used when one list must answer both scripts: the MOE dictionaries store
    縮頭縮腦, but jieba/token input usually arrives as 缩头缩脑. Converting the
    whole batch in one call matters: OpenCC per headword on the full dictionary
    is far slower than one newline-joined conversion.
    """
    variants = {word for word in words if word}
    if not variants:
        return variants
    convertible = [
        word for word in variants if "\n" not in word and "\r" not in word
    ]
    if convertible:
        joined = "\n".join(convertible)
        for config in ("t2s", "s2t"):
            converted = _opencc(config).convert(joined).split("\n")
            if len(converted) == len(convertible):
                variants.update(converted)
            else:
                # A line break inside a headword would shift the batch result;
                # convert those few one by one instead of losing alignment.
                variants.update(_opencc(config).convert(word) for word in convertible)
    for word in variants - set(convertible):
        for config in ("t2s", "s2t"):
            variants.add(_opencc(config).convert(word))
    return variants


# Every database lives in <project root>/data. Tests and optimisation runs can
# point every reader at a small generated set instead with YABAI_DATA_DIR (see
# scripts/build_test_dbs.py), so they do not need the full ~1.4 GB of sources.
DATA_DIR = Path(
    os.environ.get("YABAI_DATA_DIR")
    or Path(__file__).resolve().parents[1] / "data"
)

# Generated indexes and derived databases live in a subfolder so the raw
# dictionaries stay easy to find. YABAI_DATA_DIR moves the whole tree.
GENERATED_DIR = DATA_DIR / "generated-indexes-optimizations"

# Path to the generated CEDICT database: <project root>/data/cedict.sqlite.
DB_PATH = DATA_DIR / "cedict.sqlite"

# Path to the concise MOE dictionary (Taiwan readings), built by
# scripts/import_moe.py.
MOE_DB_PATH = DATA_DIR / "moe-taiwan" / "moe.sqlite"

# Path to the comprehensive MOE dictionary (重編國語辭典修訂本), built by
# scripts/import_moe.py --kind revised.
MOE_REVISED_DB_PATH = DATA_DIR / "moe-taiwan" / "moe_revised.sqlite"

# Path to the prefilled Taiwan readings of every dictionary, built by
# scripts/build_taiwan_readings.py. Rebuild it whenever a source database, an
# override in overrides.py or the Taiwan-reading logic below changes (AGENTS.md).
TAIWAN_DB_PATH = GENERATED_DIR / "taiwan_readings.sqlite"

# Path to the Wiktionary headword list (kaikki.org/Wiktextract), built by
# scripts/import_wiktionary.py.
WIKTIONARY_DB_PATH = DATA_DIR / "wiktionary.sqlite"
SOURCE_GLOSS_DB_PATH = GENERATED_DIR / "source_gloss.sqlite"
SENSE_COUNTS_DB_PATH = GENERATED_DIR / "sense_counts.sqlite"
_source_gloss_ok_cache: bool | None = None
HEAD_GRAMS_DB_PATH = GENERATED_DIR / "head_grams.sqlite"
_head_grams_ok_cache: bool | None = None

# Path to the Chinese Wiktionary (zh.wiktionary.org) database, built by
# scripts/import_zhwiktionary.py.
ZHWIKTIONARY_DB_PATH = DATA_DIR / "zhwiktionary.sqlite"

# Japanese definitions from the Japanese sections of the same dump, built by
# scripts/import_jpwiktionary.py.
JPWIKTIONARY_DB_PATH = DATA_DIR / "jpwiktionary.sqlite"

# Path to the Baidu pinyin crawler headwords (300k, CC-CEDICT-unlicensed
# scrape), built by scripts/import_baidu.py. Contributes a source chip only.
BAIDU_DB_PATH = DATA_DIR / "baidu.sqlite"

# Path to the SUBTLEX-CH word frequencies (Cai & Brysbaert, 2010), built by
# scripts/import_subtlex.py.
SUBTLEX_DB_PATH = DATA_DIR / "subtlex.sqlite"

# Temporary frequency source built from the FineWeb2 combined list. The old
# SUBTLEX rank column stays in the database, so setting
# YABAI_FREQ_SOURCE=subtlex restores it without rebuilding anything.
CORPUS_FREQ_DB_PATH = DATA_DIR / "corpus_freq.sqlite"
FREQ_SOURCE = os.environ.get("YABAI_FREQ_SOURCE", "corpus").strip().lower()
if FREQ_SOURCE not in {"corpus", "subtlex"}:
    FREQ_SOURCE = "corpus"
FREQ_DB_PATH = CORPUS_FREQ_DB_PATH if FREQ_SOURCE == "corpus" else SUBTLEX_DB_PATH
FREQ_RANK_COLUMN = "corpus_freq_rank" if FREQ_SOURCE == "corpus" else "freq_rank"
FREQ_LABEL = "FineWeb2 corpus (temporary)" if FREQ_SOURCE == "corpus" else "SUBTLEX-CH"
FREQ_ANCHOR = "fineweb2-frequency" if FREQ_SOURCE == "corpus" else "subtlex"

# Path to the HSK vocabulary levels, built by scripts/import_hsk.py.
HSK_DB_PATH = DATA_DIR / "hsk.sqlite"

# Path to the definition word list (scripts/import_english_words.py): the whole
# words that appear in any dictionary's definitions, used for the English-search
# hint.
ENGLISH_WORDS_DB_PATH = GENERATED_DIR / "english_words.sqlite"

# Path to the definition gram index (scripts/import_definition_grams.py): Han
# unigrams/bigrams used to skip definition scans that cannot match.
DEFINITION_GRAMS_DB_PATH = GENERATED_DIR / "definition_grams.sqlite"

# Path to the Han unigram/bigram inverted index (scripts/import_gram_words.py):
# turns 1-2 character CJK queries into one lookup instead of a table scan.
GRAM_WORDS_DB_PATH = GENERATED_DIR / "gram_words.sqlite"
_gram_words_ok_cache: bool | None = None

# Path to the prebuilt search index (scripts/build_search_index.py): the "*"
# headword list and the source pinyin rows, so a small host does not rebuild
# them (and does not call OpenCC hundreds of thousands of times) at startup.
# It is derived data, so it lives with the other generated indexes. The old
# top-level data/search_index.sqlite is moved here on first use.
# Set YABAI_SEARCH_INDEX=0 to force the old in-memory path.
SEARCH_INDEX_DB_PATH = GENERATED_DIR / "search_index.sqlite"
LEGACY_SEARCH_INDEX_DB_PATH = DATA_DIR / "search_index.sqlite"
_search_index_ok_cache: bool | None = None
_search_index_migrated = False


def _migrate_search_index() -> None:
    """Move a pre-generated-indexes search index to its new home, once."""
    global _search_index_migrated
    if _search_index_migrated:
        return
    _search_index_migrated = True
    if not LEGACY_SEARCH_INDEX_DB_PATH.exists():
        return
    # Both copies can exist because older builds wrote the top-level file while
    # an experimental build already used the generated folder. The top-level
    # one is the file the previous release actually served, so it wins unless
    # the generated copy is newer.
    try:
        if (
            SEARCH_INDEX_DB_PATH.exists()
            and SEARCH_INDEX_DB_PATH.stat().st_mtime
            >= LEGACY_SEARCH_INDEX_DB_PATH.stat().st_mtime
        ):
            return
        SEARCH_INDEX_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        os.replace(LEGACY_SEARCH_INDEX_DB_PATH, SEARCH_INDEX_DB_PATH)
    except OSError:
        # A read-only data directory just means the live fallback is used.
        pass


# --- exact, uncapped result sets -------------------------------------------
# The owner requires every result list and total to be exact over ALL matches.
# A LIMIT larger than any possible result set is the simplest way to express
# "no limit" while keeping the existing SQL shape. The dictionary has ~125k
# CEDICT rows and ~405k frequency rows, so 1e9 is far beyond any real set.
# YABAI_UNCAPPED=0 restores the previous per-site caps for A/B comparison.
UNBOUNDED = 1_000_000_000


def _cap(legacy: int) -> int:
    """The effective cap: unbounded by default, `legacy` when uncapped is off."""
    return legacy if os.environ.get("YABAI_UNCAPPED") == "0" else UNBOUNDED


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


_PLACEHOLDER_RUN = re.compile(r"\?(?:\s*,\s*\?){1,}")


@lru_cache(maxsize=512)
def _compact_sql(sql: str) -> str:
    """Collapse whitespace and long placeholder runs for the debug log.

    The same few SQL texts repeat for every row of a result set, so memoizing
    this removes most of the string work from the per-statement logging path.
    `?*N` keeps a 300-row batch readable as one line.
    """
    compact = " ".join(sql.split())
    return _PLACEHOLDER_RUN.sub(
        lambda match: f"?*{match.group(0).count('?')}", compact
    )


def _log_sql(sql: str, started: float, rows: int | None) -> None:
    """Append one timed statement to this thread's log (if logging is on)."""
    statements = getattr(_log_state, "statements", None)
    if statements is None:
        return
    duration = (time.perf_counter() - started) * 1000
    count = "?" if rows is None else str(rows)
    statements.append(
        f"{duration:8.2f} ms rows={count} | {_compact_sql(sql)}"
    )


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


# One read-only connection per database, per worker thread. A single shared
# connection cannot be used from several threads at once: concurrent searches
# interleave statements on it and SQLite raises "bad parameter or other API
# misuse". The server reuses its worker threads, so each keeps warm connections.
_connection_local = threading.local()

# Serializes the one-time ATTACH setup in connect().
_connection_lock = threading.Lock()


@lru_cache(maxsize=64)
def _database_key(database: str) -> str:
    """Absolute path of a database, cached: resolve() is a realpath() syscall.

    A search opens the same handful of files dozens of times, and on a tablet
    that path lookup was measurable. Database paths never move during a run.
    """
    return str(Path(database).resolve())


def _connect(database) -> sqlite3.Connection:
    """Return this thread's read-only connection for a database file."""
    key = _database_key(str(database))
    connections = getattr(_connection_local, "connections", None)
    if connections is None:
        connections = {}
        _connection_local.connections = connections
    connection = connections.get(key)
    if connection is not None:
        return connection
    legacy_sqlite = os.environ.get("YABAI_SQLITE_LEGACY") == "1"
    connection = sqlite3.connect(
        f"file:{key}?mode=ro"
        + ("" if legacy_sqlite else "&immutable=1"),
        uri=True,
        check_same_thread=False,
        factory=_PooledConnection,
    )
    connection.execute(
        "PRAGMA mmap_size=268435456" if legacy_sqlite else "PRAGMA mmap_size=1073741824"
    )
    # 16 MB page cache per connection keeps the hot headword/gloss pages
    # without multiplying a large cache across every worker thread.
    connection.execute(
        "PRAGMA cache_size=-8000" if legacy_sqlite else "PRAGMA cache_size=-16000"
    )
    connection.execute("PRAGMA temp_store=MEMORY")
    # Keep the established case-sensitive LIKE behavior. The indexed
    # definition path uses MATCH only to find candidates, then applies the
    # original lower(...) LIKE test as the exact filter.
    connection.execute("PRAGMA case_sensitive_like=ON")
    if pd.ENABLED and not getattr(connection, "_yabai_perf_traced", False):
        pd.count_statements(connection)
        connection._yabai_perf_traced = True
    connections[key] = connection
    return connection


def _attach_database(connection, alias: str, database) -> None:
    """ATTACH *database* as *alias*, replacing one left on a pooled connection.

    Connections are reused per thread, so a previous build can leave the alias
    attached; a plain ATTACH then raises "database ... is already in use".
    """
    attached = {row[1] for row in connection.execute("PRAGMA database_list")}
    if alias in attached:
        connection.execute(f"DETACH DATABASE {alias}")
    connection.execute(f"ATTACH DATABASE ? AS {alias}", (str(database),))


def _fetchall(connection, sql: str, params=(), label: str = ""):
    """fetchall with optional per-query tracing; identical rows either way."""
    if pd.ENABLED:
        return pd.run_sql(connection, sql, params, label)
    return connection.execute(sql, params).fetchall()


# SQLite versions disagree about the maximum number of bound variables (999 on
# older Android builds, 32766 on most desktops). A search can rank thousands of
# rows, so any IN (...) list built from a result set is split into chunks.
_IN_CHUNK_SIZE = 500


def _chunks(values: list[str], size: int | None = None):
    """Yield consecutive slices of `values` small enough for one IN (...) list."""
    size = _IN_CHUNK_SIZE if size is None else size
    for start in range(0, len(values), size):
        yield values[start : start + size]

# Which dictionaries contain a headword - drives the source chips, the
# per-dictionary definitions and the classifiers shown under a result. Each dict:
#   label      display name (chip / definition header)
#   anchor     id of its section on the About page
#   database   its SQLite file
#   sql        returns one column of headwords; the `IN (...)` word list appears
#              "repeat" times, so a query over two columns sets repeat=2
#   all_sql    returns every headword (used by the segmenter when a source has
#              no segment_sql, and by the "*" listing)
#   segment_sql  optional; like all_sql, but returns every script variant the
#              source stores (word/traditional/simplified). The segmenter reads
#              this so a word that search can find is never split apart.
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
# Japanese definitions carry a pinyin reading per sense, so the result can show
# a reading header (e.g. ㄞ / ㄞˋ) above each group.
_JP_SENSES_SQL = (
    "SELECT word, position, text, reading FROM senses WHERE word IN ({ph}) "
    "ORDER BY position"
)
# Variant forms ("see X" entries) - only some sources have a `forms` table. Each
# row maps a variant to its main form.
_FORMS_SQL = (
    "SELECT variant_trad, variant_simp, main_trad, main_simp, pinyin, qualifier, "
    "redirect FROM forms WHERE variant_trad IN ({ph}) OR main_trad IN ({ph})"
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
        "segment_sql": "SELECT traditional FROM entries "
                       "UNION SELECT simplified FROM entries",
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
        # Prefix-only lookup for the fast first response, with the reading.
        "prefix_sql": "SELECT word, word AS traditional, '' AS simplified, "
                      "MAX(pinyin) AS pinyin FROM moe "
                      "WHERE word LIKE ? ESCAPE '\\' GROUP BY word",
        # The trigram senses_fts table also indexes the headword column, so a
        # 3+ character query can use it instead of scanning `moe`.
        "head_fts_sql": "SELECT DISTINCT word FROM senses_fts "
                        "WHERE word LIKE ? ORDER BY word",
        "all_sql": "SELECT DISTINCT word FROM moe WHERE word <> ''",
        "repeat": 1,
        "sense_sql": _SENSES_SQL,
        "cls_sql": _CLASSIFIERS_SQL,
        "pinyin_sql": "SELECT pinyin FROM moe WHERE word = ? AND pinyin <> '' LIMIT 1",
        "pinyin_batch_sql": "SELECT word, pinyin FROM moe "
                            "WHERE word IN ({ph}) AND pinyin <> ''",
        "pinyin_rows_sql": "SELECT word, pinyin FROM moe WHERE pinyin <> ''",
        "count_sql": "SELECT COUNT(*) FROM moe",
    },
    {
        "label": "重編國語辭典修訂本",
        "anchor": "moe_revised",
        "lang": "taiwan",
        "database": MOE_REVISED_DB_PATH,
        "sql": "SELECT DISTINCT word FROM moe WHERE word IN ({ph})",
        "like_sql": "SELECT DISTINCT word FROM moe "
                    "WHERE word LIKE ? ESCAPE '\\' ORDER BY word",
        # Prefix-only lookup for the fast first response, with the reading.
        "prefix_sql": "SELECT word, word AS traditional, '' AS simplified, "
                      "MAX(pinyin) AS pinyin FROM moe "
                      "WHERE word LIKE ? ESCAPE '\\' GROUP BY word",
        # See the concise MOE source above.
        "head_fts_sql": "SELECT DISTINCT word FROM senses_fts "
                        "WHERE word LIKE ? ORDER BY word",
        "all_sql": "SELECT DISTINCT word FROM moe WHERE word <> ''",
        "repeat": 1,
        "sense_sql": _SENSES_SQL,
        "cls_sql": _CLASSIFIERS_SQL,
        "pinyin_sql": "SELECT pinyin FROM moe WHERE word = ? AND pinyin <> '' LIMIT 1",
        "pinyin_batch_sql": "SELECT word, pinyin FROM moe "
                            "WHERE word IN ({ph}) AND pinyin <> ''",
        "pinyin_rows_sql": "SELECT word, pinyin FROM moe WHERE pinyin <> ''",
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
        # Every Mandarin reading, already split into syllables. A 歇后语 has one
        # per half; the display joins them when the headword splits the same way.
        "pinyin_all_batch_sql": "SELECT word, "
                                "COALESCE(NULLIF(pinyin_spaced, ''), pinyin) "
                                "FROM readings WHERE word IN ({ph}) "
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
    {
        "label": "中文维基词典",
        "anchor": "zhwiktionary",
        "lang": "mainland",
        "database": ZHWIKTIONARY_DB_PATH,
        "sql": "SELECT DISTINCT word FROM headwords WHERE word IN ({ph})",
        "like_sql": "SELECT DISTINCT word FROM headwords "
                    "WHERE word LIKE ? ESCAPE '\\' ORDER BY word",
        # Prefix-only lookup for the fast first response.
        "prefix_sql": "SELECT word, traditional, simplified, pinyin FROM headwords "
                      "WHERE word LIKE ? ESCAPE '\\'",
        # The trigram senses_fts table indexes the headword column too.
        "head_fts_sql": "SELECT DISTINCT word FROM senses_fts "
                        "WHERE word LIKE ? ORDER BY word",
        "all_sql": "SELECT DISTINCT word FROM headwords WHERE word <> ''",
        "segment_sql": "SELECT word FROM headwords "
                       "UNION SELECT traditional FROM headwords "
                       "UNION SELECT simplified FROM headwords",
        "repeat": 1,
        "sense_sql": _SENSES_SQL,
        "cls_sql": _CLASSIFIERS_SQL,
        "pinyin_sql": "SELECT pinyin FROM headwords "
                      "WHERE word = ? AND pinyin <> '' LIMIT 1",
        "pinyin_batch_sql": "SELECT word, pinyin FROM headwords "
                            "WHERE word IN ({ph}) AND pinyin <> ''",
        "pinyin_rows_sql": "SELECT word, simplified, pinyin FROM headwords "
                           "WHERE pinyin <> ''",
        "count_sql": "SELECT COUNT(*) FROM headwords",
    },
    {
        "label": "ウィクショナリー",
        "anchor": "jpwiktionary",
        "lang": "japanese",
        "database": JPWIKTIONARY_DB_PATH,
        "sql": "SELECT DISTINCT word FROM headwords WHERE word IN ({ph})",
        "like_sql": "SELECT DISTINCT word FROM headwords "
                    "WHERE word LIKE ? ESCAPE '\\' ORDER BY word",
        # Prefix-only lookup for the fast first response.
        "prefix_sql": "SELECT word, traditional, simplified, pinyin FROM headwords "
                      "WHERE word LIKE ? ESCAPE '\\'",
        # See the Chinese Wiktionary source above.
        "head_fts_sql": "SELECT DISTINCT word FROM senses_fts "
                        "WHERE word LIKE ? ORDER BY word",
        "all_sql": "SELECT DISTINCT word FROM headwords WHERE word <> ''",
        "segment_sql": "SELECT word FROM headwords "
                       "UNION SELECT traditional FROM headwords "
                       "UNION SELECT simplified FROM headwords",
        "repeat": 1,
        "sense_sql": _JP_SENSES_SQL,
        "cls_sql": _CLASSIFIERS_SQL,
        "pinyin_sql": "SELECT pinyin FROM headwords "
                      "WHERE word = ? AND pinyin <> '' LIMIT 1",
        "pinyin_batch_sql": "SELECT word, pinyin FROM headwords "
                            "WHERE word IN ({ph}) AND pinyin <> ''",
        "pinyin_rows_sql": "SELECT word, simplified, pinyin FROM headwords "
                           "WHERE pinyin <> ''",
        "split_pinyin": True,
        "count_sql": "SELECT COUNT(*) FROM headwords",
    },
    {
        "label": "百度汉语",
        "anchor": "baidu",
        "lang": "mainland",
        "database": BAIDU_DB_PATH,
        "sql": "SELECT DISTINCT word FROM headwords WHERE word IN ({ph})",
        "like_sql": "SELECT DISTINCT word FROM headwords "
                    "WHERE word LIKE ? ESCAPE '\\' ORDER BY word",
        "prefix_sql": "SELECT word, word AS traditional, word AS simplified, "
                      "'' AS pinyin FROM headwords WHERE word LIKE ? ESCAPE '\\'",
        "head_fts_sql": "SELECT DISTINCT word FROM headwords "
                        "WHERE word LIKE ? ESCAPE '\\'",
        "all_sql": "SELECT DISTINCT word FROM headwords WHERE word <> ''",
        "repeat": 1,
        "sense_sql": _SENSES_SQL,
        "cls_sql": _CLASSIFIERS_SQL,
        "forms_sql": None,
        "pinyin_sql": "SELECT '' FROM headwords WHERE word = ? LIMIT 1",
        "pinyin_batch_sql": "SELECT word, '' FROM headwords "
                            "WHERE word IN ({ph})",
        "pinyin_rows_sql": "SELECT word, word, '' FROM headwords "
                           "WHERE word <> ''",
        "split_pinyin": False,
        "count_sql": "SELECT COUNT(*) FROM headwords",
    },
]

# --- headword export for the segmenter -------------------------------------
# The segmenter (segment.py) must know every word search can find, otherwise
# "it's 缩头缩脑" splits a word that "缩头缩脑" alone finds. Rather than keep a
# second list of dictionaries there, it asks this registry: each source
# contributes the headwords from its `segment_sql` (when it has one) or else its
# `all_sql`. A new dictionary added to _SOURCE_QUERIES is therefore segmented
# automatically; give it a `segment_sql` only when `all_sql` misses script
# variants it stores in separate columns.


def source_database_paths() -> list[Path]:
    """Database file of every registered dictionary (existing or not)."""
    return [Path(source["database"]) for source in _SOURCE_QUERIES]


def source_headwords(min_length: int = 2) -> set[str]:
    """Every headword of every registered dictionary that exists on disk.

    Both scripts are returned. A source that stores only one form (MOE stores
    縮頭縮腦, Baidu stores 缩头缩脑) still contributes the other through OpenCC,
    so the segmenter never misses a word just because the input uses the other
    script. A missing database, an unreadable file or a failing query is
    skipped, so a half-built data directory still segments with what is there.
    """
    words: set[str] = set()
    for source in _SOURCE_QUERIES:
        sql = source.get("segment_sql") or source.get("all_sql")
        path = Path(source["database"])
        if not sql or not path.exists():
            continue
        try:
            connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            for (value,) in connection.execute(sql):
                if value:
                    words.add(value)
        except sqlite3.Error:
            continue
        finally:
            connection.close()
    return {word for word in script_variants(words) if len(word) >= min_length}

# --- data version ----------------------------------------------------------
def data_version() -> str:
    """A short fingerprint of every data file a search result depends on.

    Built from each file's mtime, so re-running any importer changes the value.
    The frontend sends it as `?dv=` and the server only then allows the API
    response to be cached, which keeps cached results from outliving their data.
    """
    paths = [source["database"] for source in _SOURCE_QUERIES]
    paths.extend(
        [
            TAIWAN_DB_PATH,
            SUBTLEX_DB_PATH,
            FREQ_DB_PATH,
            HSK_DB_PATH,
            ENGLISH_WORDS_DB_PATH,
            DEFINITION_GRAMS_DB_PATH,
            GRAM_WORDS_DB_PATH,
        ]
    )
    parts = []
    for path in paths:
        try:
            stamp = path.stat().st_mtime
        except OSError:
            # A source that is not imported yet does not contribute.
            continue
        parts.append(f"{path.name}:{int(stamp)}")
    digest = hashlib.sha1("|".join(sorted(parts)).encode("utf-8")).hexdigest()
    return digest[:10]


# --- prebuilt search index -------------------------------------------------
# The two expensive startup caches (the "*" headword list and the source pinyin
# rows) are pure functions of the dictionary files. scripts/build_search_index.py
# stores them in data/generated-indexes-optimizations/search_index.sqlite, and
# this module reads them back with plain SQL. On the tablet that replaces ~90 s
# of pure-Python OpenCC work.
_SEARCH_INDEX_VERSION = 1
# The dialect early-out tables have their own version so adding them does not
# invalidate the headword/pinyin index (which would force a 110 s rebuild on the
# tablet just to gain a small lookup table).
_DIALECT_INDEX_VERSION = 1


def _search_index_enabled() -> bool:
    """False when YABAI_SEARCH_INDEX=0 asks for the old in-memory path."""
    return os.environ.get("YABAI_SEARCH_INDEX", "1") != "0"


def _search_index_signature() -> str:
    """Fingerprint the source files the index is built from.

    Uses size + SQLite's change counter (see _file_change_stamp), not mtime, so
    an index built on the laptop is still accepted after being copied to the
    tablet. Rebuilding a source changes the counter.
    """
    paths = [source["database"] for source in _SOURCE_QUERIES]
    paths.append(FREQ_DB_PATH)
    parts = [f"{path.name}={_file_change_stamp(path)}" for path in paths]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _search_index_ok() -> bool:
    """True when the prebuilt search index was built from the current sources."""
    global _search_index_ok_cache
    _migrate_search_index()
    if _search_index_ok_cache is not None:
        return _search_index_ok_cache
    if not _search_index_enabled() or not SEARCH_INDEX_DB_PATH.exists():
        _search_index_ok_cache = False
        return False
    connection = _connect(SEARCH_INDEX_DB_PATH)
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'signature'"
        ).fetchone()
        version = connection.execute(
            "SELECT value FROM meta WHERE key = 'version'"
        ).fetchone()
    except sqlite3.Error:
        row = version = None
    finally:
        connection.close()
    _search_index_ok_cache = (
        bool(row)
        and row[0] == _search_index_signature()
        and bool(version)
        and version[0] == str(_SEARCH_INDEX_VERSION)
    )
    return _search_index_ok_cache


def search_index_status() -> dict:
    """Report the prebuilt index state for /__debug/status and startup logs."""
    ready = _search_index_ok()
    return {
        "active": ready,
        "path": str(SEARCH_INDEX_DB_PATH),
        "reason": "" if ready else "missing or stale",
    }


def _load_headwords_from_index() -> tuple[list[str], list[int]] | None:
    """Load the sorted "*" headword list, or None when the index is unusable."""
    if not _search_index_ok():
        return None
    connection = _connect(SEARCH_INDEX_DB_PATH)
    try:
        rows = connection.execute(
            "SELECT word, grp FROM headwords ORDER BY position"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if not rows:
        return None
    return [row[0] for row in rows], [row[1] for row in rows]


def _source_pinyin_index_rows(
    prefix: str = "", like: str | None = None
) -> list[tuple] | None:
    """Source pinyin rows from the prebuilt index, or None when unavailable.

    Rows are (word, simplified, marked, toneless, numeric), the same tuple shape
    as _source_pinyin_rows(). `like` is a raw SQL LIKE pattern for a wildcard or
    "contains" match; otherwise `prefix` selects an index range.
    """
    if not _search_index_ok():
        return None
    connection = _connect(SEARCH_INDEX_DB_PATH)
    try:
        if like is not None:
            rows = connection.execute(
                "SELECT word, simplified, marked, toneless, numeric "
                "FROM pinyin WHERE toneless LIKE ? ESCAPE '\\' "
                "ORDER BY toneless, id",
                (like,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT word, simplified, marked, toneless, numeric "
                "FROM pinyin WHERE toneless >= ? AND toneless < ? "
                "ORDER BY toneless, id",
                (prefix, prefix + "\uffff"),
            ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return [tuple(row) for row in rows]


def _dialect_index_rows() -> tuple[list[str], list[str]]:
    """Distinct dialect labels and toneless readings for the early-out tables."""
    if not WIKTIONARY_DB_PATH.exists():
        return [], []
    connection = _connect(WIKTIONARY_DB_PATH)
    try:
        labels = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT lower(label) FROM readings "
                "WHERE kind <> 'mandarin' AND label <> ''"
            )
        ]
        pinyin = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT pinyin_toneless FROM readings "
                "WHERE kind <> 'mandarin'"
            )
        ]
    except sqlite3.Error:
        return [], []
    finally:
        connection.close()
    return labels, pinyin


def build_search_index(path: Path | None = None) -> dict[str, int]:
    """Write the prebuilt search index from the current source databases.

    Called by scripts/build_search_index.py. The live builders are the same
    functions a request would use, so the file can never disagree with them.
    The write is atomic: a temporary file is renamed over the old index.
    """
    if path is None:
        _migrate_search_index()
    target = Path(path) if path is not None else SEARCH_INDEX_DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    if partial.exists():
        partial.unlink()

    # Build from source even when a valid index already exists.
    global _all_headwords_cache
    _all_headwords_cache = None
    heads = _build_all_headwords_list()
    groups = _all_headwords_cache[1] if _all_headwords_cache else []
    pinyin_rows = _source_pinyin_rows()
    dialect_labels, dialect_pinyin = _dialect_index_rows()
    signature = _search_index_signature()

    connection = sqlite3.connect(partial)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE headwords ("
            "position INTEGER PRIMARY KEY, word TEXT NOT NULL, grp INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE pinyin ("
            "id INTEGER PRIMARY KEY, toneless TEXT NOT NULL, "
            "word TEXT NOT NULL, simplified TEXT NOT NULL, "
            "marked TEXT NOT NULL, numeric TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE dialect_labels (label TEXT PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE dialect_pinyin (toneless TEXT PRIMARY KEY)"
        )
        connection.executemany(
            "INSERT INTO headwords (position, word, grp) VALUES (?, ?, ?)",
            ((position, word, groups[position]) for position, word in enumerate(heads)),
        )
        # pinyin_rows is (word, simplified, marked, toneless, numeric); keep the
        # INSERT columns in that order. Only the SELECT order is rearranged.
        connection.executemany(
            "INSERT INTO pinyin "
            "(word, simplified, marked, toneless, numeric) VALUES (?, ?, ?, ?, ?)",
            pinyin_rows,
        )
        connection.executemany(
            "INSERT INTO dialect_labels (label) VALUES (?)",
            ((label,) for label in dialect_labels),
        )
        connection.executemany(
            "INSERT INTO dialect_pinyin (toneless) VALUES (?)",
            ((toneless,) for toneless in dialect_pinyin),
        )
        connection.execute("CREATE INDEX pinyin_toneless ON pinyin (toneless, id)")
        connection.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            (
                ("version", str(_SEARCH_INDEX_VERSION)),
                ("signature", signature),
                ("headwords", str(len(heads))),
                ("pinyin", str(len(pinyin_rows))),
                ("dialect_version", str(_DIALECT_INDEX_VERSION)),
                ("dialect_labels", str(len(dialect_labels))),
                ("dialect_pinyin", str(len(dialect_pinyin))),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    partial.replace(target)
    return {
        "headwords": len(heads),
        "pinyin": len(pinyin_rows),
        "dialect_labels": len(dialect_labels),
        "dialect_pinyin": len(dialect_pinyin),
    }


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
    """Info about the configured frequency list, or None when unavailable."""
    if not FREQ_DB_PATH.exists():
        return None
    updated = None
    entries = None
    try:
        connection = _connect(FREQ_DB_PATH)
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
    return {
        "label": FREQ_LABEL,
        "anchor": FREQ_ANCHOR,
        "updated": updated,
        "entries": entries,
    }


def hsk_source() -> dict | None:
    """Info about the HSK vocabulary list (or None if it isn't imported)."""
    if not HSK_DB_PATH.exists():
        return None
    updated = None
    entries = None
    try:
        connection = _connect(HSK_DB_PATH)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'updated'").fetchone()
        updated = row[0] if row else None
        entries = connection.execute("SELECT COUNT(*) FROM hsk").fetchone()[0]
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    return {"label": "HSK Vocabulary", "anchor": "hsk", "updated": updated, "entries": entries}


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
    """The pooled CEDICT connection, with MOE and the frequency list attached.

    Rows behave like dicts; the MOE database is attached as "moe" (when
    present) so a query can also match Taiwan readings. The configured
    frequency database is attached as "subtlex" for compatibility with the
    existing queries.
    """
    connection = _connect(DB_PATH)
    # Row factory lets us read columns by name, e.g. row["pinyin"].
    connection.row_factory = sqlite3.Row
    # Attach once per (thread-local) connection. Every call used to take the
    # global lock and run PRAGMA database_list, which serialised all threads.
    if not getattr(connection, "_yabai_attached", False):
        with _connection_lock:
            attached = {row[1] for row in connection.execute("PRAGMA database_list")}
            if "moe" not in attached and MOE_DB_PATH.exists():
                connection.execute("ATTACH DATABASE ? AS moe", (str(MOE_DB_PATH),))
            if "subtlex" not in attached and FREQ_DB_PATH.exists():
                connection.execute("ATTACH DATABASE ? AS subtlex", (str(FREQ_DB_PATH),))
            if "tw" not in attached and TAIWAN_DB_PATH.exists():
                connection.execute("ATTACH DATABASE ? AS tw", (str(TAIWAN_DB_PATH),))
        connection._yabai_attached = True
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


_FULLWIDTH_WILDCARDS = str.maketrans("＊？", "*?")


def _normalize_wildcards(text: str) -> str:
    """Turn full-width '＊'/'？' into their ASCII counterparts."""
    return (text or "").translate(_FULLWIDTH_WILDCARDS)


def _like_pattern(text: str) -> str:
    """Turn a user query into a SQL LIKE pattern.

    '*' is the wildcard and becomes '%', which matches zero or more characters,
    so "*a*" also matches just "a". '?' becomes '_', which matches exactly one
    character (one Han character or one letter). The full-width forms '＊' and
    '？' (common in Chinese/Japanese input) are treated the same way. Literal
    '%', '_' and '\\' are escaped so they match themselves rather than acting
    as wildcards.
    """
    text = _normalize_wildcards(text)
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.replace("*", "%").replace("?", "_")


def _pinyin_wildcard_pattern(text: str) -> str:
    text = _normalize_wildcards(text)
    """A broad SQL LIKE pattern for a pinyin wildcard query.

    '*' and '?' both become '%' here; '?' is narrowed to exactly one syllable
    afterwards by _pinyin_wildcard_regex(). Keeping the literal pieces in the
    SQL pattern limits how many readings Python has to inspect.
    """
    pieces = []
    for piece in re.split(r"([*?＊？])", text):
        if piece in ("*", "?", "＊", "？"):
            pieces.append("%")
        else:
            pieces.append(_like_pattern(to_toneless(piece)))
    return "".join(pieces)


def _pinyin_wildcard_regex(text: str) -> re.Pattern[str] | None:
    text = _normalize_wildcards(text)
    """Regex for a pinyin wildcard query, with '?' matching one whole syllable.

    The entry key is its syllables joined by NUL bytes. A '?' consumes one
    syllable plus the separator after it, and must start at a syllable boundary
    (start of the key or just after a separator), so it cannot match a partial
    syllable or several syllables. '*' keeps its existing zero-or-more-
    characters meaning. Returns None when the query has no letters to match.
    """
    if not to_toneless(text.replace("*", "").replace("?", "")):
        return None
    pieces = []
    tokens = re.split(r"([*?＊？])", text)
    for index, piece in enumerate(tokens):
        if piece in ("*", "＊"):
            pieces.append(".*")
        elif piece in ("?", "？"):
            # A literal chunk before '?' already ends with a required separator,
            # so the lookbehind is enough to keep '?' on a syllable boundary.
            pieces.append(r"(?:\A|(?<=\x00))[a-zü]+\x00")
        else:
            letters = to_toneless(piece)
            if not letters:
                continue
            # A syllable boundary may appear between any two query letters, so
            # "guojia" matches guo + jia without requiring an apostrophe.
            literal = r"\x00?".join(re.escape(char) for char in letters)
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if following == "?":
                # '?' starts a new syllable, so the literal must end at a
                # boundary rather than in the middle of the next syllable.
                literal += r"\x00"
            elif following is None:
                # Let a final complete syllable consume its trailing separator;
                # a partial final syllable simply leaves it unconsumed.
                literal += r"\x00?"
            pieces.append(literal)
    return re.compile("".join(pieces))


def _moe_db_paths() -> list[Path]:
    """The MOE databases that are present, concise first then comprehensive."""
    return [
        path
        for path in (MOE_DB_PATH, MOE_REVISED_DB_PATH)
        if path.exists()
    ]


def _moe_readings(words: list[str]) -> dict[str, list[str]]:
    """Return {word: [Taiwan pinyin, ...]} from both MOE databases."""
    paths = _moe_db_paths()
    if not words or not paths:
        return {}
    placeholders = ",".join("?" * len(words))
    by_word: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        connection = _connect(path)
        try:
            rows = connection.execute(
                f"SELECT DISTINCT word, pinyin FROM moe "
                f"WHERE word IN ({placeholders}) AND pinyin <> ''",
                words,
            ).fetchall()
        finally:
            connection.close()
        for word, pinyin in rows:
            if pinyin not in by_word[word]:
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
    """Return {character: [Taiwan readings]} from both MOE databases."""
    global _moe_char_cache
    if _moe_char_cache is None:
        mapping: dict[str, list[str]] = defaultdict(list)
        for path in _moe_db_paths():
            connection = _connect(path)
            try:
                rows = connection.execute(
                    "SELECT word, pinyin FROM moe "
                    "WHERE length(word) = 1 AND pinyin <> ''"
                ).fetchall()
            finally:
                connection.close()
            for word, pinyin in rows:
                if pinyin not in mapping[word]:
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
        for path in _moe_db_paths():
            connection = _connect(path)
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
        for path in _moe_db_paths():
            connection = _connect(path)
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


def _entry_taiwan_reading(
    traditional: str,
    simplified: str,
    pinyin: str,
    english_normalized: str,
    options: list[str],
    counts: dict[str, int],
    char_map: dict,
) -> str | None:
    """The Taiwan reading of one headword row, or None when it has none.

    The single place where the rules are combined, used by the build script (to
    prefill taiwan_readings.sqlite) and by _decorate (when a row is not in that
    table): a manual override in overrides.py wins; then the whole-word MOE
    reading (see _taiwan_reading); then CEDICT's own "Taiwan pr." note; then the
    per-character derivation.
    """
    override = TAIWAN_READINGS.get(traditional) or TAIWAN_READINGS.get(simplified)
    if override:
        return override
    if options:
        return _taiwan_reading(traditional, pinyin, options, counts, char_map)
    return _cedict_taiwan_reading(english_normalized or "") or _taiwan_reading(
        traditional, pinyin, options, counts, char_map
    )


# --- prefilled Taiwan readings (taiwan_readings.sqlite) --------------------
_taiwan_ready_cache: bool | None = None
_taiwan_trigram_cache: bool | None = None


def _taiwan_ready() -> bool:
    """True when taiwan_readings.sqlite exists and has its `readings` table."""
    global _taiwan_ready_cache
    if _taiwan_ready_cache is None:
        ready = False
        if TAIWAN_DB_PATH.exists():
            connection = _connect(TAIWAN_DB_PATH)
            try:
                ready = (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='readings'"
                    ).fetchone()
                    is not None
                )
            except sqlite3.Error:
                ready = False
            finally:
                connection.close()
        _taiwan_ready_cache = ready
    return _taiwan_ready_cache


def _taiwan_has_trigram() -> bool:
    """True when the Taiwan table has its trigram index (for "contains")."""
    global _taiwan_trigram_cache
    if _taiwan_trigram_cache is None:
        found = False
        if _taiwan_ready():
            connection = _connect(TAIWAN_DB_PATH)
            try:
                found = (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='readings_fts'"
                    ).fetchone()
                    is not None
                )
            finally:
                connection.close()
        _taiwan_trigram_cache = found
    return _taiwan_trigram_cache


# FROM clause that gives every CEDICT row its prefilled Taiwan reading. The
# columns are prefixed tw_ so they never clash with those of `entries`.
_TW_CEDICT_FROM = (
    "entries JOIN tw.readings ON tw_source = 'cedict' "
    "AND tw_word = entries.traditional AND tw_mainland = entries.pinyin"
)


def _taiwan_lookup(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], str | None]:
    """Prefilled Taiwan readings for (headword, mainland reading) pairs.

    Returns {pair: Taiwan reading}, with None when the Taiwan reading equals the
    mainland one. A pair that is missing from the result is not in the table.
    """
    if not pairs or not _taiwan_ready():
        return {}
    wanted = set(pairs)
    words = sorted({word for word, _reading in wanted})
    found: dict[tuple[str, str], str | None] = {}
    connection = _connect(TAIWAN_DB_PATH)
    try:
        for start in range(0, len(words), 400):
            chunk = words[start : start + 400]
            holes = ",".join("?" * len(chunk))
            for word, mainland, reading, changed in connection.execute(
                "SELECT tw_word, tw_mainland, tw_reading, tw_changed "
                f"FROM readings WHERE tw_word IN ({holes})",
                chunk,
            ):
                key = (word, mainland)
                if key in wanted and key not in found:
                    found[key] = reading if changed else None
    finally:
        connection.close()
    return found


# Everything the prefilled table is derived from. The build script stores this
# fingerprint; startup compares it so a stale table is reported, not trusted.
_TAIWAN_LOGIC = (
    "_word_taiwan",
    "_char_taiwan",
    "_derive_taiwan",
    "_taiwan_reading",
    "_entry_taiwan_reading",
    "_cedict_taiwan_reading",
    "_reading_counts",
)


def _file_change_stamp(path: Path) -> str:
    """size + SQLite's file-change counter: survives a copy, changes on a write."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            header = handle.read(28)
        counter = int.from_bytes(header[24:28], "big") if len(header) == 28 else 0
    except OSError:
        return "missing"
    return f"{size}:{counter}"


def taiwan_signature() -> str:
    """Fingerprint of the sources, overrides and logic the Taiwan table uses."""
    import inspect
    import json as _json

    from yabai_search import pinyin as _pinyin_module

    parts: list[str] = []
    module = sys.modules[__name__]
    for name in _TAIWAN_LOGIC:
        try:
            parts.append(inspect.getsource(getattr(module, name)))
        except (OSError, TypeError):
            parts.append(name)
    try:
        parts.append(inspect.getsource(_pinyin_module))
    except (OSError, TypeError):
        parts.append("pinyin")
    parts.append(_TAIWAN_PR_RE.pattern)
    parts.append(_json.dumps(sorted(_TAIWAN_EXCEPTIONS), ensure_ascii=False))
    parts.append(_json.dumps(TAIWAN_READINGS, ensure_ascii=False, sort_keys=True))
    for source in _SOURCE_QUERIES:
        parts.append(f"{source['anchor']}={_file_change_stamp(source['database'])}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def taiwan_table_status() -> str:
    """"ok", "missing" or "stale" for taiwan_readings.sqlite."""
    if not _taiwan_ready():
        return "missing"
    connection = _connect(TAIWAN_DB_PATH)
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'signature'"
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        connection.close()
    return "ok" if row and row[0] == taiwan_signature() else "stale"


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
    """Attach each entry's configured frequency (rank / count / per-million).

    Matched by the simplified form, then the traditional form; None when the
    word isn't in the current frequency list.
    """
    words = sorted(
        {entry["simplified"] for entry in entries}
        | {entry["traditional"] for entry in entries}
    )
    for entry in entries:
        entry["freq"] = None
    if not words or not FREQ_DB_PATH.exists():
        return entries
    by_word: dict[str, dict] = {}
    connection = _connect(FREQ_DB_PATH)
    try:
        for chunk in _chunks(words):
            placeholders = ",".join("?" * len(chunk))
            rows = connection.execute(
                "SELECT word, rank, count, per_million FROM freq "
                f"WHERE word IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for word, rank, count, per_million in rows:
                by_word[word] = {
                    "rank": rank,
                    "count": count,
                    "per_million": per_million,
                }
    finally:
        connection.close()
    for entry in entries:
        entry["freq"] = by_word.get(entry["simplified"]) or by_word.get(entry["traditional"])
    return entries


def _attach_hsk(entries: list[dict]) -> list[dict]:
    """Attach the latest HSK levels (3.0 and the 2026 syllabus).

    Matched by the simplified form, then the traditional form. Each value is a
    level number (7 means HSK 7-9). HSK 2.0 is not exposed: words only in the
    old list get no HSK level.
    """
    words = sorted(
        {entry["simplified"] for entry in entries}
        | {entry["traditional"] for entry in entries}
    )
    for entry in entries:
        entry["hsk"] = None
    if not words or not HSK_DB_PATH.exists():
        return entries
    placeholders = ",".join("?" * len(words))
    connection = _connect(HSK_DB_PATH)
    try:
        rows = connection.execute(
            "SELECT simplified, traditional, new_level, newest_level "
            f"FROM hsk WHERE simplified IN ({placeholders}) "
            f"OR traditional IN ({placeholders})",
            tuple(words) * 2,
        ).fetchall()
    finally:
        connection.close()
    by_word: dict[str, dict] = {}
    for simplified, traditional, new, newest in rows:
        if new is None and newest is None:
            continue  # HSK 2.0-only word: no current level to show
        value = {"new": new, "newest": newest}
        by_word.setdefault(simplified, value)
        if traditional:
            by_word.setdefault(traditional, value)
    for entry in entries:
        entry["hsk"] = by_word.get(entry["simplified"]) or by_word.get(entry["traditional"])
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


def _variant_form_rows(words: list[str]) -> list[tuple]:
    """Variant-form rows ("see X" links) for `words`, across every source.

    Only some sources have a `forms` table (currently CC-CEDICT). Kept separate
    from `_attach_definitions` so lazy hydration can attach just the variant
    redirect (`main`) without fetching definitions.
    """
    if not words:
        return []
    rows: list[tuple] = []
    for source in _SOURCE_QUERIES:
        if not source.get("forms_sql") or not source["database"].exists():
            continue
        connection = _connect(source["database"])
        try:
            # The forms SQL names the word list twice (variant and main column).
            for chunk in _chunks(words):
                placeholders = ",".join("?" * len(chunk))
                rows.extend(
                    connection.execute(
                        source["forms_sql"].format(ph=placeholders),
                        tuple(chunk) * 2,
                    ).fetchall()
                )
        finally:
            connection.close()
    return rows


class _VariantIndex:
    """Variant-form rows, indexed per (traditional form, reading).

    CEDICT variant rows are per reading: 只 is both "variant of 隻 [zhī]" and
    the real word 只 [zhǐ] "only", and 祇 is both "variant of 只 [zhǐ]" and the
    unrelated word 祇 [qí] "earth spirit". Indexing by the traditional form alone
    made the redirect for one reading swallow every other reading of that form.

    A form row whose reading is blank applies to every reading of its form.
    """

    def __init__(self, form_rows_all: list[tuple]) -> None:
        rows: dict[tuple[str, str, str], tuple] = {}
        for row in form_rows_all:
            variant_trad, _vs, main_trad, _ms, pinyin, _qualifier, redirect = row
            key = (main_trad, variant_trad, reading_key(pinyin))
            current = rows.get(key)
            if current is None or (redirect and not current[6]):
                rows[key] = row
        self._main: dict[tuple[str, str], str] = {}
        self._forms: dict[tuple[str, str], list[dict]] = {}
        for (main_trad, variant_trad, reading), row in rows.items():
            _vt, variant_simp, _mt, _ms, pinyin, qualifier, redirect = row
            if redirect and main_trad != variant_trad:
                self._main.setdefault((variant_trad, reading), main_trad)
            self._forms.setdefault((main_trad, reading), []).append(
                {
                    "traditional": variant_trad,
                    "simplified": variant_simp or variant_trad,
                    "pinyin": pinyin or "",
                    "qualifier": qualifier or "",
                }
            )
        self._variant_readings: dict[str, set[str]] = {}
        for variant_trad, reading in self._main:
            self._variant_readings.setdefault(variant_trad, set()).add(reading)

    def main_for(self, traditional: str, reading: str) -> str | None:
        """The form this (traditional, reading) is a variant of, if any."""
        reading = reading_key(reading)
        if reading:
            return self._main.get((traditional, reading)) or self._main.get(
                (traditional, "")
            )
        for known in sorted(self._variant_readings.get(traditional, ())):
            return self._main[(traditional, known)]
        return None

    def is_redirect(self, traditional: str, reading: str) -> bool:
        return self.main_for(traditional, reading) is not None

    def forms_for(self, traditional: str, reading: str) -> list[dict]:
        """Variants listed under this (traditional, reading), de-duplicated."""
        reading = reading_key(reading)
        keys = (
            [(traditional, reading), (traditional, "")]
            if reading
            else [key for key in self._forms if key[0] == traditional]
        )
        found: list[dict] = []
        seen: set[str] = set()
        for key in keys:
            for form in self._forms.get(key, ()):
                if form["traditional"] not in seen:
                    seen.add(form["traditional"])
                    found.append(form)
        return found


def _set_variant_fields(entries: list[dict], form_rows_all: list[tuple]) -> list[dict]:
    """Set entry["main"] and entry["forms"] from variant-form rows.

    `main` is the redirect target when this entry is a "see X" / "variant of X"
    entry pointing at a *different* form (self-references like 血[xiě] ->
    血[xuè] are ignored). `forms` lists the variants that point at this entry,
    shown as "Other forms". Matching is by traditional form AND reading (see
    `_VariantIndex`): the traditional form stays distinct even when simplified
    collide (軟 vs 輭, both simplified 软), and the reading keeps one reading's
    redirect from hiding the form's other readings (只 zhǐ vs 只 zhī).
    `redirect` marks a real cross-reference; "also written X" rows are
    display-only and never set `main`.
    """
    index = _VariantIndex(form_rows_all)
    for entry in entries:
        trad = entry["traditional"]
        reading = entry.get("pinyin") or ""
        entry["main"] = index.main_for(trad, reading)
        if trad in HIDE_FORMS or entry["simplified"] in HIDE_FORMS:
            entry["forms"] = []
        else:
            entry["forms"] = index.forms_for(trad, reading)
    return entries


_SENSE_COUNTS_VERSION = 3
_sense_counts_ok_cache: bool | None = None


def _sense_counts_ok() -> bool:
    """True when sense_counts.sqlite was built from the current CEDICT file."""
    global _sense_counts_ok_cache
    if _sense_counts_ok_cache is not None:
        return _sense_counts_ok_cache
    if os.environ.get("YABAI_SENSE_COUNTS", "1") == "0":
        _sense_counts_ok_cache = False
        return False
    if not SENSE_COUNTS_DB_PATH.exists():
        _sense_counts_ok_cache = False
        return False
    connection = _connect(SENSE_COUNTS_DB_PATH)
    try:
        version = connection.execute(
            "SELECT value FROM meta WHERE key = 'version'"
        ).fetchone()
        signature = connection.execute(
            "SELECT value FROM meta WHERE key = 'signature'"
        ).fetchone()
    except sqlite3.Error:
        version = signature = None
    finally:
        connection.close()
    _sense_counts_ok_cache = (
        bool(version)
        and version[0] == str(_SENSE_COUNTS_VERSION)
        and bool(signature)
        and signature[0] == _file_change_stamp(DB_PATH)
    )
    return _sense_counts_ok_cache


def build_sense_counts(
    path: Path | None = None, cedict_path: Path | None = None
) -> dict[str, int]:
    """Write sense_counts.sqlite from the current CEDICT senses table.

    Stores both counts the app needs: the filtered count used by
    `_cedict_reading_counts` (blank and cross-reference rows excluded) and the
    raw count used by lookup's ORDER BY (only cross-references excluded).
    """
    target = Path(path) if path is not None else SENSE_COUNTS_DB_PATH
    source = Path(cedict_path) if cedict_path is not None else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    if partial.exists():
        partial.unlink()

    connection = _connect(source)
    try:
        rows = connection.execute(
            "SELECT word, reading, "
            "SUM(CASE WHEN text IS NOT NULL AND TRIM(text) <> '' "
            f"AND {_XREF_SQL} THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN {_XREF_SQL} THEN 1 ELSE 0 END) "
            "FROM senses GROUP BY word, reading"
        ).fetchall()
    finally:
        connection.close()

    stored = [
        (word, reading or "", min(count, _MAX_DEFS), raw)
        for word, reading, count, raw in rows
        if count or raw
    ]

    writer = sqlite3.connect(partial)
    try:
        writer.execute("PRAGMA journal_mode=OFF")
        writer.execute("PRAGMA synchronous=OFF")
        writer.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        writer.execute(
            "CREATE TABLE senses ("
            "word TEXT NOT NULL, reading TEXT NOT NULL, "
            "count INTEGER NOT NULL, raw_count INTEGER NOT NULL, "
            "PRIMARY KEY (word, reading))"
        )
        writer.executemany(
            "INSERT INTO senses (word, reading, count, raw_count) "
            "VALUES (?, ?, ?, ?)",
            stored,
        )
        writer.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            (
                ("version", str(_SENSE_COUNTS_VERSION)),
                ("signature", _file_change_stamp(source)),
                ("rows", str(len(stored))),
            ),
        )
        writer.commit()
    finally:
        writer.close()
    partial.replace(target)
    return {"rows": len(stored)}


def _raw_sense_counts(words: list[str]) -> dict[tuple[str, str], int]:
    """Raw lookup-order counts for these simplified words, from the small DB."""
    if not words or not _sense_counts_ok():
        return {}
    counts: dict[tuple[str, str], int] = {}
    connection = _connect(SENSE_COUNTS_DB_PATH)
    try:
        for chunk in _chunks(sorted(set(words))):
            placeholders = ",".join("?" * len(chunk))
            for word, reading, raw in connection.execute(
                "SELECT word, reading, raw_count FROM senses "
                f"WHERE word IN ({placeholders})",
                tuple(chunk),
            ):
                counts[(word, reading or "")] = raw
    finally:
        connection.close()
    return counts


def _cedict_reading_counts(words: list[str]) -> dict[str, dict[str, int]]:
    """Count CEDICT senses per (headword, reading), capped like the definitions.

    Mirrors the grouping in `_attach_definitions` so `_reading_count` (main.py)
    can pick the same representative reading without fetching the definitions.
    """
    if not words:
        return {}
    counts: dict[str, dict[str, int]] = {}
    if _sense_counts_ok():
        connection = _connect(SENSE_COUNTS_DB_PATH)
        try:
            for chunk in _chunks(words):
                placeholders = ",".join("?" * len(chunk))
                rows = connection.execute(
                    "SELECT word, reading, count FROM senses "
                    f"WHERE word IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                for word, reading, count in rows:
                    counts.setdefault(word, {})[reading or ""] = count
        finally:
            connection.close()
        return counts
    connection = connect()
    try:
        for chunk in _chunks(words):
            placeholders = ",".join("?" * len(chunk))
            try:
                rows = connection.execute(
                    "SELECT word, reading, COUNT(*) FROM senses "
                    f"WHERE word IN ({placeholders}) "
                    "AND text IS NOT NULL AND TRIM(text) <> '' "
                    f"AND {_XREF_SQL} "
                    "GROUP BY word, reading",
                    tuple(chunk),
                ).fetchall()
            except sqlite3.OperationalError:
                return counts
            for word, reading, count in rows:
                # Keyed by the reading exactly as stored: capitalisation
                # separates a surname ("Gān", 1 sense) from the word ("gān",
                # 12 senses), and the representative reading is chosen by these
                # counts.
                counts.setdefault(word, {})[reading or ""] = min(count, _MAX_DEFS)
    finally:
        connection.close()
    return counts


def _entry_sense_count(counts: dict[str, dict[str, int]], entry: dict) -> int:
    """Senses of this entry's own reading, from `_cedict_reading_counts`.

    CEDICT keys senses by the simplified word, but a traditional spelling can
    also be a key of its own (乾 holds the qián senses, 干 holds gān). Like
    `_attach_definitions`, use the key that actually has this reading; trying the
    traditional key first and stopping there scored every gān row of 乾/干 as 0,
    so a tie let the surname "Gān" win over "gān" (12 senses).
    """
    pinyin = entry.get("pinyin") or ""
    key = reading_key(pinyin)
    candidates = [
        counts[word]
        for word in (entry["traditional"], entry["simplified"])
        if counts.get(word)
    ]
    # Exact reading first, across both keys: "Gān" and "gān" are different.
    for by_reading in candidates:
        if pinyin in by_reading:
            return by_reading[pinyin]
    # Then accept a spelling difference for the same tone (numeric vs marked).
    for by_reading in candidates:
        if any(reading_key(r) == key for r in by_reading):
            return sense_count_for(by_reading, pinyin)
    return 0


def sense_count_for(by_reading: dict[str, int], pinyin: str | None) -> int:
    """Senses for one reading: the exact string first (case and tone), then a
    tone-aware match for a reading spelled differently (numeric vs marked)."""
    pinyin = pinyin or ""
    if pinyin in by_reading:
        return by_reading[pinyin]
    key = reading_key(pinyin)
    return max(
        (n for reading, n in by_reading.items() if reading_key(reading) == key),
        default=0,
    )


def _attach_relevance(entries: list[dict], query: str) -> list[dict]:
    """Attach the English relevance tier for the current query, if enabled."""
    if os.environ.get("YABAI_RELEVANCE", "1") == "0":
        return entries
    for entry in entries:
        features = relevance.match_entry(entry, query)
        if features is not None:
            entry["_relevance"] = True
            entry["tier"] = features.tier
    return entries


def _mark_latin_readings(entries: list[dict]) -> list[dict]:
    """Flag readings that are a Latin word spelled out, not real pinyin.

    The browser uses `pinyin_latin` to keep such a reading in Latin instead of
    converting parts of it to bopomofo (word姐 is "w o r d jiě").
    """
    for entry in entries:
        entry["pinyin_latin"] = not is_standard_pinyin(entry.get("pinyin", ""))
    return entries


def _light_decorate(entries: list[dict]) -> list[dict]:
    """Attach the cheap fields needed to rank and de-duplicate rows.

    Lazy hydration: a search still finds and scores every match, but only the
    requested page gets full definitions. This pass adds the frequency (the
    relevance tie-break), the variant redirect (`main`), and the CEDICT sense
    count used to choose the best reading for a duplicate headword. The row is
    fully decorated later by `decorate_entries` if it lands on the page.
    """
    if not entries:
        return entries
    _attach_freq(entries)
    words = sorted(
        {entry["traditional"] for entry in entries}
        | {entry["simplified"] for entry in entries}
    )
    _set_variant_fields(entries, _variant_form_rows(words))
    counts = _cedict_reading_counts(words)
    for entry in entries:
        entry["_reading_count"] = _entry_sense_count(counts, entry)
    _mark_latin_readings(entries)
    return entries


def _own_readings(
    entries: list[dict], index: "_VariantIndex"
) -> tuple[dict[tuple[str, str], set[str]], set[tuple[str, str]]]:
    """The readings each (traditional, simplified) headword really owns in CEDICT.

    CEDICT keys its senses by the *simplified* word, so 隻 and 秖 and 只 (all
    simplified 只) share one pool of senses. A traditional spelling only owns the
    readings of its own CEDICT rows, minus rows that are just a cross-reference
    to another form (只 [zhī] is "variant of 隻", so 只 owns zhǐ only; 隻 owns zhī
    only; 長 owns both cháng and zhǎng). Used to keep a row from showing another
    spelling's readings under its own headword.
    """
    simplified = sorted(
        {entry["simplified"] for entry in entries if entry["simplified"]}
    )
    if not simplified:
        return {}, set()
    placeholders = ",".join("?" * len(simplified))
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT traditional, simplified, pinyin FROM entries "
            f"WHERE simplified IN ({placeholders})",
            tuple(simplified),
        ).fetchall()
    finally:
        connection.close()
    own: dict[tuple[str, str], set[str]] = {}
    spellings: dict[tuple[str, str], set[str]] = {}
    for traditional, simp, pinyin in rows:
        if not pinyin or index.is_redirect(traditional, pinyin):
            continue
        own.setdefault((traditional, simp), set()).add(reading_key(pinyin))
        spellings.setdefault((simp, reading_key(pinyin)), set()).add(traditional)
    # (simplified, reading) pairs that several traditional spellings share: their
    # senses sit in one pool (隻 and 秖 are both 只 zhī), so each spelling's own
    # senses have to be told apart by its CEDICT row.
    pooled = {key for key, forms in spellings.items() if len(forms) > 1}
    return own, pooled


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

    # Fetch every source's senses and classifiers once for the result set, plus
    # the variant-form redirects (cheap, and reused by the light path).
    senses: dict[str, dict[str, list[str]]] = {}
    classifiers: dict[str, dict[str, list[dict]]] = {}
    form_rows_all = _variant_form_rows(words)
    variant_index = _VariantIndex(form_rows_all)
    own_readings, pooled_readings = _own_readings(entries, variant_index)
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
                        value = normalize_english(_strip_pron_notes(value))
                        # A classifier inside a sense is written "(CL:棵[ke1])";
                        # spell out the label for the numbered definition.
                        value = value.replace("CL:", "Classifier:")
                    bucket = senses[anchor].setdefault(word, [])
                    item = (reading or "", value)
                    # Traditional spellings that share a simplified headword can
                    # contribute the same sense twice (e.g. 乾 and 干 both list
                    # "surname Gan"): keep it once per reading.
                    if item not in bucket:
                        bucket.append(item)
            if anchor == "wiktionary":
                # Wiktionary can store a gloss on a different traditional
                # spelling of the same simplified word (e.g. 寧為 vs 寧爲).
                # Attach those glosses under the simplified key too.
                extra_glosses = connection.execute(
                    "SELECT r.word, r.simplified, w.gloss FROM readings r "
                    "JOIN wiktionary w ON w.word = r.word "
                    "WHERE r.simplified IN ({ph}) AND w.gloss IS NOT NULL".format(
                        ph=placeholders
                    ),
                    params,
                )
                for word, simplified, text in extra_glosses:
                    value = (text or "").strip()
                    if not value:
                        continue
                    for key in (word, simplified):
                        bucket = senses[anchor].setdefault(key, [])
                        if value not in {existing for _read, existing in bucket}:
                            bucket.append(("", value))
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
        finally:
            connection.close()

    def for_entry(mapping: dict, entry: dict) -> tuple[str | None, list]:
        """The matched source headword and its value, by traditional then simplified."""
        for word in (entry["traditional"], entry["simplified"]):
            if word in mapping:
                return word, mapping[word]
        return None, []

    def for_senses(mapping: dict, entry: dict) -> tuple[str | None, list]:
        """Like for_entry, but prefer the key that actually has this reading.

        CEDICT stores senses under the simplified headword, so e.g. 乾 (gān)
        would otherwise borrow 乾's qián senses just because 乾 is listed first.
        """
        reading = reading_key(entry.get("pinyin", ""))
        for word in (entry["traditional"], entry["simplified"]):
            values = mapping.get(word)
            if values and any(reading_key(value[0]) == reading for value in values):
                return word, values
        return for_entry(mapping, entry)

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
            headword, values = for_senses(senses[anchor], entry)
            if anchor == "cedict" and values:
                # Only this spelling's own readings (see `_own_readings`). Never
                # filter down to nothing: a reading we cannot match keeps all.
                if variant_index.is_redirect(
                    entry["traditional"], entry.get("pinyin")
                ):
                    # A "variant of X" row owns just its own reading (只 [zhī]).
                    wanted = {reading_key(entry.get("pinyin"))}
                else:
                    wanted = own_readings.get(
                        (entry["traditional"], entry["simplified"])
                    )
                if wanted:
                    scoped = [v for v in values if reading_key(v[0]) in wanted]
                    if scoped:
                        values = scoped
                values = _drop_self_references(
                    values, {entry["traditional"], entry["simplified"]}
                )
                own_key = reading_key(entry.get("pinyin"))
                if (entry["simplified"], own_key) in pooled_readings:
                    # Several spellings share this reading's senses: keep only
                    # the ones on this spelling's row (grain -> 秖, classifier ->
                    # 隻). Match the exact reading first so "Gān" (surname) is not
                    # treated as the same group as "gān" (dry).
                    own_reading = entry.get("pinyin") or ""
                    same_reading = [v for v in values if v[0] == own_reading]
                    if not same_reading:
                        same_reading = [
                            v for v in values if reading_key(v[0]) == own_key
                        ]
                    mine = _owned_by_entry(same_reading, entry)
                    values = [v for v in values if v not in same_reading] + mine
            _cls_headword, clss = for_entry(classifiers[anchor], entry)
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
                        "headword": headword or _cls_headword or entry["simplified"],
                        "groups": groups,
                        "defs": [text for _reading, text in values[:_MAX_DEFS]],
                        "classifiers": clss[:_MAX_DEFS],
                    }
                )
        entry["definitions"] = defs
    _set_variant_fields(entries, form_rows_all)
    return entries


# Pinyin tone-marked vowels, used to recognise a reading in a definition. A
# parenthetical English note ("colloquial") has none, so it is ignored.
_PINYIN_TONE_MARKS = "āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜĀÁǍÀĒÉĚÈĪÍǏÌŌÓǑÒŪÚǓÙǕǗǙǛ"
# Han characters including the supplementary ideographic planes (extension B+).
_HAN_RANGES = (
    "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\U00020000-\U0002a6df\U0002a700-\U0002ebef"
    "\U0002f800-\U0002fa1f\U00030000-\U000323af"
)

# A reading written after a Chinese word, e.g. "alternative form of 零次元
# (língcìyuán)". Group 1 is the text inside the parentheses.
_GLOSS_READING_RE = re.compile(
    r"[" + _HAN_RANGES + r"]\s*\(([^()]*[" + _PINYIN_TONE_MARKS + r"][^()]*)\)"
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
    r"([0-9" + _HAN_RANGES + r"]+)(?:\s*[／/]\s*([0-9" + _HAN_RANGES + r"]+))?\s*\(([^()]*)\)"
)


@lru_cache(maxsize=50_000)
@lru_cache(maxsize=100_000)
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


# Dungan readings are stored as a Cyrillic spelling plus a Latin romanization
# and tone numerals: "җигуанзыхуар (jiguanzɨhuar, I-I-I-I)".
_DUNGAN_READING_RE = re.compile(r"\(([^,()]+),\s*([IVX]+(?:[-\s]+[IVX]+)*)\)")


def _clean_dungan_reading(reading: str) -> str:
    """Keep the Latin romanization and tone numerals from a Dungan reading.

    The Cyrillic spelling and the Latin romanization say the same thing; the
    app shows the Latin one, with its tones, as a single readable line.
    """
    match = _DUNGAN_READING_RE.search(reading or "")
    if not match:
        return reading
    return f"{match.group(1).strip()} ({match.group(2).strip()})"


# The dash or colon that splits a 歇后语 (two-part saying) into its two halves.
_READING_SEPARATOR_RE = re.compile(r"[—\-:：;；]+")


def _joined_reading(headword: str, readings: list[str]) -> str | None:
    """Join part-by-part readings for a split phrase, or return None.

    Wiktionary stores one Mandarin reading per half of a 歇后语, e.g.
    猪八戒吃人参果——全不知滋味 has "Zhū Bājiè chī rénshēnguǒ" and
    "quán bù zhī zīwèi". Joining them lets the furigana put one syllable over
    each character instead of showing only the first half. A normal polyphonic
    word has no separator, so it keeps its first reading.
    """
    if len(readings) < 2 or not _READING_SEPARATOR_RE.search(headword):
        return None
    parts = [part for part in _READING_SEPARATOR_RE.split(headword) if part]
    if len(parts) != len(readings):
        return None
    spoken = sum(
        1
        for char in headword
        if not unicodedata.category(char).startswith(("P", "S"))
    )
    joined = " ".join(readings)
    return joined if len(joined.split()) == spoken else None


def _attach_source_pinyin(entries: list[dict]) -> list[dict]:
    """Fill an empty pinyin from the sources' own readings, in one batch.

    A `*` (all-headwords) page builds minimal rows for headwords CEDICT lacks,
    so those start without a reading. Look the readings up in the source
    databases (e.g. Wiktionary's readings table) instead of leaving them blank.
    """

    def needs_lookup(entry: dict) -> bool:
        # An empty reading, a dialect reading (which may be only the first of
        # several), or a split phrase (which may have one reading per half).
        return (
            not entry.get("pinyin")
            or entry.get("pinyin_no_tone")
            or bool(_READING_SEPARATOR_RE.search(entry.get("traditional") or ""))
        )

    # A dialect reading already on the entry is only the first of several the
    # sources list, so those entries are looked up too.
    words = sorted(
        {entry["traditional"] for entry in entries if needs_lookup(entry)}
        | {entry["simplified"] for entry in entries if needs_lookup(entry)}
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

    # Words with no Mandarin reading get their dialect readings (Wu,
    # Shanghainese, Xiang, Eastern Min, ...), left uncolored, each with the
    # dialect's name as a label. Keep every variety the sources list: the first
    # one is the main reading, and the rest are shown on the lines below.
    fallback: dict[str, list[tuple[str, str]]] = {}
    for source in _SOURCE_QUERIES:
        for word, pinyin, label in query(source, "pinyin_fallback_batch_sql"):
            if pinyin:
                if (label or "") == "Dungan":
                    pinyin = _clean_dungan_reading(pinyin)
                fallback.setdefault(word, []).append((pinyin, label or ""))

    # Every Mandarin reading the sources list, keyed by headword. A 歇后语 has
    # one reading per half and needs them joined (see _joined_reading).
    all_mandarin: dict[str, list[str]] = {}
    for source in _SOURCE_QUERIES:
        for word, pinyin in query(source, "pinyin_all_batch_sql"):
            if not pinyin:
                continue
            values = all_mandarin.setdefault(word, [])
            if pinyin not in values:
                values.append(pinyin)

    for entry in entries:
        # A split phrase may have one Mandarin reading per half. Join them so
        # the furigana can cover the whole headword instead of just the first
        # half.
        for form in (entry["traditional"], entry["simplified"]):
            readings = all_mandarin.get(form)
            if readings:
                joined = _joined_reading(form, readings)
                if joined:
                    entry["pinyin"] = joined
                break

        # A Mandarin reading is final. A dialect reading (pinyin_no_tone) still
        # needs the source's full list: _source_only_entries kept only the first
        # one, so a word like 松毛須 would lose its Eastern Min reading.
        if entry.get("pinyin") and not entry.get("pinyin_no_tone"):
            continue
        word = entry["traditional"]
        if word not in found and word not in fallback:
            word = entry["simplified"]
        # A Mandarin reading wins, unless the entry already has a dialect
        # reading that we are enriching.
        if not entry.get("pinyin_no_tone") and word in found:
            pinyin, split, no_tone, label = found[word]
            entry["pinyin"] = _split_pinyin(pinyin) if split else pinyin
            if no_tone:
                entry["pinyin_no_tone"] = True
                if label:
                    entry["pinyin_label"] = label
            continue
        readings: list[dict] = []
        seen_readings: set[tuple[str, str]] = set()
        for pinyin, label in fallback.get(word, []):
            key = (pinyin, label)
            if key in seen_readings:
                continue
            seen_readings.add(key)
            readings.append({"pinyin": pinyin, "label": label})
        if not readings:
            continue
        entry["pinyin"] = readings[0]["pinyin"]
        entry["pinyin_no_tone"] = True
        if readings[0]["label"]:
            entry["pinyin_label"] = readings[0]["label"]
        if len(readings) > 1:
            entry["readings"] = readings
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
    _attach_hsk(entries)
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

    # The entry's own word. The prefilled table (taiwan_readings.sqlite) is the
    # source of truth, so the shown reading, the search and the ranking agree. A
    # row that is not in the table (or no table at all) falls back to computing
    # the reading live with the same function the build script uses.
    prefilled = _taiwan_lookup(
        [(entry["traditional"], entry["pinyin"]) for entry in entries]
    )
    missing = [
        entry
        for entry in entries
        if (entry["traditional"], entry["pinyin"]) not in prefilled
    ]
    by_word = _moe_readings([entry["traditional"] for entry in missing])
    for entry in entries:
        key = (entry["traditional"], entry["pinyin"])
        if key in prefilled:
            entry["pinyin_taiwan"] = prefilled[key]
            continue
        entry["pinyin_taiwan"] = _entry_taiwan_reading(
            entry["traditional"],
            entry["simplified"],
            entry["pinyin"],
            entry.get("english_normalized", ""),
            by_word.get(entry["traditional"], []),
            counts,
            char_map,
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

    # IDS headwords have no Unicode glyph; use their scraped Commons images.
    for entry in entries:
        for word in (entry.get("traditional", ""), entry.get("simplified", "")):
            if any("\u2ff0" <= char <= "\u2fff" for char in word):
                images = _ids_images_for(word)
                if images:
                    entry["ids_images"] = images
                    break

    _mark_latin_readings(entries)
    return entries


def decorate_entries(entries: list[dict]) -> list[dict]:
    """Fully decorate rows that a light (`hydrate`) search pass only ranked.

    Lazy hydration leaves rows outside the response page without definitions;
    call this on the page slice just before it is rendered.
    """
    pending = [entry for entry in entries if "definitions" not in entry]
    if pending:
        _decorate(pending)
    # Drop the lightweight marker so the response looks like a normal one.
    for entry in entries:
        entry.pop("_reading_count", None)
    return entries


# Explicit LRU shared by lookup() and lookup_many(): the batched path can fill
# entries for several words at once, while a single lookup still stays cheap.
_lookup_cache: "OrderedDict[str, list[dict]]" = OrderedDict()
_lookup_cache_lock = threading.Lock()
_LOOKUP_CACHE_MAX = 8192
_LOOKUP_BATCH_WORDS = 400


def _lookup_cache_get(word: str) -> list[dict] | None:
    with _lookup_cache_lock:
        hit = _lookup_cache.get(word)
        if hit is not None:
            _lookup_cache.move_to_end(word)
        return hit


def _lookup_cache_put(word: str, entries: list[dict]) -> None:
    with _lookup_cache_lock:
        _lookup_cache[word] = entries
        _lookup_cache.move_to_end(word)
        while len(_lookup_cache) > _LOOKUP_CACHE_MAX:
            _lookup_cache.popitem(last=False)


def _preferred_sort(word: str, entries: list[dict]) -> None:
    """Apply lookup()'s preferred-reading rule to one word's rows."""
    preferred = (
        PREFERRED_READINGS.get(word)
        or PREFERRED_READINGS.get(to_simplified(word))
        or PREFERRED_READINGS.get(to_traditional(word))
    )
    if preferred:
        target = _preferred_key(preferred)
        entries.sort(key=lambda entry: _preferred_key(entry["pinyin"]) != target)


def _lookup_uncached(words: list[str]) -> dict[str, list[dict]]:
    """Fetch and decorate several headwords with one query per batch.

    The SQL order is the same as lookup()'s, and the preferred-reading sort is
    applied per target afterwards. A row that matches two targets (its
    simplified and traditional forms are both requested) is shared between
    them; callers copy before mutating, as they already did for lookup().
    """
    result: dict[str, list[dict]] = {word: [] for word in words}
    use_counts = _sense_counts_ok()
    for start in range(0, len(words), _LOOKUP_BATCH_WORDS):
        batch = words[start : start + _LOOKUP_BATCH_WORDS]
        batch_set = set(batch)
        placeholders = ",".join("?" * len(batch))
        params = tuple(batch) * 2
        columns = (
            "simplified, traditional, pinyin, english, english_normalized, "
            "entries.id AS _id"
        )
        if use_counts:
            sql = (
                f"SELECT {columns} FROM entries "
                f"WHERE simplified IN ({placeholders}) "
                f"OR traditional IN ({placeholders})"
            )
        else:
            sql = (
                f"SELECT {columns} FROM entries "
                f"WHERE simplified IN ({placeholders}) "
                f"OR traditional IN ({placeholders}) "
                "ORDER BY (SELECT COUNT(*) FROM senses s "
                "          WHERE s.word = entries.simplified "
                "          AND s.reading = entries.pinyin "
                f"          AND {_XREF_SQL}) DESC, "
                "entries.id"
            )
        connection = connect()
        try:
            rows = [dict(row) for row in connection.execute(sql, params).fetchall()]
        finally:
            connection.close()
        batch_rows: dict[str, list[dict]] = {word: [] for word in batch}
        for row in rows:
            for key in {row["simplified"], row["traditional"]}:
                if key in batch_set:
                    batch_rows[key].append(row)
        if use_counts:
            raw_counts = _raw_sense_counts(
                [row["simplified"] for row in rows if row.get("simplified")]
            )
            for word in batch:
                batch_rows[word].sort(
                    key=lambda row: (
                        -raw_counts.get(
                            (row["simplified"], row["pinyin"] or ""), 0
                        ),
                        row["_id"],
                    )
                )
        unique = {id(row): row for word in batch for row in batch_rows[word]}
        for row in unique.values():
            row.pop("_id", None)
        _decorate(list(unique.values()))
        for word in batch:
            result[word] = batch_rows[word]
    for word in words:
        _preferred_sort(word, result[word])
    return result


def lookup_many(words: list[str]) -> dict[str, list[dict]]:
    """Return lookup(word) for every word, using as few queries as possible."""
    unique = [word for word in dict.fromkeys(words) if word]
    if not unique:
        return {}
    result: dict[str, list[dict]] = {}
    missing: list[str] = []
    for word in unique:
        hit = _lookup_cache_get(word)
        if hit is not None:
            result[word] = hit
        else:
            missing.append(word)
    if missing:
        fetched = _lookup_uncached(missing)
        for word in missing:
            entries = fetched.get(word, [])
            _lookup_cache_put(word, entries)
            result[word] = entries
    return result


def lookup(word: str) -> list[dict]:
    """Return entries whose simplified or traditional form is exactly `word`.

    Entries are ordered so the reading with the most senses (normally the most
    common one) comes first - e.g. 都[dōu] before 都[dū] and 都[Dū] - because the
    parsed input shows the first entry's reading. Cached: callers only read the
    returned entries (annotate, variant expansion), never mutate them.
    """
    if not word:
        return []
    hit = _lookup_cache_get(word)
    if hit is not None:
        return hit
    entries = _lookup_uncached([word])[word]
    _lookup_cache_put(word, entries)
    return entries


# The parsed-input display only reads these fields from entries[0].
_SLIM_KEYS = (
    "pinyin",
    "pinyin_taiwan",
    "pinyin_label",
    "pinyin_no_tone",
    "ids_images",
)


def slim_entry(entry: dict) -> dict:
    """Only the fields the parsed-input display reads."""
    return {key: entry[key] for key in _SLIM_KEYS if entry.get(key)}


@lru_cache(maxsize=50_000)
def lookup_reading(word: str) -> list[dict]:
    """Like lookup(word)[:1], but decorates only the winning entry.

    Same SQL and preferred-reading rule; _decorate (definitions, sources,
    frequency) runs on one row instead of every row, and the cached value holds
    only the five display fields, so a large cache stays small.
    """
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT simplified, traditional, pinyin, english, english_normalized "
            "FROM entries WHERE simplified = ? OR traditional = ? "
            "ORDER BY (SELECT COUNT(*) FROM senses s "
            "          WHERE s.word = entries.simplified "
            "          AND s.reading = entries.pinyin "
            f"          AND {_XREF_SQL}) DESC, "
            "entries.id",
            (word, word),
        ).fetchall()
    finally:
        connection.close()
    raw = [dict(row) for row in rows]
    if not raw:
        return []
    preferred = (
        PREFERRED_READINGS.get(word)
        or PREFERRED_READINGS.get(to_simplified(word))
        or PREFERRED_READINGS.get(to_traditional(word))
    )
    if preferred:
        target = _preferred_key(preferred)
        raw.sort(key=lambda entry: _preferred_key(entry["pinyin"] or "") != target)
    decorated = _decorate(raw[:1])
    return [slim_entry(decorated[0])] if decorated else []


def warm_lookup_readings(limit: int = 5000) -> None:
    """Pre-fill the parser's lookup cache with the most frequent words."""
    connection = connect()
    try:
        words = [
            row[0]
            for row in connection.execute(
                "SELECT word FROM subtlex.freq ORDER BY rank LIMIT ?", (limit,)
            )
        ]
    except sqlite3.Error:
        return
    finally:
        connection.close()
    for word in words:
        if word and has_cjk(word):
            lookup_reading(word)


def has_cjk(text: str) -> bool:
    """True when `text` contains a CJK ideograph.

    Used to tell a real Chinese word apart from a latin headword (CEDICT has a
    few latin headwords, e.g. "word" whose "pinyin" is just the letters).
    Includes the supplementary ideographic planes (extension B+).
    """
    for char in text:
        code = ord(char)
        if (
            0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
            or 0x20000 <= code <= 0x2A6DF
            or 0x2A700 <= code <= 0x2EBEF
            or 0x2F800 <= code <= 0x2FA1F
            or 0x30000 <= code <= 0x323AF
        ):
            return True
    return False


# A rare character the MOE dictionary keeps as a picture ("&8ff0._104_0.gif;").
# Such a headword has no CJK codepoint, but is still a real entry to look up.
_MOE_GLYPH_RE = re.compile(r"&[A-Za-z0-9][A-Za-z0-9._-]*\.(?:gif|png);")


def has_cjk_or_glyph(text: str) -> bool:
    """Like has_cjk(), but also accepts an MOE glyph picture reference."""
    return has_cjk(text) or bool(_MOE_GLYPH_RE.search(text))


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
        elif char in "*＊":
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
    """True when the selected frequency rank column was filled at import time."""
    global _freq_rank_ready_cache
    if _freq_rank_ready_cache is None:
        connection = connect()
        try:
            column = connection.execute(
                "SELECT 1 FROM pragma_table_info('entries') WHERE name = ?",
                (FREQ_RANK_COLUMN,),
            ).fetchone()
            flag = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (FREQ_RANK_COLUMN,)
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
    """ORDER BY terms that put the most frequent words first.

    Empty when the configured frequency database is missing. Words missing from
    the list sort last. Uses the precomputed rank column when it is available,
    and the live subquery otherwise.
    """
    if not FREQ_DB_PATH.exists():
        return []
    if _freq_rank_ready():
        return [f"{FREQ_RANK_COLUMN} IS NULL", FREQ_RANK_COLUMN]
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
_ANCHOR_ORDER = ["cedict", "wiktionary", "moe", "moe_revised"]


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
    source_head_legacy = os.environ.get("YABAI_SOURCE_HEAD_LEGACY") == "1"
    # (form, LIKE pattern) pairs, one per script variant, for a normal
    # (non-exact) search.
    pairs = [
        (form, _like_pattern(form if any(c in text for c in "*?＊？") else f"*{form}*"))
        for form in forms
    ]
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
        query_stage = pd.stage(f"src:{source['anchor']}:query")
        query_stage.__enter__()
        try:
            if all_stars:
                if not source.get("all_sql"):
                    query_stage.__exit__(None, None, None)
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
                # Match the query like the CEDICT search does (see pairs above).
                rows = []
                seen_rows: set[str] = set()
                # Some sources already have a trigram index over the headword
                # column (their senses_fts table); Wiktionary has its own.
                fts_sql = source.get("head_fts_sql") or source.get("like_sql_fts")
                use_fts = bool(fts_sql) and _source_has_fts(source)
                head_source = {
                    "wiktionary": "wikt_head",
                    "moe": "moe_head",
                    "moe_revised": "moe_revised_head",
                    "zhwiktionary": "zhwikt_head",
                    "jpwiktionary": "jpwikt_head",
                }.get(source["anchor"], "")
                for form, pattern in pairs:
                    candidates = (
                        _gram_candidates(head_source, form)
                        if head_source and not source_head_legacy
                        else None
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
                        sql = fts_sql
                        query_pattern = f"%{form}%"
                    else:
                        sql = source["like_sql"]
                        query_pattern = pattern
                    for (head,) in database.execute(sql, (query_pattern,)):
                        if head not in seen_rows:
                            seen_rows.add(head)
                            rows.append((head,))
            trace = pd.current()
            if trace:
                trace.counters[f"src:{source['anchor']}:candidates"] = len(rows)
            query_stage.__exit__(None, None, None)
            read_stage = pd.stage(f"src:{source['anchor']}:readings")
            read_stage.__enter__()
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
                            if label == "Dungan":
                                pinyin = _clean_dungan_reading(pinyin)
                            break
                if add(head, pinyin, no_tone, label):
                    break
            read_stage.__exit__(None, None, None)
        finally:
            database.close()
        if len(heads) >= limit:
            break

    # The frequency list can contribute headwords too (it has no definitions).
    if len(heads) < limit and FREQ_DB_PATH.exists():
        database = _connect(FREQ_DB_PATH)
        freq_stage = pd.stage("src:freq:query")
        freq_stage.__enter__()
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
                # A 3+ character headword can use the trigram index; shorter
                # ones fall back to the scan. freq_fts rows are in rank order.
                use_fts = _has_freq_fts()
                for form, pattern in pairs:
                    candidates = (
                        _gram_candidates("freq_head", form)
                        if not source_head_legacy
                        else None
                    )
                    if candidates is not None:
                        if candidates:
                            holes = ",".join("?" * len(candidates))
                            for (head,) in database.execute(
                                "SELECT word FROM freq WHERE word IN "
                                f"({holes}) ORDER BY rank",
                                tuple(candidates),
                            ):
                                if head not in seen_rows:
                                    seen_rows.add(head)
                                    rows.append((head,))
                        continue
                    if use_fts and len(form) >= 3:
                        sql = ("SELECT word FROM freq_fts WHERE word LIKE ? "
                               "ORDER BY freq_rank")
                        query_pattern = f"%{form}%"
                    else:
                        sql = ("SELECT word FROM freq WHERE word LIKE ? ESCAPE '\\' "
                               "ORDER BY rank")
                        query_pattern = pattern
                    for (head,) in database.execute(sql, (query_pattern,)):
                        if head not in seen_rows:
                            seen_rows.add(head)
                            rows.append((head,))
        finally:
            database.close()
        trace = pd.current()
        if trace:
            trace.counters["src:freq:candidates"] = len(rows)
        freq_stage.__exit__(None, None, None)
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


# Pinyin rows from the source headword tables, cached until a database changes.
_source_pinyin_rows_cache: tuple[tuple, list[tuple[str, str, str, str, str]]] | None = None


def _source_pinyin_rows() -> list[tuple[str, str, str, str, str]]:
    """(word, simplified, marked, toneless, numeric) for every source reading."""
    global _source_pinyin_rows_cache
    sources = [source for source in _SOURCE_QUERIES if source.get("pinyin_rows_sql")]
    stamp = tuple(
        (
            source["anchor"],
            source["database"].stat().st_mtime
            if source["database"].exists()
            else None,
        )
        for source in sources
    )
    if _source_pinyin_rows_cache is not None and _source_pinyin_rows_cache[0] == stamp:
        return _source_pinyin_rows_cache[1]
    rows: list[tuple[str, str, str, str, str]] = []
    for source in sources:
        if not source["database"].exists():
            continue
        connection = _connect(source["database"])
        try:
            for record in connection.execute(source["pinyin_rows_sql"]):
                word = record[0]
                simplified = record[1] if len(record) > 2 else to_simplified(word)
                pinyin = record[-1]
                if not word or not simplified or not pinyin:
                    continue
                marked = (
                    _split_pinyin(pinyin)
                    if source.get("split_pinyin")
                    else pinyin
                )
                toneless = to_toneless(marked)
                if not toneless:
                    continue
                rows.append((word, simplified, marked, toneless, to_numeric(marked)))
        finally:
            connection.close()
    _source_pinyin_rows_cache = (stamp, rows)
    return rows


# The same rows sorted by toneless reading, so a query can binary-search the
# prefix instead of scanning every source reading (there are hundreds of
# thousands after import). Rebuilt whenever _source_pinyin_rows_cache is.
_source_pinyin_sorted_cache: tuple[tuple, list[str], list[tuple]] | None = None


def _source_pinyin_sorted() -> tuple[list[str], list[tuple]]:
    """Return (toneless keys, rows) sorted by reading for prefix searches."""
    global _source_pinyin_sorted_cache
    rows = _source_pinyin_rows()
    stamp = _source_pinyin_rows_cache[0]
    if (
        _source_pinyin_sorted_cache is not None
        and _source_pinyin_sorted_cache[0] == stamp
    ):
        return _source_pinyin_sorted_cache[1], _source_pinyin_sorted_cache[2]
    ordered = sorted(rows, key=lambda row: row[3])
    keys = [row[3] for row in ordered]
    _source_pinyin_sorted_cache = (stamp, keys, ordered)
    return keys, ordered


def _prefix_rows(keys: list[str], rows: list[tuple], prefix: str) -> list[tuple]:
    """The rows whose toneless reading starts with `prefix`."""
    start = bisect_left(keys, prefix)
    end = bisect_left(keys, prefix + "\uffff")
    return rows[start:end]


def _source_pinyin_entries(
    query: str, limit: int = 20, keep_rank: bool = False, taiwan: bool = False
) -> list[dict]:
    """Cached wrapper around the source-pinyin scan.

    Common English queries such as "the" and "ing" fall through to this scan on
    every request even though its result only changes when a source database is
    rebuilt. The raw rows are cached; each caller gets fresh dicts because
    `_decorate` mutates them. YABAI_SOURCE_PINYIN_CACHE=0 restores the scan.
    `keep_rank` retains `_search_rank`; `taiwan` matches prefilled Taiwan
    readings.
    """
    entries = [
        dict(entry)
        for entry in _source_pinyin_raw(query, limit, taiwan and _taiwan_ready())
    ]
    if not keep_rank:
        for entry in entries:
            entry.pop("_search_rank", None)
    return entries


@lru_cache(maxsize=1024)
def _source_pinyin_raw(
    query: str, limit: int = 20, taiwan: bool = False
) -> tuple[dict, ...]:
    """Source-only entries whose reading matches a pinyin query.

    CEDICT pinyin search only looks at its own entries; this adds Wiktionary's
    readings (and MOE's) so a word like 剾 (Wiktionary-only, kōu) is found too.
    Returns undecorated rows for the caller to decorate. With `taiwan` the
    readings are matched against the prefilled Taiwan readings of every
    non-CEDICT source (taiwan_readings.sqlite) instead of the sources' own.
    """
    text = (query or "").strip()
    if not text:
        return ()
    parsed = _parse_pinyin_query(text)
    if parsed:
        letters, tones, boundaries = parsed
    else:
        letters = to_toneless(text)
        tones, boundaries = {}, set()
    if not letters:
        return ()
    pattern = _like_pattern(f"*{letters}*")
    # A pinyin wildcard is matched against the source readings with the same
    # zero-or-more-character segments as the SQL LIKE search.
    wildcard_re = None
    if "*" in text:
        wildcard_re = re.compile(
            "^"
            + ".*".join(
                re.escape(to_toneless(part)) for part in text.split("*")
            )
            + "$"
        )
    known = _cedict_heads()
    # (rank key, word, marked reading, simplified word)
    found: list[tuple[tuple, str, str, str]] = []
    # The tone/boundary constraints are fixed for this call, so a reading's rank
    # can be memoised: the same numeric reading recurs across sources.
    rank_cache: dict[str, int | None] = {}

    def consider(
        word: str,
        marked: str,
        numeric: str,
        simplified: str,
        rank: int | None = None,
        shown: str | None = None,
    ) -> None:
        # `marked`/`numeric` are what the query is matched against; `shown` is
        # the reading the row carries (the source's own, when matching Taiwan).
        if simplified in known:
            return
        if parsed:
            if numeric in rank_cache:
                rank = rank_cache[numeric]
            else:
                rank = _reading_rank(numeric, letters, tones, boundaries)
                rank_cache[numeric] = rank
            if rank is None:
                return
        elif rank is None:
            # A plain reading search matches readings that start with the
            # query first, then readings that merely contain it. A wildcard uses
            # its own segment pattern.
            key = to_toneless(marked)
            if wildcard_re is not None:
                if not wildcard_re.fullmatch(key):
                    return
                rank = 0 if key == letters else 1
            elif key == letters:
                rank = 0
            elif key.startswith(letters):
                rank = 1
            elif letters in key:
                rank = 2
            else:
                return
        found.append(((rank, len(marked), word), word, shown or marked, simplified))

    if taiwan:
        # Prefilled Taiwan readings of the non-CEDICT sources (MOE, Wiktionary,
        # zh/ja Wiktionary). Same ranking as below, on the Taiwan reading.
        tw_connection = _connect(TAIWAN_DB_PATH)
        try:
            columns = "tw_word, tw_simplified, tw_mainland, tw_reading, tw_numeric"
            if wildcard_re is not None:
                phases = [
                    (_like_pattern("*".join(to_toneless(part) for part in text.split("*"))), None)
                ]
            else:
                phases = [(f"{letters}%", None), (pattern, f"{letters}%")]
            for pat, exclude in phases:
                sql = (
                    f"SELECT {columns} FROM readings WHERE tw_source <> 'cedict' "
                    "AND tw_toneless LIKE ? ESCAPE '\\'"
                )
                params: tuple = (pat,)
                if exclude is not None:
                    sql += " AND tw_toneless NOT LIKE ? ESCAPE '\\'"
                    params += (exclude,)
                for word, simplified, mainland, reading, numeric in tw_connection.execute(
                    sql, params
                ):
                    consider(word, reading, numeric, simplified, shown=mainland)
                if len(found) >= limit and wildcard_re is None:
                    break
        finally:
            tw_connection.close()
    else:
        if WIKTIONARY_DB_PATH.exists():
            connection = _connect(WIKTIONARY_DB_PATH)
            try:
                # readings_extra holds only Mandarin readings CEDICT does not cover,
                # so the common search never touches the full readings table.
                has_extra = _has_readings_extra()
                if has_extra:
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
                    consider(word, marked, numeric, simplified)

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
                elif has_extra:
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

        # MOE, zh.wiktionary and ja.wiktionary headword readings. The prebuilt
        # search index answers the prefix (and wildcard) part with one indexed
        # query; without it, build the sorted list once and binary-search it.
        indexed_rows = None
        if _search_index_enabled():
            if wildcard_re is not None:
                wildcard_sql = "%".join(
                    to_toneless(part) for part in text.split("*")
                )
                indexed_rows = _source_pinyin_index_rows(like=wildcard_sql)
            else:
                indexed_rows = _source_pinyin_index_rows(prefix=letters)
        if indexed_rows is not None:
            candidates = indexed_rows
            all_rows = None
        else:
            keys, rows = _source_pinyin_sorted()
            candidates = (
                rows if wildcard_re is not None else _prefix_rows(keys, rows, letters)
            )
            all_rows = rows
        for word, simplified, marked, toneless, numeric in candidates:
            if simplified in known:
                continue
            consider(word, marked, numeric, simplified)

        # A tonal query can also match a reading that merely contains the letters
        # (rank 2). Only look for those when the prefix matches did not fill the
        # page, and skip the rows already handled above.
        if parsed and len(found) < limit:
            if indexed_rows is not None:
                extra_rows = _source_pinyin_index_rows(like=f"%{letters}%") or ()
            else:
                extra_rows = all_rows or ()
            for word, simplified, marked, toneless, numeric in extra_rows:
                if toneless.startswith(letters) or letters not in toneless:
                    continue
                if simplified in known:
                    continue
                consider(word, marked, numeric, simplified)

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
    return tuple(entries)


def _source_gloss_version_ok() -> bool:
    """True when source_gloss.sqlite was built from the current CEDICT file."""
    global _source_gloss_ok_cache
    if _source_gloss_ok_cache is not None:
        return _source_gloss_ok_cache
    if not SOURCE_GLOSS_DB_PATH.exists():
        _source_gloss_ok_cache = False
        return False
    connection = _connect(SOURCE_GLOSS_DB_PATH)
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'cedict_sha256'"
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        connection.close()
    if not row:
        print(
            "source_gloss.sqlite has no CEDICT version; using live gloss scan",
            file=sys.stderr,
        )
        _source_gloss_ok_cache = False
        return False
    digest = hashlib.sha256(DB_PATH.read_bytes()).hexdigest()
    _source_gloss_ok_cache = digest == row[0]
    if not _source_gloss_ok_cache:
        print(
            "source_gloss.sqlite is stale for the current CEDICT; "
            "using live gloss scan",
            file=sys.stderr,
        )
    return _source_gloss_ok_cache


def _head_grams_version_ok() -> bool:
    """True when head_grams.sqlite was built from the current CEDICT."""
    global _head_grams_ok_cache
    if _head_grams_ok_cache is not None:
        return _head_grams_ok_cache
    if not HEAD_GRAMS_DB_PATH.exists():
        _head_grams_ok_cache = False
        return False
    connection = _connect(HEAD_GRAMS_DB_PATH)
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'cedict_sha256'"
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        connection.close()
    if not row:
        _head_grams_ok_cache = False
        return False
    digest = hashlib.sha256(DB_PATH.read_bytes()).hexdigest()
    _head_grams_ok_cache = digest == row[0]
    if not _head_grams_ok_cache:
        print(
            "head_grams.sqlite is stale for the current CEDICT; "
            "using the headword scan",
            file=sys.stderr,
        )
    return _head_grams_ok_cache


def _headword_candidate_ids(forms: list[str]) -> set[int] | None:
    """Candidate entry ids for a 1-2 character headword query, or None."""
    if os.environ.get("YABAI_HEADWORD_LEGACY") == "1" or not _head_grams_version_ok():
        return None
    grams: set[str] = set()
    for form in forms:
        if len(form) == 1 or len(form) == 2:
            grams.add(form)
        else:
            return None
    if not grams:
        return None
    connection = _connect(HEAD_GRAMS_DB_PATH)
    try:
        holes = ",".join("?" * len(grams))
        rows = connection.execute(
            f"SELECT entry_id FROM head_grams WHERE gram IN ({holes})",
            tuple(grams),
        ).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def fast_path_status() -> dict:
    """Report whether each derived fast path is usable, with a reason."""
    status: dict[str, dict] = {}

    def record(name: str, path: Path, check) -> None:
        if not path.exists():
            status[name] = {"active": False, "reason": "missing"}
            return
        try:
            active = bool(check())
            status[name] = {
                "active": active,
                "reason": "" if active else "stale or invalid metadata",
            }
        except Exception as exc:
            status[name] = {"active": False, "reason": str(exc)}

    record("gloss_index", SOURCE_GLOSS_DB_PATH, _source_gloss_version_ok)
    record("head_grams", HEAD_GRAMS_DB_PATH, _head_grams_version_ok)
    record("gram_words", GRAM_WORDS_DB_PATH, _gram_words_version_ok)
    trigram = bool(_has_cedict_trigram())
    status["definition_trigram"] = {
        "active": trigram,
        "reason": "" if trigram else "missing",
    }
    try:
        connection = connect()
        column = connection.execute(
            "SELECT 1 FROM pragma_table_info('entries') WHERE name = ?",
            (FREQ_RANK_COLUMN,),
        ).fetchone()
        flag = connection.execute(
            "SELECT value FROM meta WHERE key = ?", (FREQ_RANK_COLUMN,)
        ).fetchone()
        active = bool(column) and flag is not None and flag[0] == "1"
        status["freq_rank"] = {
            "active": active,
            "reason": "" if active else "column or meta flag missing",
        }
    except sqlite3.Error as exc:
        status["freq_rank"] = {"active": False, "reason": str(exc)}
    return status


def _source_gloss_entries(query: str, limit: int = 20) -> list[dict]:
    """Wiktionary headwords whose English gloss matches `query` (undecorated)."""
    text = (query or "").strip()
    if not text or has_cjk(text) or not WIKTIONARY_DB_PATH.exists():
        return []
    text_lower = text.lower()
    pattern = _like_pattern(text if "*" in text else f"*{text}*")
    # Scan enough rows that removing CEDICT-known headwords cannot hide the
    # source-only hits. A phrase can have dozens of dictionary headwords (for
    # example every Taixuanjing tetragram); the old limit*3 cut them off.
    scan_limit = max(limit * 50, 1000)
    # The prebuilt source-only index already excludes CEDICT headwords. Its
    # trigram FTS table turns a phrase search into a millisecond lookup instead
    # of scanning the large live Wiktionary FTS table and filtering afterwards.
    if (
        SOURCE_GLOSS_DB_PATH.exists()
        and os.environ.get("YABAI_GLOSS_LEGACY") != "1"
        and _source_gloss_version_ok()
        and len(text) >= 3
        and text.isascii()
        and "*" not in text
    ):
        connection = _connect(SOURCE_GLOSS_DB_PATH)
        try:
            if text.isascii() and "*" not in text and len(text) >= 3:
                try:
                    rows = _fetchall(
                        connection,
                        "SELECT word FROM source_gloss_fts "
                        "WHERE source_gloss_fts MATCH ? LIMIT ?",
                        ('"' + text.replace('"', '""') + '"', scan_limit),
                        "gloss_scan",
                    )
                except sqlite3.Error:
                    rows = []
                if not rows:
                    rows = _fetchall(
                        connection,
                        "SELECT word FROM source_gloss "
                        "WHERE lower(gloss) LIKE ? ESCAPE '\\' LIMIT ?",
                        (_like_pattern(f"*{text_lower}*"), scan_limit),
                        "gloss_scan",
                    )
            else:
                rows = _fetchall(
                    connection,
                    "SELECT word FROM source_gloss "
                    "WHERE lower(gloss) LIKE ? ESCAPE '\\' LIMIT ?",
                    (_like_pattern(f"*{text_lower}*"), scan_limit),
                    "gloss_scan",
                )
        finally:
            connection.close()
        seen: set[str] = set()
        entries: list[dict] = []
        for (word,) in rows:
            simplified = to_simplified(word)
            if simplified in seen:
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

    connection = _connect(WIKTIONARY_DB_PATH)
    try:
        # A three-or-more character query uses the trigram index (which covers
        # the gloss column) and keeps the table's rowid order for the LIMIT.
        # lower() on both sides keeps the match case-insensitive even when
        # case_sensitive_like is on (which makes LIKE use the B-tree index for
        # Chinese/pinyin queries).
        if "*" not in text and len(text) >= 3 and _has_wiktionary_fts():
            rows = _fetchall(
                connection,
                "SELECT word FROM wiktionary_fts WHERE lower(gloss) LIKE ? "
                "ORDER BY rowid LIMIT ?",
                (f"%{text_lower}%", scan_limit),
                "gloss_scan",
            )
        else:
            rows = _fetchall(
                connection,
                "SELECT DISTINCT word FROM wiktionary "
                "WHERE gloss IS NOT NULL AND lower(gloss) LIKE ? ESCAPE '\\' LIMIT ?",
                (_like_pattern(f"*{text_lower}*"), scan_limit),
                "gloss_scan",
            )
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
    return not any(char in form for char in "*?＊？%_")


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


def _gram_words_version_ok() -> bool:
    """True when gram_words.sqlite matches all source databases it indexes."""
    global _gram_words_ok_cache
    if _gram_words_ok_cache is not None:
        return _gram_words_ok_cache
    if not GRAM_WORDS_DB_PATH.exists():
        _gram_words_ok_cache = False
        return False
    connection = _connect(GRAM_WORDS_DB_PATH)
    try:
        stored = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'source:%'"
            )
        }
    except sqlite3.Error:
        stored = {}
    finally:
        connection.close()
    sources = {
        "moe": MOE_DB_PATH,
        "moe_revised": MOE_REVISED_DB_PATH,
        "wiktionary": WIKTIONARY_DB_PATH,
        "zhwiktionary": ZHWIKTIONARY_DB_PATH,
        "jpwiktionary": JPWIKTIONARY_DB_PATH,
        "freq": FREQ_DB_PATH,
    }
    for name, path in sources.items():
        if not path.exists():
            continue
        value = stored.get(f"source:{name}")
        if not value or hashlib.sha256(path.read_bytes()).hexdigest() != value:
            print(
                f"gram_words.sqlite is stale for {name}; using legacy scans",
                file=sys.stderr,
            )
            _gram_words_ok_cache = False
            return False
    _gram_words_ok_cache = True
    return True


def _gram_candidates(source: str, form: str) -> list[str] | None:
    """Words containing a 1-2 Han-character `form`, or None to fall back.

    The inverted index turns a 1-2 character query into one lookup. None means
    the index is unavailable, the form is not a pure Han 1-2 character string,
    or there are too many candidates for a SQL IN list; the caller then uses the
    normal scan. An empty list means the form occurs nowhere.
    """
    if not (
        GRAM_WORDS_DB_PATH.exists()
        and _gram_words_version_ok()
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


_cedict_trigram_cache: bool | None = None


def _has_cedict_trigram() -> bool:
    """True when the CEDICT database has the trigram entries_fts_trigram index."""
    global _cedict_trigram_cache
    if _cedict_trigram_cache is None:
        connection = _connect(DB_PATH)
        try:
            _cedict_trigram_cache = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='entries_fts_trigram'"
                ).fetchone()
                is not None
            )
        finally:
            connection.close()
    return _cedict_trigram_cache


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


_moe_revised_fts_cache: bool | None = None


def _has_moe_revised_fts() -> bool:
    """True when the comprehensive MOE database has the senses_fts table."""
    global _moe_revised_fts_cache
    if _moe_revised_fts_cache is None:
        connection = _connect(MOE_REVISED_DB_PATH)
        try:
            _moe_revised_fts_cache = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='senses_fts'"
                ).fetchone()
                is not None
            )
        finally:
            connection.close()
    return _moe_revised_fts_cache


_wiktionary_fts_cache: bool | None = None
_zhwiktionary_fts_cache: bool | None = None
_jpwiktionary_fts_cache: bool | None = None


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


def _has_zhwiktionary_fts() -> bool:
    """True when the Chinese Wiktionary database has its trigram senses index."""
    global _zhwiktionary_fts_cache
    if _zhwiktionary_fts_cache is None:
        connection = _connect(ZHWIKTIONARY_DB_PATH)
        try:
            _zhwiktionary_fts_cache = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='senses_fts'"
                ).fetchone()
                is not None
            )
        finally:
            connection.close()
    return _zhwiktionary_fts_cache


def _has_jpwiktionary_fts() -> bool:
    """True when the Japanese Wiktionary database has its trigram senses index."""
    global _jpwiktionary_fts_cache
    if _jpwiktionary_fts_cache is None:
        connection = _connect(JPWIKTIONARY_DB_PATH)
        try:
            _jpwiktionary_fts_cache = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='senses_fts'"
                ).fetchone()
                is not None
            )
        finally:
            connection.close()
    return _jpwiktionary_fts_cache


_readings_extra_cache: bool | None = None


def _has_readings_extra() -> bool:
    """True when the Wiktionary database has the readings_extra table.

    Cached for the process: connections are immutable=1 and a re-import
    already requires a restart.
    """
    global _readings_extra_cache
    if _readings_extra_cache is None:
        _readings_extra_cache = False
        if WIKTIONARY_DB_PATH.exists():
            connection = _connect(WIKTIONARY_DB_PATH)
            try:
                _readings_extra_cache = (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='readings_extra'"
                    ).fetchone()
                    is not None
                )
            finally:
                connection.close()
    return _readings_extra_cache


_freq_fts_cache: bool | None = None


def _has_freq_fts() -> bool:
    """True when the configured frequency database has a freq_fts table."""
    global _freq_fts_cache
    if _freq_fts_cache is None:
        if not FREQ_DB_PATH.exists():
            return False
        connection = _connect(FREQ_DB_PATH)
        try:
            _freq_fts_cache = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='freq_fts'"
                ).fetchone()
                is not None
            )
        finally:
            connection.close()
    return _freq_fts_cache


def _source_has_fts(source: dict) -> bool:
    """True when this source's trigram headword index is available."""
    anchor = source["anchor"]
    if anchor == "moe":
        return _has_moe_fts()
    if anchor == "moe_revised":
        return _has_moe_revised_fts()
    if anchor == "wiktionary":
        return _has_wiktionary_fts()
    if anchor == "zhwiktionary":
        return _has_zhwiktionary_fts()
    if anchor == "jpwiktionary":
        return _has_jpwiktionary_fts()
    return False


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

    for path, has_fts, use_gram_index in (
        (MOE_DB_PATH, _has_moe_fts, True),
        (MOE_REVISED_DB_PATH, _has_moe_revised_fts, False),
    ):
        if not path.exists():
            continue
        connection = _connect(path)
        try:
            use_fts = has_fts()
            for form, pattern in pairs:
                # The gram index is built from the concise dictionary only, so
                # the comprehensive dictionary must use the normal scan.
                candidates = _gram_candidates("moe_sense", form) if use_gram_index else None
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
    if ZHWIKTIONARY_DB_PATH.exists():
        connection = _connect(ZHWIKTIONARY_DB_PATH)
        try:
            use_fts = _has_zhwiktionary_fts()
            for form, pattern in pairs:
                if use_fts and len(form) >= 3 and _simple_form(form):
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
    if JPWIKTIONARY_DB_PATH.exists():
        connection = _connect(JPWIKTIONARY_DB_PATH)
        try:
            use_fts = _has_jpwiktionary_fts()
            for form, pattern in pairs:
                if use_fts and len(form) >= 3 and _simple_form(form):
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
    for path in (DB_PATH, MOE_DB_PATH, MOE_REVISED_DB_PATH, WIKTIONARY_DB_PATH):
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
    if not FREQ_DB_PATH.exists():
        return []
    connection = _connect(FREQ_DB_PATH)
    try:
        rows = connection.execute(
            "SELECT word FROM freq ORDER BY rank LIMIT ?", (limit * 2,)
        ).fetchall()
    finally:
        connection.close()
    return _word_entries([row[0] for row in rows], limit)


def _extra_label_entries(phrase: str, limit: int) -> list[dict] | None:
    """Entries for a quoted phrase that names an app-added section label."""
    name = _label_name(phrase)
    if name is None:
        return None
    if name == "ids":
        return _ids_entries()
    return _LABELS[name][1](limit)


_LABELS = {
    "taiwan pronunciation": ("taiwan pronunciation", _entries_with_taiwan_pron),
    "also pronounced": ("also pronounced", _entries_with_alt_pron),
    "other forms": ("other forms", _entries_with_forms),
    "classifier": ("classifier", _entries_with_classifiers),
    "frequency": ("frequency", _entries_with_frequency),
}


def _label_name(phrase: str) -> str | None:
    """Return the canonical section name a quoted phrase refers to."""
    text = phrase.strip().lower().rstrip(": ")
    if text in _IDS_NOTE.lower():
        return "ids"
    for label in _LABELS:
        if text == label or (
            len(text) >= 3 and (label.startswith(text) or text.startswith(label))
        ):
            return label
    return None


# The frequency section is 289,606 deduped rows (405,472 raw). Building that
# list once is cheap; returning it through _word_entries per request is not,
# because the IN (...) list would exceed SQLite's variable limit. The label
# page uses this ordered list as its index and slices one page at a time.
_frequency_label_words_cache: list[str] | None = None
_frequency_label_lock = threading.Lock()


def _frequency_label_words() -> list[str]:
    """First raw word per simplified frequency headword, in rank order."""
    global _frequency_label_words_cache
    if _frequency_label_words_cache is not None:
        return _frequency_label_words_cache
    with _frequency_label_lock:
        if _frequency_label_words_cache is not None:
            return _frequency_label_words_cache
        words: list[str] = []
        if FREQ_DB_PATH.exists():
            connection = _connect(FREQ_DB_PATH)
            try:
                seen: set[str] = set()
                for (word,) in connection.execute(
                    "SELECT word FROM freq ORDER BY rank"
                ):
                    key = to_simplified(word)
                    if key in seen:
                        continue
                    seen.add(key)
                    words.append(word)
            finally:
                connection.close()
        _frequency_label_words_cache = words
        return words


def label_page(
    query: str, offset: int, page_limit: int
) -> tuple[list[dict], int] | None:
    """Exact page for a quoted section label, without materialising it all.

    Only the frequency section needs this: it has 289,606 deduped rows and
    materialising them through `_word_entries` would exceed SQLite's variable
    limit. The other sections are under 7k rows and are returned whole and
    sliced by the caller.
    """
    text = (query or "").strip()
    if not (len(text) >= 2 and text.startswith('"') and text.endswith('"')):
        return None
    if _label_name(text[1:-1].strip()) != "frequency":
        return None
    words = _frequency_label_words()
    page = words[offset : offset + page_limit]
    if not page:
        return [], len(words)
    return _word_entries(page, len(page)), len(words)


def _source_matches(
    query: str,
    limit: int = 20,
    heads_checked: bool = False,
    pinyin_checked: bool = False,
) -> list[dict]:
    """Every source-only match for a query: headword, pinyin and English gloss.

    `heads_checked`/`pinyin_checked` say the caller already ran the normal
    (non-exact) headword scan or the pinyin scan for this query and found
    nothing. Skipping them here avoids scanning the same source tables twice
    in one search.
    """
    text = (query or "").strip()
    if not text:
        return []
    # An exact source headword counts even without a gloss: Wiktionary stores
    # some words only as readings (e.g. a phrase whose simplified spelling has
    # no gloss row), so the normal gloss-filtered lookup misses them.
    entries = list(
        _source_only_entries(query, limit=limit, exact=True, allow_no_gloss=True)
    )
    if not heads_checked:
        entries.extend(_source_only_entries(query, limit=limit))
    if has_cjk(text):
        # Searching a Chinese phrase also matches source definitions (e.g. the
        # IDS characters named inside a Wiktionary gloss).
        entries.extend(_source_definition_entries(query, limit=limit))
    else:
        if not pinyin_checked:
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


def _dialect_query(text: str, limit: int) -> tuple[tuple, ...]:
    """Raw Wiktionary readings rows for a dialect-label/pinyin query."""
    label_pattern = _like_pattern(f"*{text.lower()}*")
    letters = to_toneless(text)
    where = "kind <> 'mandarin' AND label <> '' AND lower(label) LIKE ? ESCAPE '\\'"
    params: list[str] = [label_pattern]
    if letters:
        # A whole dialect reading only (not a prefix): common Mandarin-ish
        # pinyin such as "shi" should not pull in large dialect lists.
        where += " OR (kind <> 'mandarin' AND pinyin_toneless = ?)"
        params.append(letters)
    connection = _connect(WIKTIONARY_DB_PATH)
    try:
        rows = connection.execute(
            "SELECT word, pinyin, label, simplified FROM readings "
            f"WHERE {where} "
            "ORDER BY length(word), word, id LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        connection.close()
    return tuple(rows)


@lru_cache(maxsize=1024)
def _dialect_rows(text: str, limit: int) -> tuple[tuple, ...]:
    """Cache the raw rows: the label scan has no usable index."""
    return _dialect_query(text, limit)


_dialect_index_cache: tuple[list[str], set[str]] | None = None
_dialect_index_loaded = False


def _dialect_index() -> tuple[list[str], set[str]] | None:
    """Load the dialect early-out tables, or None when the index lacks them."""
    global _dialect_index_cache, _dialect_index_loaded
    if _dialect_index_loaded:
        return _dialect_index_cache
    _dialect_index_loaded = True
    if not _search_index_ok():
        return None
    connection = _connect(SEARCH_INDEX_DB_PATH)
    try:
        version = connection.execute(
            "SELECT value FROM meta WHERE key = 'dialect_version'"
        ).fetchone()
        if not version or version[0] != str(_DIALECT_INDEX_VERSION):
            return None
        labels = [
            row[0]
            for row in connection.execute("SELECT label FROM dialect_labels")
        ]
        pinyin = {
            row[0]
            for row in connection.execute("SELECT toneless FROM dialect_pinyin")
        }
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    _dialect_index_cache = (labels, pinyin)
    return _dialect_index_cache


def _dialect_early_out(text: str) -> bool:
    """True when the index proves the dialect scan would return no rows."""
    index = _dialect_index()
    if index is None:
        return False
    lowered = text.lower()
    # _like_pattern treats all of these specially; do not try to reason about
    # wildcard queries here.
    if any(char in lowered for char in "?*%_\\＊？"):
        return False
    labels, pinyin = index
    if any(lowered in label for label in labels):
        return False
    letters = to_toneless(text)
    if letters and letters in pinyin:
        return False
    return True


def dialect_entries(query: str, limit: int | None = None) -> list[dict]:
    """Words whose Wiktionary dialect reading label or reading matches `query`.

    Wiktionary stores variety labels such as "Chengdu Sichuanese" on readings,
    not on the headword or gloss. Searching that variety name (or its pinyin)
    should still find words like 姜包儿 even when the label is their only
    searchable mention.

    The raw scan is cached because the leading-wildcard label match cannot use
    an index and the same queries recur. YABAI_DIALECT_CACHE=0 restores the
    original uncached scan.
    """
    text = (query or "").strip()
    if not text or "*" in text or has_cjk(text) or len(text) < 3:
        return []
    if limit is None:
        limit = _cap(200)
    if not WIKTIONARY_DB_PATH.exists():
        return []
    if (
        os.environ.get("YABAI_DIALECT_INDEX", "1") != "0"
        and _dialect_early_out(text)
    ):
        return []
    if os.environ.get("YABAI_DIALECT_CACHE", "1") == "0":
        rows = _dialect_query(text, limit)
    else:
        rows = _dialect_rows(text.lower(), limit)

    entries: list[dict] = []
    seen: set[str] = set()
    for word, pinyin, label, simplified in rows:
        simplified = simplified or to_simplified(word)
        if simplified in seen:
            continue
        seen.add(simplified)
        entries.append(
            {
                "simplified": simplified,
                "traditional": word,
                "pinyin": pinyin,
                "english": "",
                "english_normalized": "",
                "pinyin_no_tone": True,
                "pinyin_label": label,
            }
        )
    return _decorate(entries)


# How many results a "list everything" (*) page shows.
ALL_PAGE_SIZE = 25

# Cache of (every distinct simplified headword, its tier group) across all
# sources. Both lists are built together so they cannot drift apart.
_all_headwords_cache: tuple[list[str], list[int]] | None = None


def _build_fold(pairs) -> Callable[[str], str]:
    """Return fold(word): a variant spelling -> its canonical simplified form.

    `pairs` is (traditional, simplified) rows from CEDICT. A traditional form
    that CEDICT maps to exactly one simplified form uses it. This catches
    variants OpenCC misses (e.g. 寶/寳 -> 宝, 砲 -> 炮). A form with several
    candidates (乾 -> 干 or 乾) is ambiguous, so OpenCC decides instead.
    """
    candidates: dict[str, set[str]] = {}
    for traditional, simplified in pairs:
        if traditional and simplified:
            candidates.setdefault(traditional, set()).add(simplified)
    unique = {
        traditional: next(iter(forms))
        for traditional, forms in candidates.items()
        if len(forms) == 1
    }

    def fold(word: str) -> str:
        key = unique.get(word) or to_simplified(word)
        # A second hop lets OpenCC's output be refined by the CEDICT map.
        return unique.get(key, key)

    return fold


def _headword_group(
    head: str, def_words: set[str], has_reading: set[str], dialect_words: set[str]
) -> int:
    """The search tier of a folded headword under a bare "*".

    Mirrors entryTier() in static/app.js: a dialect-only reading is tier 4
    first, then a word with no definition is tier 5 (it has a reading) or
    tier 6 (it has neither).
    """
    if head in dialect_words:
        return 4
    if head in def_words or to_traditional(head) in def_words:
        return 1
    return 5 if head in has_reading else 6


def _order_headwords(
    heads: list[str],
    def_words: set[str],
    has_reading: set[str],
    dialect_words: set[str],
    freq_rank: dict[str, int],
) -> tuple[list[str], list[int]]:
    """Sort headwords by (tier, frequency); return (heads, parallel groups).

    A bare "*" makes match tiers 1-3 moot, so the tiers left are 1, 4, 5 and
    6. Inside a tier the most frequent words come first, and words missing from
    the frequency list come last.
    """

    def freq_key(head: str) -> tuple[int, int]:
        rank = freq_rank.get(head)
        if rank is None:
            rank = freq_rank.get(to_traditional(head))
        return (rank is None, rank or 0)

    keyed = sorted(
        (
            _headword_group(head, def_words, has_reading, dialect_words),
            *freq_key(head),
            head,
        )
        for head in heads
    )
    return [row[-1] for row in keyed], [row[0] for row in keyed]


def _all_headwords_list() -> list[str]:
    """Every distinct simplified headword across all sources, sorted and cached.

    A prebuilt search index (scripts/build_search_index.py) answers this in a
    second or two. Without it, the list is built from the source databases -
    that is correct but slow on a phone (tens of seconds of OpenCC work), so
    warm_caches() no longer does it at startup.
    """
    global _all_headwords_cache
    if _all_headwords_cache is not None:
        return _all_headwords_cache[0]
    loaded = _load_headwords_from_index()
    if loaded is not None:
        _all_headwords_cache = loaded
        return loaded[0]
    return _build_all_headwords_list()


def _build_all_headwords_list() -> list[str]:
    """Build the "*" headword list from the source databases (uncached read).

    Simplified forms are collected (via OpenCC and the CEDICT map) so
    traditional/simplified pairs collapse to one entry - otherwise a page of 50
    raw headwords would display fewer rows after the pairs are merged.
    """
    global _all_headwords_cache
    connection = connect()
    try:
        parts = [
            "SELECT simplified AS headword FROM entries",
            "SELECT traditional FROM entries",
        ]
        # Which headwords actually have a Mandarin reading. Dialect-only
        # readings are collected separately below and become tier 4.
        has_reading: set[str] = {
            row[0]
            for row in connection.execute(
                "SELECT simplified FROM entries WHERE pinyin <> ''"
            )
        }
        # Dialect readings that can stand in for a missing Mandarin reading.
        dialect_raw: set[str] = set()
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
        if MOE_REVISED_DB_PATH.exists():
            # The comprehensive dictionary adds headwords the concise one lacks.
            _attach_database(connection, "moe_rev", MOE_REVISED_DB_PATH)
            parts.append(
                "SELECT DISTINCT word FROM moe_rev.senses WHERE text <> ''"
            )
            has_reading.update(
                to_simplified(row[0])
                for row in connection.execute(
                    "SELECT word FROM moe_rev.moe WHERE pinyin <> ''"
                )
            )
            def_words.update(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT word FROM moe_rev.senses WHERE text <> ''"
                )
            )
        if WIKTIONARY_DB_PATH.exists():
            _attach_database(connection, "wikt", WIKTIONARY_DB_PATH)
            # A Wiktionary headword with no gloss has nothing to show; leave it
            # out (the frequency list has no definitions either, so it is not a
            # headword source here).
            parts.append(
                "SELECT DISTINCT word FROM wikt.wiktionary WHERE gloss IS NOT NULL"
            )
            has_reading.update(
                to_simplified(row[0])
                for row in connection.execute(
                    "SELECT word FROM wikt.readings "
                    "WHERE pinyin <> '' AND kind = 'mandarin'"
                )
            )
            # Dialect-only readings (Wu, Xiang, ...) become tier 4. The app
            # only falls back to one when the word has no Mandarin reading, so
            # subtract the Mandarin set later.
            dialect_raw.update(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT word FROM wikt.readings "
                    "WHERE pinyin <> '' AND kind <> 'mandarin'"
                )
            )
            def_words.update(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT word FROM wikt.wiktionary "
                    "WHERE gloss IS NOT NULL"
                )
            )
        if ZHWIKTIONARY_DB_PATH.exists():
            _attach_database(connection, "zhwikt", ZHWIKTIONARY_DB_PATH)
            # Chinese Wiktionary headwords, plus their readings and definitions.
            parts.append("SELECT DISTINCT word FROM zhwikt.headwords WHERE word <> ''")
            has_reading.update(
                to_simplified(row[0])
                for row in connection.execute(
                    "SELECT word FROM zhwikt.headwords WHERE pinyin <> ''"
                )
            )
            def_words.update(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT word FROM zhwikt.senses WHERE text <> ''"
                )
            )
        if JPWIKTIONARY_DB_PATH.exists():
            _attach_database(connection, "jpwikt", JPWIKTIONARY_DB_PATH)
            # Japanese Wiktionary headwords, plus their readings and definitions.
            parts.append("SELECT DISTINCT word FROM jpwikt.headwords WHERE word <> ''")
            has_reading.update(
                to_simplified(row[0])
                for row in connection.execute(
                    "SELECT word FROM jpwikt.headwords WHERE pinyin <> ''"
                )
            )
            def_words.update(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT word FROM jpwikt.senses WHERE text <> ''"
                )
            )
        # Use the CEDICT map so variants OpenCC misses (e.g. 寳 -> 宝,
        # 砲 -> 炮) still collapse onto their simplified form, and therefore
        # get the same tier as the word they belong to.
        fold = _build_fold(
            connection.execute("SELECT traditional, simplified FROM entries")
        )
        union = " UNION ".join(parts)
        rows = connection.execute(f"SELECT DISTINCT headword FROM ({union})").fetchall()

        # Fold the three lookup sets with the same function as the headwords.
        # dialect_words is "has a dialect reading and no Mandarin one", which is
        # exactly when the app shows a tier-4 reading.
        has_reading = {fold(word) for word in has_reading}
        def_words = {fold(word) for word in def_words}
        dialect_words = {fold(word) for word in dialect_raw} - has_reading

        freq_rank: dict[str, int] = {}
        if FREQ_DB_PATH.exists():
            freq_rank = {
                row[0]: row[1]
                for row in connection.execute("SELECT word, rank FROM subtlex.freq")
            }
    finally:
        connection.close()
    seen: set[str] = set()
    heads: list[str] = []
    for (head,) in rows:
        key = fold(head)
        if key and key not in seen:
            seen.add(key)
            heads.append(key)

    heads, groups = _order_headwords(
        heads, def_words, has_reading, dialect_words, freq_rank
    )
    _all_headwords_cache = (heads, groups)
    return heads


def all_headword_groups() -> list[int]:
    """The tier group of every headword, parallel to _all_headwords_list().

    Exposed for tests: it lets a test assert the cached "*" order is tier-first
    without decorating 280k entries.
    """
    _all_headwords_list()
    return _all_headwords_cache[1] if _all_headwords_cache else []


def all_headwords(offset: int = 0, limit: int = ALL_PAGE_SIZE) -> tuple[list[str], int]:
    """One page of every distinct headword across all sources, plus the total.

    Used by the "*" search. A bare "*" fits every headword, so match tiers 1-3 do
    not apply, but tiers 4-6 do: words with a definition come first, then
    dialect-only readings (tier 4), then words with a reading but no definition
    (tier 5), then words with neither (tier 6). Inside each tier the list is
    ordered by frequency (missing frequencies last). The order is fixed once the
    list is cached, so paging is stable.
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
    if not has_cjk_or_glyph(word):
        return []
    return _decorate(
        _source_only_entries(word, limit=1, exact=True, allow_no_gloss=True)
    )


def prefix_entries(query: str, anchor: str, limit: int = 25) -> list[dict]:
    """Headwords that START WITH `query` from one dictionary, exact first.

    Used by the primary (fast) search tier for Chinese queries: instead of
    always previewing CEDICT, preview the first dictionary for the selected
    search language (see default_dictionaries.py). Only prefix matches are
    returned; words that merely contain the query are left to the full tier.
    """
    query = (query or "").strip()
    if not query:
        return []
    forms = _script_forms(query)
    exact = set(forms)

    if anchor == "cedict":
        clauses = []
        params: list[str] = []
        for form in forms:
            pattern = _like_pattern(f"{form}*")
            clauses.append(
                "(simplified LIKE ? ESCAPE '\\' OR traditional LIKE ? ESCAPE '\\')"
            )
            params += [pattern, pattern]
        exact_clause = "(" + " OR ".join(
            "(simplified = ? OR traditional = ?)" for _ in forms
        ) + ")"
        exact_params: list[str] = []
        for form in forms:
            exact_params += [form, form]
        order = [exact_clause + " DESC"] + _freq_order() + [
            "length(simplified)",
            "id",
        ]
        connection = connect()
        try:
            rows = _fetchall(
                connection,
                "SELECT simplified, traditional, pinyin, english, english_normalized "
                "FROM entries WHERE "
                + " OR ".join(clauses)
                + " ORDER BY "
                + ", ".join(order)
                + " LIMIT ?",
                [*params, *exact_params, limit],
                "primary_prefix",
            )
        finally:
            connection.close()
        return _decorate([dict(row) for row in rows])

    source = next((item for item in _SOURCE_QUERIES if item["anchor"] == anchor), None)
    if (
        source is None
        or not source["database"].exists()
        or not source.get("prefix_sql")
    ):
        return []

    connection = _connect(source["database"])
    try:
        found: dict[str, tuple] = {}
        for form in forms:
            pattern = _like_pattern(f"{form}*")
            for word, traditional, simplified, pinyin in connection.execute(
                source["prefix_sql"], (pattern,)
            ):
                if word and word not in found:
                    found[word] = (word, traditional, simplified, pinyin)
    finally:
        connection.close()

    entries = [
        {
            "simplified": simplified or to_simplified(word),
            # Keep the dictionary's own spelling; convert only when it has none.
            "traditional": traditional or word or to_traditional(word),
            "pinyin": pinyin or "",
            "english": "",
            "english_normalized": "",
        }
        for word, traditional, simplified, pinyin in found.values()
    ]
    _attach_freq(entries)
    entries.sort(
        key=lambda entry: (
            entry["simplified"] not in exact and entry["traditional"] not in exact,
            entry.get("freq") is None,
            entry["freq"]["rank"] if entry.get("freq") else 0,
            len(entry["simplified"]),
            entry["simplified"],
        )
    )
    return _decorate(entries[:limit])


# Per-dictionary reading index for the fast pinyin preview: anchor ->
# (stamp, toneless keys, rows) with the rows sorted by toneless reading.
_anchor_pinyin_cache: dict[str, tuple] = {}


def _anchor_pinyin_sorted(anchor: str) -> tuple[list[str], list[tuple]]:
    """Readings of ONE dictionary as sorted (toneless keys, rows).

    Rows are (word, simplified, marked, toneless, numeric), the same shape as
    _source_pinyin_rows(), but limited to a single source so a search language
    can preview its own dictionary (TW -> MOE) instead of always CC-CEDICT.
    Cached until that dictionary's database file changes.
    """
    source = next((item for item in _SOURCE_QUERIES if item["anchor"] == anchor), None)
    if (
        source is None
        or not source.get("pinyin_rows_sql")
        or not source["database"].exists()
    ):
        return [], []
    stamp = source["database"].stat().st_mtime
    cached = _anchor_pinyin_cache.get(anchor)
    if cached is not None and cached[0] == stamp:
        return cached[1], cached[2]
    rows: list[tuple[str, str, str, str, str]] = []
    connection = _connect(source["database"])
    try:
        for record in connection.execute(source["pinyin_rows_sql"]):
            word = record[0]
            simplified = record[1] if len(record) > 2 else to_simplified(word)
            pinyin = record[-1]
            if not word or not simplified or not pinyin:
                continue
            marked = _split_pinyin(pinyin) if source.get("split_pinyin") else pinyin
            toneless = to_toneless(marked)
            if not toneless:
                continue
            rows.append((word, simplified, marked, toneless, to_numeric(marked)))
    finally:
        connection.close()
    rows.sort(key=lambda row: row[3])
    keys = [row[3] for row in rows]
    _anchor_pinyin_cache[anchor] = (stamp, keys, rows)
    return keys, rows

def _freq_key(entry: dict) -> tuple:
    """Sort key for frequency: missing frequency last, then rank ascending."""
    return (
        entry.get("freq") is None,
        entry["freq"]["rank"] if entry.get("freq") else 0,
    )




def pinyin_prefix_entries(query: str, anchor: str, limit: int = 25) -> list[dict]:
    """Headwords from one dictionary whose READING starts with a pinyin query.

    The pinyin counterpart of prefix_entries(): the fast tier previews the first
    dictionary of the selected search language. Whole readings come first, then
    readings that start with the query, then frequency. A tone digit is honoured
    exactly as in search(). Returns [] when the query is not pinyin or the
    dictionary has no such reading, so the caller can fall back to CC-CEDICT.
    """
    query = (query or "").strip().lower()
    if not query:
        return []
    if any(0x3100 <= ord(char) <= 0x312F for char in query):
        converted = _zhuyin_to_numeric(query)
        if converted:
            query = converted
    parsed = _parse_pinyin_query(query)
    if parsed:
        letters, tones, boundaries = parsed
    else:
        letters = to_toneless(query)
        tones, boundaries = {}, set()
    if not letters or re.fullmatch(r"[a-zü]+", letters) is None:
        return []
    keys, rows = _anchor_pinyin_sorted(anchor)
    if not rows:
        return []
    found: dict[str, tuple[int, tuple]] = {}
    for row in _prefix_rows(keys, rows, letters):
        if parsed:
            rank = _reading_rank(row[4], letters, tones, boundaries)
            if rank is None or rank > 1:
                continue
        else:
            rank = 0 if row[3] == letters else 1
        word = row[0]
        if word not in found or rank < found[word][0]:
            found[word] = (rank, row)
    if not found:
        return []
    entries = []
    for rank, row in found.values():
        entries.append(
            {
                "simplified": row[1],
                "traditional": row[0],
                "pinyin": row[2],
                "english": "",
                "english_normalized": "",
                "_search_rank": rank,
            }
        )
    _attach_freq(entries)
    entries.sort(
        key=lambda entry: (
            entry["_search_rank"],
            *_freq_key(entry),
            len(entry["simplified"]),
            entry["simplified"],
        )
    )
    top = _decorate(entries[:limit])
    for entry in top:
        entry.pop("_search_rank", None)
    return top


def _anchor_defined_words(anchor: str, words: list[str]) -> set[str]:
    """The subset of `words` that one dictionary gives a definition.

    Uses the source's `senses` table rather than only its headword list, so a
    Wiktionary headword with no gloss does not count as defined. Wiktionary also
    stores some glosses under a different traditional spelling of the same
    simplified word (黨群 vs 黨羣), so its readings table is checked too.
    """
    source = next((item for item in _SOURCE_QUERIES if item["anchor"] == anchor), None)
    if source is None or not source["database"].exists() or not words:
        return set()
    found: set[str] = set()
    connection = _connect(source["database"])
    try:
        for start in range(0, len(words), 400):
            chunk = words[start : start + 400]
            holes = ",".join("?" * len(chunk))
            try:
                rows = connection.execute(
                    source["sense_sql"].format(ph=holes), tuple(chunk)
                ).fetchall()
                found.update(row[0] for row in rows)
            except sqlite3.Error:
                # Older source databases may predate the senses table.
                pass
            if anchor == "wiktionary":
                try:
                    rows = connection.execute(
                        "SELECT DISTINCT r.simplified FROM readings r "
                        "JOIN wiktionary w ON w.word = r.word "
                        f"WHERE r.simplified IN ({holes}) "
                        "AND w.gloss IS NOT NULL",
                        tuple(chunk),
                    ).fetchall()
                    found.update(row[0] for row in rows)
                except sqlite3.Error:
                    pass
    finally:
        connection.close()
    return found


# Per-dictionary "does a definition contain the query" lookups, keyed by anchor.
# `{ph}` is the IN (...) list of headwords; the one `?` is the LIKE pattern.
# CC-CEDICT is not listed: its rows already carry their English text, so the
# match is tested in Python (see _definition_match_anchors).
_SENSE_TEXT_MATCH_SQL = (
    "SELECT DISTINCT word FROM senses WHERE word IN ({ph}) "
    "AND lower(text) LIKE ? ESCAPE '\\'"
)
_DEFINITION_MATCH_SQL = {
    "wiktionary": (
        "SELECT DISTINCT word FROM wiktionary WHERE word IN ({ph}) "
        "AND gloss IS NOT NULL AND lower(gloss) LIKE ? ESCAPE '\\'"
    ),
}


def _definition_needle(query: str) -> str:
    """The text a definition search looks for: the query without its quotes."""
    text = (query or "").strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return text.strip().lower()


def _definition_match_anchors(entries: list[dict], query: str) -> list[set[str]]:
    """For each row, the dictionaries whose DEFINITIONS contain the query.

    A definition search ("word", or any query that matched no headword or
    reading) finds a row because some dictionary's definition text contains the
    query. Which dictionary that was decides whether the row belongs to the
    selected dictionaries or to "Other Dictionaries": a headword that merely
    exists in a selected dictionary (字 in the MOE dictionary) says nothing
    about the definition that matched (CC-CEDICT's "word"). A row nothing could
    be verified for gets an empty set; the caller keeps its older rules for it.
    """
    needle = _definition_needle(query)
    matches: list[set[str]] = [set() for _ in entries]
    if not needle or not entries:
        return matches
    pattern_re = _query_regex(needle)
    for i, entry in enumerate(entries):
        text = entry.get("english_normalized") or entry.get("english") or ""
        if text and pattern_re.search(text.lower()):
            matches[i].add("cedict")
    like = _like_pattern(f"*{needle}*")
    words = sorted(
        {entry["traditional"] for entry in entries if entry.get("traditional")}
        | {entry["simplified"] for entry in entries if entry.get("simplified")}
    )
    for source in _SOURCE_QUERIES:
        anchor = source["anchor"]
        if anchor == "cedict" or not source["database"].exists():
            continue
        sql = _DEFINITION_MATCH_SQL.get(anchor, _SENSE_TEXT_MATCH_SQL)
        found: set[str] = set()
        connection = _connect(source["database"])
        try:
            for chunk in _chunks(words):
                holes = ",".join("?" * len(chunk))
                try:
                    rows = connection.execute(
                        sql.format(ph=holes), (*chunk, like)
                    ).fetchall()
                except sqlite3.Error:
                    # Older source databases may predate the senses table.
                    break
                found.update(row[0] for row in rows)
        finally:
            connection.close()
        if not found:
            continue
        for i, entry in enumerate(entries):
            if entry.get("traditional") in found or entry.get("simplified") in found:
                matches[i].add(anchor)
    return matches


_NON_PINYIN_LETTER_RE = re.compile(r"[^a-zü]")


def _client_toneless(text: str) -> str:
    """Toneless letters of `text`, exactly like toToneless() in static/app.js.

    Lower-case, marked vowels to their base letter, then drop everything that
    is not a-z or ü. (pinyin.to_toneless also maps v to ü and drops digits, so
    it is not used here: the server and the page must agree on every tier.)
    """
    return _NON_PINYIN_LETTER_RE.sub("", (text or "").lower().translate(_MARK_TO_BASE))


def _glob_match(pattern: str, word: str) -> bool:
    """True when `word` matches the WHOLE of `pattern`.

    A "glob" is a pattern with two special characters: '*' stands for any run
    of characters (including none) and '?' stands for exactly one character.
    This is the same rule as the SQL LIKE pattern from _like_pattern(), but it
    runs in Python so rows from the secondary dictionaries get the same tier as
    CC-CEDICT rows. It walks both strings once and only ever falls back to the
    most recent '*', so a pattern full of stars cannot blow up the way a regex
    can. static/app.js has a copy (globMatch); keep the two in step.
    """
    p = w = 0
    star = -1  # index of the last '*' seen in the pattern
    mark = 0  # where in the word that '*' currently stops swallowing
    while w < len(word):
        if p < len(pattern) and pattern[p] == "*":
            star, mark = p, w
            p += 1
        elif p < len(pattern) and (pattern[p] == "?" or pattern[p] == word[w]):
            p += 1
            w += 1
        elif star >= 0:
            # Let the last '*' swallow one more character and try again.
            mark += 1
            p, w = star + 1, mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def _is_wildcard_query(query: str) -> bool:
    """True for an unquoted '*' / '?' query (full-width forms too).

    Mirrors isWildcardQuery() in static/app.js and the wildcard branch of
    main.py's api_search.
    """
    text = (query or "").strip()
    quoted = len(text) >= 2 and text.startswith('"') and text.endswith('"')
    return any(c in text for c in "*?＊？") and not quoted


def _wildcard_tier(words: list[str], query: str) -> int | None:
    """Match tier 1-3 of a Chinese wildcard pattern against a row's headwords.

    1 exact: the whole word fits the pattern (`*后` and `?后` fit 以后 and 后,
       but `?后` needs exactly two characters and fits 以后 only).
    2 starts with: the pattern fits the beginning of the word (售后服务 starts
       with 售后, so it is tier 2 for both `?后` and `*后`).
    3 contains: the pattern fits somewhere later inside the word.
    None when the pattern does not fit anywhere. The best tier over the
    simplified and traditional spelling wins.
    """
    return _wildcard_tier_normalized(
        words, _normalize_wildcards(query.strip()).lower()
    )


def _wildcard_tier_normalized(words: list[str], pattern: str) -> int | None:
    """_wildcard_tier with the pattern already normalized and lowercased."""
    best: int | None = None
    for word in words:
        word = word.lower()
        # A prefix match is the whole-word match of "pattern + any ending";
        # a contains match is "any start + pattern + any ending".
        for tier, glob in ((1, pattern), (2, pattern + "*"), (3, "*" + pattern + "*")):
            if _glob_match(glob, word):
                best = tier if best is None else min(best, tier)
                break
    return best


def _match_context(query: str) -> tuple[str, bool, str | None, str]:
    """The query-side values every _match_tier call for one search needs.

    rank_by_category ranks thousands of rows for one query. Recomputing
    has_cjk(), the wildcard normalization and the toneless key for every row
    was a measurable part of a slow search on the tablet.
    """
    text = (query or "").strip()
    if not text:
        return "", False, None, ""
    is_cjk = has_cjk(text)
    wildcard = (
        _normalize_wildcards(text).lower() if _is_wildcard_query(text) else None
    )
    return text, is_cjk, wildcard, "" if is_cjk else _client_toneless(text)


def _match_tier(entry: dict, query: str, taiwan: bool) -> int | None:
    """Match tier 1-3 of one row (1 exact, 2 starts with, 3 contains).

    Mirrors matchTier() in static/app.js: a Chinese query compares with the
    headwords, any other query with the reading the row shows (the Taiwan
    reading when the Taiwan toggle is on). None when nothing matches.
    """
    return _match_tier_context(entry, *_match_context(query), taiwan)


def _match_tier_context(
    entry: dict,
    text: str,
    is_cjk: bool,
    wildcard: str | None,
    client_key: str,
    taiwan: bool,
) -> int | None:
    """_match_tier with the query side precomputed by _match_context."""
    if not text:
        return None
    if is_cjk:
        words = [w for w in (entry.get("simplified"), entry.get("traditional")) if w]
        if wildcard is not None:
            # A Chinese "*" / "?" pattern is matched as a pattern, not as text.
            return _wildcard_tier_normalized(words, wildcard)
        if any(word == text for word in words):
            return 1
        if any(word.startswith(text) for word in words):
            return 2
        if any(text in word for word in words):
            return 3
        return None
    if not client_key:
        return None
    shown = entry.get("pinyin")
    if taiwan:
        shown = entry.get("pinyin_taiwan") or entry.get("pinyin_numeric") or shown
    reading = _client_toneless(shown or "")
    if reading == client_key:
        return 1
    if reading.startswith(client_key):
        return 2
    return 3 if client_key in reading else None


def rank_by_category(
    entries: list[dict],
    selected: Collection[str],
    query: str = "",
    taiwan: bool = False,
    definition_search: bool = False,
) -> list[dict]:
    """Order a whole result list the way the page orders one page, across all pages.

    Category is the primary key, as the README says ("Results within the same
    category are sorted by tier"):

    1. a definition in a selected dictionary,
    2. a definition only in a dictionary that is not selected,
    3. no definition anywhere.

    Tier is the secondary key (entryTier() in static/app.js): 1 exact, 2 starts
    with, 3 contains, 4 only a dialect reading, 5 a reading but no definition,
    6 neither. Category always wins, so a category 1 dialect row (tier 4)
    beats a category 2 exact match (tier 1). Inside one (category, tier) the
    old section order is kept: reading, no reading, IDS, dialect.

    The server pages before the page sorts, so it has to sort the whole list by
    that same key first. Otherwise each page is sorted on its own and a green
    (tier 3) row can sit at the top of page 47 while red and yellow rows are
    still waiting on later pages. The sort is stable, so inside one
    (category, tier, section) the search's own order (frequency) is kept.

    "Has a definition" is approximated from each dictionary's own headword
    table (the `sql` used for source chips): CC-CEDICT rows carry their English
    text, every other dictionary is looked up in batches.
    """
    if not entries:
        return entries
    # The caller keeps the user's drag order for the fast preview. Ranking only
    # needs membership, so use a set here and keep `selected` ordered outside.
    selected = set(selected)
    words = sorted(
        {entry["traditional"] for entry in entries if entry.get("traditional")}
        | {entry["simplified"] for entry in entries if entry.get("simplified")}
    )

    def listed(entry: dict, defined: set[str]) -> bool:
        return entry.get("traditional") in defined or entry.get("simplified") in defined

    has_cedict = [bool(entry.get("english")) for entry in entries]
    category1 = [
        "cedict" in selected and has_cedict[i] for i in range(len(entries))
    ]
    other_selected = [anchor for anchor in selected if anchor != "cedict"]
    other_defined = {
        anchor: _anchor_defined_words(anchor, words) for anchor in other_selected
    }
    for i, entry in enumerate(entries):
        if not category1[i]:
            category1[i] = any(listed(entry, d) for d in other_defined.values())
    # Rows outside category 1 with no CEDICT text may still have a definition
    # in a dictionary that is not selected; that decides tier 5/6 vs. category 2.
    need_any = [
        i for i, entry in enumerate(entries) if not category1[i] and not has_cedict[i]
    ]
    any_defined: set[str] = set()
    if need_any:
        probe = sorted(
            {
                word
                for i in need_any
                for word in (entries[i].get("traditional"), entries[i].get("simplified"))
                if word
            }
        )
        for source in _SOURCE_QUERIES:
            if source["anchor"] in other_defined:
                any_defined |= other_defined[source["anchor"]] & set(probe)
            else:
                any_defined |= _anchor_defined_words(source["anchor"], probe)

    # An English definition search arrives already ordered by relevance.py.
    # Keep that order inside a category; only annotate the category the page
    # needs for its "Other Dictionaries" heading and keep the relevance tier.
    if definition_search and any(entry.get("_relevance") for entry in entries):
        # A row found by its definition belongs to the selected dictionaries
        # only when a SELECTED dictionary's definition contains the query. That
        # a selected dictionary also lists the headword is irrelevant: with
        # Taiwan selected, "word" matches CC-CEDICT's English for 字, and 字
        # being in the MOE dictionary must not pull it out of "Other
        # Dictionaries". Headword matches (the Latin tier-0 rows) and rows whose
        # definition match cannot be verified keep the headword rule above.
        matched = _definition_match_anchors(entries, query)
        for i, entry in enumerate(entries):
            if entry.pop("_head_match", False) or not matched[i]:
                continue
            category1[i] = bool(matched[i] & selected)
        ranked: list[tuple[int, int, dict]] = []
        for i, entry in enumerate(entries):
            defined = (
                category1[i]
                or has_cedict[i]
                or bool(matched[i])
                or listed(entry, any_defined)
            )
            category = 1 if category1[i] else 2 if defined else 3
            entry["category"] = category
            if matched[i]:
                # The dictionaries that matched, so the page can show those
                # definitions for a row that lives under "Other Dictionaries".
                entry["match_anchors"] = sorted(matched[i])
            entry.pop("_relevance", None)
            ranked.append((category, i, entry))
        # Category is the primary key. The sort is stable on the original
        # index, so relevance order is kept inside each category.
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [entry for _category, _i, entry in ranked]

    # The query side is the same for every row; compute it once.
    text, is_cjk, wildcard, client_key = _match_context(query)

    def key(index: int) -> tuple[int, int, int]:
        entry = entries[index]
        has_reading = bool(entry.get("pinyin"))
        dialect = bool(entry.get("pinyin_label") or entry.get("pinyin_no_tone"))
        defined = (
            category1[index] or has_cedict[index] or listed(entry, any_defined)
        )
        if dialect:
            tier = 4
        elif not defined:
            tier = 5 if has_reading else 6
        else:
            tier = (
                _match_tier_context(
                    entry, text, is_cjk, wildcard, client_key, taiwan
                )
                or 3
            )
        # Category is the primary key, so it always beats the tier.
        if category1[index]:
            category = 1
        elif defined:
            category = 2
        else:
            category = 3
        # Section order inside one (category, tier), as render() lists it:
        # reading, no reading, IDS, dialect.
        ids = "\u2ff0" in (entry.get("simplified") or "") or "\u2ff0" in (
            entry.get("traditional") or ""
        )
        sub = (2 if ids else 0 if has_reading else 1) if not dialect else 3
        return (category, tier, sub)

    for entry in entries:
        # Only the definition branch above reads this marker.
        entry.pop("_head_match", None)
    keys = [key(index) for index in range(len(entries))]
    order = sorted(range(len(entries)), key=keys.__getitem__)
    ranked: list[dict] = []
    for index in order:
        entry = entries[index]
        category, tier, _sub = keys[index]
        # Send the same values the page uses, so it does not have to recompute
        # (and cannot disagree with the server order).
        entry["category"] = category
        entry["tier"] = tier
        ranked.append(entry)
    return ranked


def group_rows(rows: list[dict]) -> list[list[dict]]:
    """Group rows that share a simplified headword, in first-seen order.

    A raw result list can hold several spellings that display as the same
    simplified form (了/瞭, 和/咊). Grouping before paging keeps all of them on
    one page, so the browser's merge cannot produce the same word on two pages
    and the total counts merged words instead of raw rows.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = row.get("simplified") or row.get("traditional") or ""
        groups.setdefault(key, []).append(row)
    return list(groups.values())


def page_groups(
    rows: list[dict], offset: int, limit: int
) -> tuple[list[dict], int]:
    """One page of whole simplified-headword groups, plus the group count.

    The returned rows may be more than `limit` when a group holds several
    spellings; the browser merges them down to at most `limit` rows.
    """
    groups = group_rows(rows)
    page = groups[offset : offset + limit]
    for group in page:
        key = group[0].get("simplified") or group[0].get("traditional") or ""
        for row in group:
            row["group"] = key
    return [row for group in page for row in group], len(groups)


def wildcard_candidates(query: str) -> list[dict] | None:
    """Every row a Chinese "*" / "?" pattern fits, lightly decorated.

    Returns None when the query is not a Chinese pattern (a pinyin pattern
    still goes through wildcard_search). Unlike wildcard_search this does not
    page: the caller ranks the whole list with rank_by_category() and then
    slices the page, so the order is the same on every page.

    The pattern may fit anywhere in the word (`?后` finds 以后, 售后服务 and
    以后再说), because rank_by_category() sorts exact matches, then words that
    start with a match, then words that merely contain one. The rows arrive
    sorted by frequency, which rank_by_category() keeps inside each tier.
    """
    text = (query or "").strip()
    if not any(c in text for c in "*?＊？"):
        return None
    chinese = to_simplified(text)
    literal = chinese
    for _c in ("*", "?", "＊", "？"):
        literal = literal.replace(_c, "")
    if not has_cjk(literal):
        return None
    # "*pattern*" lets the pattern fit anywhere in the headword.
    anywhere = _like_pattern(f"*{chinese}*")
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT simplified, traditional, pinyin, english, english_normalized "
            "FROM entries WHERE simplified LIKE ? ESCAPE '\\' "
            "ORDER BY length(simplified), id",
            (anywhere,),
        ).fetchall()
    finally:
        connection.close()
    # Headwords only the secondary dictionaries have are matched the same way
    # (the pattern is wrapped in stars so it can fit anywhere there too).
    source_rows = _source_only_entries(f"*{text}*", limit=_cap(500))
    entries: list[dict] = []
    seen: set[tuple] = set()
    for entry in [dict(row) for row in rows] + source_rows:
        if not has_cjk(entry["simplified"]):
            continue
        key = (entry["traditional"], entry.get("pinyin"))
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    # Attach the frequency, then order by it (missing last). The sort is
    # stable, so equal frequencies keep the shorter-word-first order above.
    _light_decorate(entries)
    entries.sort(key=_freq_key)
    return entries


def wildcard_search(
    query: str, limit: int = 50, offset: int = 0, taiwan: bool = False
) -> tuple[list[dict], int, bool]:
    """Paged results for a "*" or "?" pattern query.

    Returns (entries, total, pinyin_match). Chinese patterns match the headword;
    latin patterns match the pinyin reading, mirroring the normal wildcard
    search, with the same ordering but an OFFSET for pagination. In a Chinese
    query "?" matches exactly one Han character; in a pinyin query it matches
    exactly one whole syllable.
    """
    text = (query or "").strip()
    if not any(c in text for c in "*?＊？"):
        return [], 0, False
    literal = to_simplified(text)
    for _c in ("*", "?", "＊", "？"):
        literal = literal.replace(_c, "")
    is_chinese = has_cjk(literal)
    source_page: list[dict] = []
    connection = connect()
    try:
        freq = _freq_order()
        freq_clause = (", " + ", ".join(freq)) if freq else ""
        if is_chinese:
            chinese = to_simplified(text)
            pattern = _like_pattern(chinese)
            where = "simplified LIKE ? ESCAPE '\\'"
            count_params = (pattern,)
            order = (
                "(simplified = ?) DESC, "
                "(simplified LIKE ? ESCAPE '\\') DESC"
                f"{freq_clause}, length(simplified), id"
            )
            order_params = (literal, _like_pattern(f"{literal}*"))
            cedict_total = connection.execute(
                f"SELECT COUNT(*) FROM entries WHERE {where}", count_params
            ).fetchone()[0]
            # Source-only headwords matching the same wildcard are appended
            # after the CEDICT matches (one combined paged list).
            source_entries = _source_only_entries(text, limit=_cap(500))
            total = cedict_total + len(source_entries)
            if offset < cedict_total:
                rows = connection.execute(
                    "SELECT simplified, traditional, pinyin, english, english_normalized "
                    f"FROM entries WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                    (*count_params, *order_params, limit, offset),
                ).fetchall()
                source_start = 0
            else:
                rows = []
                source_start = offset - cedict_total
            source_take = limit - len(rows)
            if source_take > 0:
                source_page = source_entries[
                    source_start : source_start + source_take
                ]
            pinyin_match = False
        else:
            if "?" in text or "？" in text:
                # "?" means one whole syllable, which SQL LIKE cannot express.
                # Use a broad LIKE pattern to narrow the candidates, then check
                # the entry's syllable boundaries in Python.
                regex = _pinyin_wildcard_regex(text)
                if regex is None:
                    return [], 0, False
                pattern = _pinyin_wildcard_pattern(text)
                use_tw = taiwan and _taiwan_ready()
                if use_tw:
                    candidates = connection.execute(
                        "SELECT entries.simplified AS simplified, "
                        "entries.traditional AS traditional, "
                        "entries.pinyin AS pinyin, "
                        "COALESCE(tw.readings.tw_numeric, entries.pinyin_numeric) AS pinyin_numeric, "
                        "entries.english AS english, "
                        "entries.english_normalized AS english_normalized, "
                        f"entries.{FREQ_RANK_COLUMN} AS freq_rank "
                        "FROM entries JOIN tw.readings ON tw_source = 'cedict' "
                        "AND tw_word = entries.traditional "
                        "AND tw_mainland = entries.pinyin "
                        "WHERE tw.readings.tw_toneless LIKE ? ESCAPE '\\'",
                        (pattern,),
                    ).fetchall()
                else:
                    candidates = connection.execute(
                        "SELECT simplified, traditional, pinyin, pinyin_numeric, "
                        "english, english_normalized, "
                        f"{FREQ_RANK_COLUMN} AS freq_rank "
                        "FROM entries WHERE pinyin_toneless LIKE ? ESCAPE '\\'",
                        (pattern,),
                    ).fetchall()
                matched = []
                for candidate in candidates:
                    syllables = (candidate["pinyin_numeric"] or "").split()
                    key = "\x00".join(to_toneless(part) for part in syllables) + "\x00"
                    if regex.fullmatch(key):
                        matched.append(dict(candidate))
                matched.sort(
                    key=lambda entry: (
                        entry["freq_rank"] is None,
                        entry["freq_rank"] or 0,
                        len(entry["simplified"]),
                        entry["simplified"],
                    )
                )
                total = len(matched)
                rows = matched[offset : offset + limit]
                pinyin_match = True
            else:
                use_tw = taiwan and _taiwan_ready()
                pinyin_key = to_toneless(text)
                pattern = _like_pattern(
                    "*".join(to_toneless(part) for part in text.split("*"))
                )
                if use_tw:
                    where = "tw.readings.tw_toneless LIKE ? ESCAPE '\\'"
                    count_params = (pattern,)
                    order = (
                        "(tw.readings.tw_toneless LIKE ? ESCAPE '\\') DESC, "
                        "(tw.readings.tw_toneless = ?) DESC"
                        f"{freq_clause}, length(entries.simplified), entries.id"
                    )
                    order_params = (pattern, pinyin_key)
                    total = connection.execute(
                        "SELECT COUNT(*) FROM entries JOIN tw.readings ON "
                        "tw_source = 'cedict' AND tw_word = entries.traditional "
                        "AND tw_mainland = entries.pinyin "
                        f"WHERE {where}",
                        count_params,
                    ).fetchone()[0]
                    rows = connection.execute(
                        "SELECT entries.simplified AS simplified, "
                        "entries.traditional AS traditional, "
                        "entries.pinyin AS pinyin, "
                        "entries.english AS english, "
                        "entries.english_normalized AS english_normalized "
                        "FROM entries JOIN tw.readings ON tw_source = 'cedict' "
                        "AND tw_word = entries.traditional "
                        "AND tw_mainland = entries.pinyin "
                        f"WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                        (*count_params, *order_params, limit, offset),
                    ).fetchall()
                    pinyin_match = True
                else:
                    pinyin_key = to_toneless(text)
                    if not pinyin_key:
                        return [], 0, False
                    pattern = _like_pattern(
                        "*".join(to_toneless(part) for part in text.split("*"))
                    )
                    note_pattern = _like_pattern(f"*pr. [{pinyin_key}*")
                    where = f"pinyin_toneless LIKE ? ESCAPE '\\' OR {_note_or()}"
                    count_params = (pattern, note_pattern)
                    order = (
                        # Direct readings before note-only matches, then the whole
                        # reading, then frequency (a "Taiwan pr." note must not
                        # outrank a more common word with its own matching reading).
                        "(pinyin_toneless LIKE ? ESCAPE '\\') DESC, "
                        "(pinyin_toneless = ?) DESC"
                        f"{freq_clause}, length(simplified), id"
                    )
                    order_params = (pattern, pinyin_key)
                    total = connection.execute(
                        f"SELECT COUNT(*) FROM entries WHERE {where}", count_params
                    ).fetchone()[0]
                    rows = connection.execute(
                        "SELECT simplified, traditional, pinyin, english, english_normalized "
                        f"FROM entries WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                        (*count_params, *order_params, limit, offset),
                    ).fetchall()
                    pinyin_match = True
    finally:
        connection.close()

    entries: list[dict] = []
    seen: set[tuple] = set()
    for row in rows:
        entry = dict(row)
        if not has_cjk(entry["simplified"]):
            continue
        key = (entry["traditional"], entry["pinyin"])
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    for entry in source_page:
        if not has_cjk(entry["simplified"]):
            continue
        key = (entry["traditional"], entry.get("pinyin"))
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    return _decorate(entries), total, pinyin_match


# --- Chinese <-> Arabic numerals ------------------------------------------
# Headwords use both forms (21三体综合症, 二十一世纪, 廿一). Expand a query so
# a search for one form also finds the others.
_CN_DIGITS = "零一二三四五六七八九"
_CN_NUM_RE = re.compile(r"[零〇一二三四五六七八九十百千万亿两廿卅卌]+")
_ARABIC_NUM_RE = re.compile(r"\d+")


def _arabic_to_chinese(number: int) -> str:
    """Convert an integer to standard Chinese numerals (21 -> 二十一)."""
    if number == 0:
        return "零"
    if number < 0:
        return "负" + _arabic_to_chinese(-number)
    units = ("", "十", "百", "千")

    def section(value: int) -> str:
        text = ""
        zero = False
        for place in range(3, -1, -1):
            digit = (value // (10**place)) % 10
            if digit == 0:
                if text and not text.endswith("零"):
                    zero = True
                continue
            if zero:
                text += "零"
                zero = False
            text += _CN_DIGITS[digit] + units[place]
        return text

    if number < 10000:
        text = section(number)
    elif number < 100000000:
        high, low = divmod(number, 10000)
        text = section(high) + "万"
        if low:
            text += ("零" if low < 1000 else "") + section(low)
    else:
        high, low = divmod(number, 100000000)
        text = _arabic_to_chinese(high) + "亿"
        if low:
            text += ("零" if low < 10000000 else "") + _arabic_to_chinese(low)
    # 一十 -> 十 at the start (10, 11, 100000 -> 十万).
    if text.startswith("一十"):
        text = "十" + text[2:]
    return text


def _digitwise_chinese(value: str | int) -> str:
    """Read each digit separately (2026 -> 二零二六)."""
    return "".join(_CN_DIGITS[int(digit)] for digit in str(value))


def _abbreviated_chinese(number: int) -> str:
    """Use 廿/卅/卌 for 20s/30s/40s where that form is common."""
    standard = _arabic_to_chinese(number)
    for unit, base in (("廿", 20), ("卅", 30), ("卌", 40)):
        if base <= number < base + 10:
            rest = standard[-1] if number % 10 else ""
            return unit + rest
    return standard


def _liang_chinese(number: int) -> str:
    """Use 两 instead of 二 before a unit (两百, 两千, 两万)."""
    standard = _arabic_to_chinese(number)
    if number == 2:
        return "两"
    if (
        number >= 200
        and standard.startswith("二")
        and standard[1:2] in {"百", "千", "万", "亿"}
    ):
        return "两" + standard[1:]
    return standard


def _chinese_to_arabic(text: str) -> int | None:
    """Parse a standard or common variant Chinese numeral, or return None."""
    text = text.replace("〇", "零").replace("两", "二")
    text = text.replace("廿", "二十").replace("卅", "三十").replace("卌", "四十")
    if not text:
        return None
    allowed = _CN_DIGITS + "十百千万亿"
    if any(char not in allowed for char in text):
        return None
    # A digit string without units (二零二六) is read digit by digit.
    if not any(unit in text for unit in "十百千万亿"):
        return int("".join(str(_CN_DIGITS.index(char)) for char in text))

    def parse_section(section: str) -> int | None:
        total = 0
        number = 0
        for char in section:
            if char in _CN_DIGITS:
                number = _CN_DIGITS.index(char)
            elif char == "十":
                total += (number or 1) * 10
                number = 0
            elif char == "百":
                total += (number or 1) * 100
                number = 0
            elif char == "千":
                total += (number or 1) * 1000
                number = 0
            else:
                return None
        return total + number

    if "亿" in text:
        high, rest = text.split("亿", 1)
        high_value = parse_section(high) if high else 1
        if high_value is None:
            return None
        rest_value = _chinese_to_arabic(rest) if rest else 0
        if rest_value is None:
            return None
        return high_value * 100000000 + rest_value
    if "万" in text:
        high, rest = text.split("万", 1)
        high_value = parse_section(high) if high else 1
        if high_value is None:
            return None
        rest_value = _chinese_to_arabic(rest) if rest else 0
        if rest_value is None:
            return None
        return high_value * 10000 + rest_value
    return parse_section(text)


def _chinese_numeral_variants(number: int) -> set[str]:
    """All common spellings of one number that a headword might use."""
    standard = _arabic_to_chinese(number)
    digitwise = _digitwise_chinese(number)
    return {
        standard,
        digitwise,
        digitwise.replace("零", "〇"),
        _abbreviated_chinese(number),
        _liang_chinese(number),
    }


def _number_query_forms(text: str, limit: int = 12) -> list[str]:
    """Return the original query plus its Arabic/Chinese numeral variants."""
    text = (text or "").strip()
    if not text:
        return []
    forms = [text]

    def add(value: str) -> None:
        if value and value not in forms and len(forms) < limit:
            forms.append(value)

    if _ARABIC_NUM_RE.search(text):
        add(_ARABIC_NUM_RE.sub(lambda m: _arabic_to_chinese(int(m.group())), text))
        add(_ARABIC_NUM_RE.sub(lambda m: _digitwise_chinese(m.group()), text))
        add(
            _ARABIC_NUM_RE.sub(
                lambda m: _digitwise_chinese(m.group()).replace("零", "〇"), text
            )
        )
        add(_ARABIC_NUM_RE.sub(lambda m: _abbreviated_chinese(int(m.group())), text))
        add(_ARABIC_NUM_RE.sub(lambda m: _liang_chinese(int(m.group())), text))

    if _CN_NUM_RE.search(text):
        def convert(match: re.Match) -> str:
            run = match.group()
            number = _chinese_to_arabic(run)
            # Only convert a run that is actually a canonical numeral. This
            # leaves words like 万一 or 千万 alone.
            if number is None or run not in _chinese_numeral_variants(number):
                return run
            return str(number)

        arabic = _CN_NUM_RE.sub(convert, text)
        add(arabic)
        if arabic != text:
            add(_ARABIC_NUM_RE.sub(lambda m: _arabic_to_chinese(int(m.group())), arabic))
            add(_ARABIC_NUM_RE.sub(lambda m: _digitwise_chinese(m.group()), arabic))
            add(
                _ARABIC_NUM_RE.sub(
                    lambda m: _digitwise_chinese(m.group()).replace("零", "〇"),
                    arabic,
                )
            )
            add(_ARABIC_NUM_RE.sub(lambda m: _abbreviated_chinese(int(m.group())), arabic))
            add(_ARABIC_NUM_RE.sub(lambda m: _liang_chinese(int(m.group())), arabic))
    return forms


def headword_query_forms(query: str) -> list[str]:
    """Headword substrings to try for a query, including number variants.

    The form the user typed is kept as well as its OpenCC simplification, so a
    traditional query such as 乾 can keep the qián entries whose own simplified
    form is 乾 even when the simplified form 干 is also searched.
    """
    original = (query or "").replace("*", "").strip()
    text = to_simplified(original)
    if not text:
        return []
    forms = _number_query_forms(text)
    if original and original not in forms:
        forms.insert(0, original)
    return forms


# Parenthetical notes in CEDICT definitions, e.g. "to turn left (Shanghainese)".
_PAREN_NOTE_RE = re.compile(r"\([^)]*\)")

# Parentheses that actually name a language or dialect. Cross-references such
# as "[xiang4]" are not language notes, so only these parentheses are ignored
# when deciding whether a query occurs in a normal definition.
_LANGUAGE_NOTE_HINT_RE = re.compile(
    r"\b(?:dialect|dialectal|language|languages|"
    r"cantonese|shanghainese|hokkien|hakka|minnan|taiwanese|mandarin|"
    r"hunanese|teochew|chaozhou|swatow|amoy|sichuanese|northeastern)\b",
    re.IGNORECASE,
)

# Words that show a definition is describing the language itself, not a person
# or place whose name happens to contain the query.
_LANGUAGE_CONTEXT_RE = re.compile(
    r"\b(?:dialect|dialects|dialectal|language|languages|"
    r"romanization|mandarin)\b",
    re.IGNORECASE,
)

# Dialect/language names that mark a query as a language-note search. A query
# in this set gets definition-note matches ranked below normal definitions.
_LANGUAGE_QUERY_TERMS = frozenset(
    {
        "dialect",
        "dialectal",
        "regional",
        "language",
        "languages",
        "cantonese",
        "shanghainese",
        "hokkien",
        "hakka",
        "minnan",
        "taiwanese",
        "mandarin",
        "hunanese",
        "teochew",
        "chaozhou",
        "swatow",
        "amoy",
        "wu",
        "xiang",
        "gan",
        "jin",
        "hui",
        "pinghua",
        "min",
        "yue",
        "sichuan",
        "sichuanese",
        "beijing",
        "northeastern",
        "shandong",
        "henan",
        "hebei",
        "anhui",
        "zhejiang",
        "guangxi",
        "yunnan",
        "guizhou",
        "jiangxi",
        "jiangsu",
        "shaanxi",
        "shanxi",
        "guangdong",
        "canton",
    }
)

_LANGUAGE_SUFFIX_RE = re.compile(r"\s+dialect(?:al)?$", re.IGNORECASE)


def _is_language_query(query: str) -> bool:
    """True when the query names a language/dialect note term.

    Wildcard and quoted queries are left alone: only a plain name such as
    "shanghainese", "wu" or "sichuan dialect" triggers the note ranking.
    """
    text = (query or "").strip().lower()
    if not text or "*" in text or text.startswith('"'):
        return False
    text = _LANGUAGE_SUFFIX_RE.sub("", text)
    return text in _LANGUAGE_QUERY_TERMS


def _query_regex(query: str) -> re.Pattern:
    """A case-insensitive substring regex; '*' still acts as a wildcard."""
    return re.compile(re.escape(query.lower()).replace(r"\*", ".*"))


def _strip_language_notes(text: str) -> str:
    """Remove parentheses that name a language or dialect."""

    def replace(match: re.Match) -> str:
        note = match.group(0)
        return "" if _LANGUAGE_NOTE_HINT_RE.search(note) else note

    return _PAREN_NOTE_RE.sub(replace, text or "")


def _language_match_tier(text: str, query: str) -> int:
    """Rank a definition match for a language-name query.

    0 = the definition describes the language itself ("Xiang ... dialect"),
    1 = the query occurs only inside a language note, 2 = some other mention
    (a person or place name, for example).
    """
    if not text:
        return 2
    stripped = _strip_language_notes(text)
    if _query_regex(query).search(stripped.lower()):
        # The language words may sit in a note ("Sichuanese (a group of
        # Southwestern Mandarin dialects)"), so look at the full text here.
        return 0 if _LANGUAGE_CONTEXT_RE.search(text) else 2
    return 1


def _rank_language_matches(entries: list[dict], query: str) -> list[dict]:
    """Stable partition: language entries, then notes, then other mentions."""
    tiers: dict[int, list[dict]] = {0: [], 1: [], 2: []}
    for entry in entries:
        text = entry.get("english_normalized") or ""
        tiers[_language_match_tier(text, query)].append(entry)
    return tiers[0] + tiers[1] + tiers[2]


def _merge_unique(base: list[dict], extra: list[dict]) -> list[dict]:
    """Append `extra` entries that are not already in `base` (by headword)."""
    seen = {
        (entry.get("traditional"), entry.get("simplified")) for entry in base
    }
    merged = list(base)
    for entry in extra:
        key = (entry.get("traditional"), entry.get("simplified"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged


def _tw_cedict_rows(
    connection, letters: str, include: str, exclude: str | None, trigram: bool
) -> list:
    """CEDICT rows whose prefilled TAIWAN reading matches `letters`.

    `include` is a LIKE pattern over tw_toneless; `exclude` (optional) removes
    rows an earlier phase already returned. Rows carry the Taiwan numeric reading
    as `pinyin_numeric`, so the caller ranks on the reading the user searched.
    """
    if trigram:
        # `letters` is plain a-z/ü, so no LIKE escaping is needed.
        where = (
            "tw.readings.rowid IN (SELECT rowid FROM tw.readings_fts "
            "WHERE tw_toneless LIKE ?)"
        )
        params: tuple = (f"%{letters}%" if exclude is not None else f"{letters}%",)
        if exclude is not None:
            where += (
                " AND tw.readings.rowid NOT IN (SELECT rowid FROM tw.readings_fts "
                "WHERE tw_toneless LIKE ?)"
            )
            params += (f"{letters}%",)
    else:
        where = "tw_toneless LIKE ? ESCAPE '\\'"
        params = (include,)
        if exclude is not None:
            where += " AND tw_toneless NOT LIKE ? ESCAPE '\\'"
            params += (exclude,)
    return _fetchall(
        connection,
        "SELECT entries.id AS id, entries.simplified AS simplified, "
        "entries.traditional AS traditional, entries.pinyin AS pinyin, "
        "tw_numeric AS pinyin_numeric, 0 AS alt "
        f"FROM {_TW_CEDICT_FROM} WHERE tw_source = 'cedict' AND {where}",
        params,
        "pinyin_lookup_taiwan",
    )


def _search_core(
    query: str,
    limit: int = 50,
    taiwan: bool = False,
    part: str = "all",
    _skip_pinyin: bool = False,
    hydrate: int | None = None,
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

    `hydrate` turns on lazy hydration: only the first `hydrate` rows are fully
    decorated, and the rest get just the cheap fields used to rank and
    de-duplicate them (`_light_decorate`). Rows are fully decorated later by
    `decorate_entries` when they reach the page. `None` decorates everything,
    which is what `part="primary"`, fuzzy searches and the other callers want.
    """
    query = query.strip()
    if not query:
        return [], False  # nothing to search for
    # Keep the original casing for the relevance boost (proper nouns).
    original_query = query
    # Lowercase only pure Latin queries. A mixed headword such as BP机 must keep
    # its case for the case-sensitive LIKE headword lookup.
    if not has_cjk(query):
        query = query.lower()

    def decorate(rows: list[dict]) -> list[dict]:
        """Fully decorate the first `hydrate` rows, light-decorate the rest."""
        if hydrate is None or len(rows) <= hydrate:
            return _decorate(rows)
        head = rows[:hydrate]
        tail = rows[hydrate:]
        if head:
            _decorate(head)
        if tail:
            _light_decorate(tail)
        return head + tail

    # "Quoted" -> English phrase search only.
    if len(query) >= 2 and query.startswith('"') and query.endswith('"'):
        phrase = query[1:-1].strip()
        if not phrase:
            return [], False
        # The note and section labels are generated by the app, not stored in a
        # dictionary: a quoted phrase from one of them returns those entries.
        extra = _extra_label_entries(phrase, limit)
        if extra is not None:
            return decorate(extra), False
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
            rows = _fetchall(connection, sql, params, "definition_fts")
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
        source_rows = []
        seen_source = set(fts_keys)
        # A quoted Latin phrase must match a definition that is actually
        # displayed. The live Wiktionary gloss table contains extra senses for
        # common characters (the Taixuanjing tetragram named 文, for example)
        # that are not shown on the CEDICT entry, so searching it here made a
        # phrase match look unrelated. The prebuilt source-gloss index contains
        # the source-only entries whose gloss is displayed.
        if has_cjk(phrase):
            source_candidates = _source_definition_entries(
                phrase, limit=limit
            ) + _source_gloss_entries(phrase, limit=limit)
        else:
            source_candidates = _source_gloss_entries(phrase, limit=limit)
        for entry in source_candidates:
            key = entry["simplified"]
            if key in seen_source:
                continue
            seen_source.add(key)
            source_rows.append(entry)
        if not fts_unique and not source_rows:
            # A quoted Chinese headword or pinyin may not occur in any
            # definition. Fall back to the normal search so quoting does not
            # hide a word the dictionary knows.
            return search(phrase, limit, taiwan, part=part, hydrate=hydrate)
        combined = fts_unique + source_rows
        if os.environ.get("YABAI_RELEVANCE", "1") != "0":
            _attach_freq(combined)
            _attach_relevance(combined, phrase)
            combined = relevance.rank_entries(
                combined,
                phrase,
                boost_exact=phrase[:1].isupper() and phrase[:1].isalpha(),
            )
        decorated = decorate(combined)
        if part == "primary":
            return decorated[:limit], False
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
    parsed = None if _skip_pinyin else _parse_pinyin_query(query)
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
        # With the Taiwan toggle the candidate rows are chosen by their prefilled
        # TAIWAN reading (taiwan_readings.sqlite), so a word whose Taiwan reading
        # matches is fetched even if its mainland reading does not, and the other
        # way round. Without the table it falls back to the live derivation.
        use_tw = taiwan and _taiwan_ready()
        connection = connect()
        try:
            ranked: list[tuple] = []
            seen_ids: set[int] = set()
            phases = [(prefix, None)]
            if part != "primary" and (use_tw or not taiwan):
                phases.append((contains, prefix))
            # The trigram index answers the same substring test as LIKE, but
            # only once the query has enough characters to form a trigram.
            use_trigram = _has_cedict_trigram() and len(letters) >= 3
            tw_trigram = _taiwan_has_trigram() and len(letters) >= 3
            for pattern, exclude in phases:
                if use_tw:
                    rows = _tw_cedict_rows(
                        connection, letters, pattern, exclude, tw_trigram
                    )
                    for row in rows:
                        if row["id"] in seen_ids:
                            continue
                        seen_ids.add(row["id"])
                        rank = _reading_rank(
                            row["pinyin_numeric"], letters, tones, boundaries
                        )
                        if rank is not None:
                            ranked.append((rank, len(row["simplified"]), row["id"]))
                    if len(ranked) >= limit:
                        break
                    continue
                if use_trigram:
                    # `letters` is plain a-z/ü, so no LIKE escaping is needed.
                    if exclude is None:
                        where = (
                            "id IN (SELECT rowid FROM entries_fts_trigram "
                            "WHERE pinyin_toneless LIKE ?)"
                        )
                        params = (note, f"{letters}%", note_pattern)
                    else:
                        where = (
                            "id IN (SELECT rowid FROM entries_fts_trigram "
                            "WHERE pinyin_toneless LIKE ?) "
                            "AND id NOT IN (SELECT rowid FROM entries_fts_trigram "
                            "WHERE pinyin_toneless LIKE ?)"
                        )
                        params = (note, f"%{letters}%", f"{letters}%", note_pattern)
                elif exclude is None:
                    where = "pinyin_toneless LIKE ? ESCAPE '\\'"
                    params = (note, pattern, note_pattern)
                else:
                    where = (
                        "pinyin_toneless LIKE ? ESCAPE '\\' "
                        "AND pinyin_toneless NOT LIKE ? ESCAPE '\\'"
                    )
                    params = (note, pattern, exclude, note_pattern)
                rows = _fetchall(
                    connection,
                    "SELECT id, simplified, traditional, pinyin, pinyin_numeric, "
                    "instr(english_normalized, ?) > 0 AS alt FROM entries "
                    f"WHERE {where} OR {_note_or()}",
                    params,
                    "pinyin_lookup",
                )
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
        if part == "primary":
            source_rows = []
        else:
            with pd.stage("source_pinyin"):
                source_rows = _source_pinyin_entries(query, limit=limit)
        # Keep the CEDICT order intact. Source prefixes only move ahead of
        # the first CEDICT row that merely contains the query; source
        # contains-only rows stay at the end.
        def pinyin_rank(entry: dict) -> int:
            reading = to_toneless(entry.get("pinyin", ""))
            if reading == letters:
                return 0
            if reading.startswith(letters):
                return 1
            return 2

        source_prefix = [e for e in source_rows if pinyin_rank(e) <= 1]
        source_contains = [e for e in source_rows if pinyin_rank(e) == 2]
        insert_at = next(
            (i for i, entry in enumerate(top) if pinyin_rank(entry) == 2),
            len(top),
        )
        combined = (
            top[:insert_at]
            + source_prefix
            + top[insert_at:]
            + source_contains
        )
        decorated = decorate(combined)
        # Prefer the reading with the most senses when rank and frequency tie
        # (德 should show dé before Dé), without reordering different words.
        _sense_words = sorted({e.get("simplified") for e in combined if e.get("simplified")})
        _sense_counts = _cedict_reading_counts(_sense_words) if _sense_words else {}
        for entry in decorated:
            entry["_sense_count"] = _entry_sense_count(_sense_counts, entry)
        _group_order = {}
        for _i, _e in enumerate(decorated):
            if _e.get("simplified") not in _group_order:
                _group_order[_e.get("simplified")] = _i
        decorated.sort(
            key=lambda entry: (
                _group_order.get(entry.get("simplified"), 0),
                -entry.get("_sense_count", 0),
            )
        )
        for entry in decorated:
            entry.pop("_search_rank", None)
            entry.pop("_sense_count", None)
        if part == "primary":
            return decorated[: len(top)], True
        return decorated, True

    # Exact pinyin reading (e.g. "yabai" -> 牙白): if the query is a complete
    # Chinese pronunciation, show only those matches and skip English results.
    # A '*' makes it a toneless pattern ("*xinu*" -> 喜怒哀樂); an apostrophe (or
    # space) pins syllable boundaries ("dan'gan" -> 单干, not 档案).
    wildcard = any(c in query for c in "*＊")
    segments = [to_toneless(part) for part in re.split(r"['’\s]+", query) if part]
    pinyin_key = "" if _skip_pinyin else to_toneless(query)
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
            # A plain Latin pinyin query can use the trigram index; a wildcard
            # or a short query stays on the LIKE scan.
            use_trigram = (
                _has_cedict_trigram()
                and not wildcard
                and len(pinyin_key) >= 3
                and re.fullmatch(r"[a-zü]+", pinyin_key) is not None
            )
            if use_trigram:
                pinyin_clause = (
                    "id IN (SELECT rowid FROM entries_fts_trigram "
                    "WHERE pinyin_toneless LIKE ?)"
                )
                pinyin_params = (f"{pinyin_key}%",)
                direct_pattern = f"{pinyin_key}%"
            else:
                pinyin_clause = "pinyin_toneless LIKE ? ESCAPE '\\'"
                pinyin_params = (pattern,)
                direct_pattern = pattern
            use_tw = taiwan and _taiwan_ready()
            if use_tw:
                # Taiwan toggle: match the prefilled Taiwan reading of EVERY CEDICT
                # row (a whole reading first, then readings that start with the
                # query), so the candidates and the displayed reading agree.
                rows = _fetchall(
                    connection,
                    "SELECT entries.simplified AS simplified, "
                    "entries.traditional AS traditional, entries.pinyin AS pinyin, "
                    "entries.english AS english, "
                    "entries.english_normalized AS english_normalized "
                    f"FROM {_TW_CEDICT_FROM} "
                    "WHERE tw_source = 'cedict' AND tw_toneless LIKE ? ESCAPE '\\' "
                    "ORDER BY (tw_toneless = ?) DESC"
                    f"{freq_clause}, length(entries.simplified), entries.id LIMIT ?",
                    (pattern, pinyin_key, limit),
                    "pinyin_lookup_taiwan",
                )
            else:
                rows = _fetchall(
                    connection,
                    "SELECT simplified, traditional, pinyin, english, english_normalized "
                    f"FROM entries WHERE {pinyin_clause} "
                    f"OR {_note_or()} "
                    # Direct readings first, then the whole reading, then frequency.
                    # An alternate-pronunciation note ("Taiwan pr. [wei2]") must not
                    # outrank a more common word whose own reading is the query.
                    "ORDER BY (pinyin_toneless LIKE ? ESCAPE '\\') DESC, "
                    "(pinyin_toneless = ?) DESC"
                    f"{freq_clause}, length(simplified), id LIMIT ?",
                    (*pinyin_params, note_pattern, direct_pattern, pinyin_key, limit),
                    "pinyin_lookup",
                )
            # With the Taiwan toggle on, also match MOE (Taiwan) readings, so a
            # query like "lese" finds 垃圾.
            if taiwan and not use_tw:
                words = sorted(_moe_toneless_map().get(pinyin_key, ()))
                if words:
                    holes = ",".join("?" * len(words))
                    rows += connection.execute(
                        "SELECT simplified, traditional, pinyin, english, english_normalized "
                        f"FROM entries WHERE traditional IN ({holes}) "
                        "ORDER BY length(simplified), id LIMIT ?",
                        (*words, limit),
                    ).fetchall()

            # The full tier also matches readings that merely contain the query,
            # e.g. "nuaile" -> 喜怒哀樂 (xǐ nù āi lè). They stay after every
            # exact and prefix match. The fast tier deliberately skips this.
            if part != "primary" and not wildcard:
                contains_pattern = _like_pattern(f"*{pinyin_key}*")
                if use_tw:
                    tw_order = ", ".join(_freq_order() + ["length(entries.simplified)", "entries.id"])
                    if _taiwan_has_trigram() and len(pinyin_key) >= 3 and (
                        re.fullmatch(r"[a-zü]+", pinyin_key) is not None
                    ):
                        tw_contains = (
                            "tw.readings.rowid IN (SELECT rowid FROM tw.readings_fts "
                            "WHERE tw_toneless LIKE ?)"
                        )
                        tw_params = (f"%{pinyin_key}%",)
                    else:
                        tw_contains = "tw_toneless LIKE ? ESCAPE '\\'"
                        tw_params = (contains_pattern,)
                    rows += _fetchall(
                        connection,
                        "SELECT entries.simplified AS simplified, "
                        "entries.traditional AS traditional, "
                        "entries.pinyin AS pinyin, entries.english AS english, "
                        "entries.english_normalized AS english_normalized "
                        f"FROM {_TW_CEDICT_FROM} "
                        f"WHERE tw_source = 'cedict' AND {tw_contains} "
                        "AND tw_toneless NOT LIKE ? ESCAPE '\\' "
                        f"ORDER BY {tw_order} LIMIT ?",
                        (*tw_params, pattern, limit),
                        "pinyin_contains_taiwan",
                    )
                else:
                    contains_pattern = _like_pattern(f"*{pinyin_key}*")
                    if use_trigram:
                        contains_clause = (
                            "id IN (SELECT rowid FROM entries_fts_trigram "
                            "WHERE pinyin_toneless LIKE ?)"
                        )
                        contains_params = (f"%{pinyin_key}%",)
                    else:
                        contains_clause = "pinyin_toneless LIKE ? ESCAPE '\\'"
                        contains_params = (contains_pattern,)
                    contains_order = ", ".join(
                        _freq_order() + ["length(simplified)", "id"]
                    )
                    rows += _fetchall(
                        connection,
                        "SELECT simplified, traditional, pinyin, english, english_normalized "
                        f"FROM entries WHERE {contains_clause} "
                        "AND pinyin_toneless NOT LIKE ? ESCAPE '\\' "
                        f"ORDER BY {contains_order} LIMIT ?",
                        (*contains_params, direct_pattern, limit),
                        "pinyin_contains",
                    )
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
            # Do not let a Latin word spelled out as pinyin ("w o r d jiě")
            # hijack an English query. If no standard reading remains, the
            # search falls through to the English-definition path.
            if not is_standard_pinyin(row["pinyin"]):
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
        if part == "primary":
            source_rows = []
        else:
            with pd.stage("source_pinyin"):
                source_rows = _source_pinyin_entries(
                    query, limit=limit, taiwan=taiwan
                )
        source_rows = [
            entry
            for entry in source_rows
            if is_standard_pinyin(entry.get("pinyin", ""))
        ]
        # Prefer the reading with the most senses when rank and frequency tie.
        if entries:
            _distinct = {e.get("simplified") for e in entries if e.get("simplified")}
            _counts = _cedict_reading_counts(list(_distinct))
            _group_order = {}
            for _i, _e in enumerate(entries):
                _e["_orig"] = _i
                _e["_sense_count"] = _entry_sense_count(_counts.get(_e.get("simplified"), {}), _e)
                if _e.get("simplified") not in _group_order:
                    _group_order[_e.get("simplified")] = _i
            entries.sort(
                key=lambda _e: (
                    _group_order.get(_e.get("simplified"), 0),
                    -_e.get("_sense_count", 0),
                    _e.get("_orig", 0),
                )
            )
            for _e in entries:
                _e.pop("_sense_count", None)
                _e.pop("_orig", None)
        if entries or source_rows:
            if part == "primary":
                return decorate(entries), True

            # Keep the CEDICT order intact. Source prefixes only move ahead of
            # the first CEDICT row that merely contains the query; source
            # contains-only rows stay at the end.
            def pinyin_rank(entry: dict) -> int:
                reading = to_toneless(entry.get("pinyin", ""))
                if reading == pinyin_key:
                    return 0
                if reading.startswith(pinyin_key):
                    return 1
                return 2

            # Prefer the reading with the most senses when rank and frequency
            # tie (德 should show dé before Dé), matching lookup()'s rule.
            _sense_words = sorted({e.get("simplified") for e in entries if e.get("simplified")})
            _sense_counts = _cedict_reading_counts(_sense_words) if _sense_words else {}
            for _i, _e in enumerate(entries):
                _e["_orig"] = _i
                _e["_sense_count"] = _entry_sense_count(_sense_counts, _e)
            entries.sort(
                key=lambda _e: (
                    pinyin_rank(_e),
                    -_e.get("_sense_count", 0),
                    _e.get("_orig", 0),
                )
            )
            insert_at = next(
                (i for i, entry in enumerate(entries) if pinyin_rank(entry) == 2),
                len(entries),
            )
            for _e in source_rows:
                _w = _e.get("simplified")
                _sc = _cedict_reading_counts([_w]) if _w else {}
                _e["_sense_count"] = _entry_sense_count(_sc, _e)
            source_prefix = [e for e in source_rows if pinyin_rank(e) <= 1]
            source_contains = [e for e in source_rows if pinyin_rank(e) == 2]
            source_prefix.sort(key=lambda e: -e.get("_sense_count", 0))
            source_contains.sort(key=lambda e: -e.get("_sense_count", 0))
            decorated = decorate(
                entries[:insert_at]
                + source_prefix
                + entries[insert_at:]
                + source_contains
            )
            for _e in decorated:
                _e.pop("_orig", None)
                _e.pop("_sense_count", None)
            return decorated, True

    # Chinese: convert to simplified. A plain query matches anywhere in the word,
    # ranked exact first, then words starting with the query, then the rest.
    chinese = to_simplified(query)
    literal = chinese.replace("*", "")  # the "exact" word, ranked first

    # Hanzi and numeric queries may match the headword column. A purely latin
    # query is pinyin or English and must not match latin headwords like
    # "word". Numeric headwords (21三体综合症, 421) should rank above entries
    # that only mention the number in a definition.
    is_chinese = has_cjk(literal)
    is_numeric = not is_chinese and bool(re.search(r"\d", literal))

    # Search all common spellings of a number: 21, 二十一, 廿一, 二零二六,
    # and so on. The original query is always first.
    headword_forms = (
        _number_query_forms(chinese)
        if "*" not in query and (is_chinese or is_numeric)
        else [chinese]
    )
    if is_chinese and "*" not in query and query != chinese:
        # Search the form the user typed as well as its OpenCC simplification.
        # Otherwise 乾 becomes 干 and finds only the gān entries, never the
        # qián entries whose own simplified form is 乾.
        headword_forms = [query] + [
            form for form in headword_forms if form != query
        ]
    contains_patterns = [
        _like_pattern(f"*{form}*") for form in headword_forms
    ]
    prefix_patterns = [
        _like_pattern(f"{form}*") for form in headword_forms
    ]
    if "*" in query:
        # Explicit wildcard (e.g. "*字"): honour it as typed.
        contains_patterns = [_like_pattern(chinese)]
        prefix_patterns = contains_patterns

    # Unquoted searches stop here when a headword matches. Definitions are only
    # searched when there is no headword match. Source-only headwords count too.
    headword_clause = ""
    headword_params: list[str] = []
    headword_order: list[str] = []
    headword_order_params: list[str] = []
    if is_chinese:
        # A Chinese query can match either script form. This matters for
        # traditional-only variants such as 柹 that OpenCC cannot convert to
        # their simplified main form (柿): the row is still found here and
        # `_expand_variants` redirects it to 柿.
        headword_clause = "(" + " OR ".join(
            "(simplified LIKE ? ESCAPE '\\' OR traditional LIKE ? ESCAPE '\\')"
            for _ in headword_forms
        ) + ")"
        for pattern in contains_patterns:
            headword_params += [pattern, pattern]
        if len(headword_forms) > 1:
            headword_order.append("(simplified = ? OR traditional = ?) DESC")
            headword_order_params += [headword_forms[0], headword_forms[0]]
            headword_order.append(
                "(simplified LIKE ? ESCAPE '\\' "
                "OR traditional LIKE ? ESCAPE '\\') DESC"
            )
            headword_order_params += [prefix_patterns[0], prefix_patterns[0]]
        headword_order.append(
            "(" + " OR ".join(
                "(simplified = ? OR traditional = ?)" for _ in headword_forms
            ) + ") DESC"
        )
        for form in headword_forms:
            headword_order_params += [form, form]
        headword_order.append(
            "(" + " OR ".join(
                "(simplified LIKE ? ESCAPE '\\' OR traditional LIKE ? ESCAPE '\\')"
                for _ in headword_forms
            ) + ") DESC"
        )
        for pattern in prefix_patterns:
            headword_order_params += [pattern, pattern]
    elif is_numeric:
        headword_clause = "(" + " OR ".join(
            "(simplified LIKE ? ESCAPE '\\' OR traditional LIKE ? ESCAPE '\\')"
            for _ in headword_forms
        ) + ")"
        for pattern in contains_patterns:
            headword_params += [pattern, pattern]
        if len(headword_forms) > 1:
            headword_order.append("(simplified = ? OR traditional = ?) DESC")
            headword_order_params += [headword_forms[0], headword_forms[0]]
            headword_order.append(
                "(simplified LIKE ? ESCAPE '\\' "
                "OR traditional LIKE ? ESCAPE '\\') DESC"
            )
            headword_order_params += [prefix_patterns[0], prefix_patterns[0]]
            headword_order.append(
                "(simplified LIKE ? ESCAPE '\\' "
                "OR traditional LIKE ? ESCAPE '\\') DESC"
            )
            headword_order_params += [contains_patterns[0], contains_patterns[0]]
        headword_order.append(
            "(" + " OR ".join(
                "(simplified = ? OR traditional = ?)" for _ in headword_forms
            ) + ") DESC"
        )
        for form in headword_forms:
            headword_order_params += [form, form]
        headword_order.append(
            "(" + " OR ".join(
                "(simplified LIKE ? ESCAPE '\\' OR traditional LIKE ? ESCAPE '\\')"
                for _ in headword_forms
            ) + ") DESC"
        )
        for pattern in prefix_patterns:
            headword_order_params += [pattern, pattern]
        headword_order.append(
            "(" + " OR ".join(
                "(simplified LIKE ? ESCAPE '\\' OR traditional LIKE ? ESCAPE '\\')"
                for _ in headword_forms
            ) + ") DESC"
        )
        for pattern in contains_patterns:
            headword_order_params += [pattern, pattern]
    headword_rows: list = []
    if headword_clause:
        candidates = None
        # The gram index covers CJK/numeric headwords, not mixed Latin ones
        # such as BP机; skip the prune for those.
        mixed_latin = any(char.isascii() and char.isalpha() for char in literal)
        if (is_chinese or is_numeric) and not mixed_latin:
            candidates = _headword_candidate_ids(headword_forms)
        if candidates is not None:
            if candidates:
                holes = ",".join("?" * len(candidates))
                headword_clause += f" AND id IN ({holes})"
                headword_params += [str(value) for value in sorted(candidates)]
            else:
                headword_clause += " AND 0"
        headword_sql = (
            "SELECT simplified, traditional, pinyin, english, english_normalized "
            "FROM entries WHERE " + headword_clause + " ORDER BY "
            + ", ".join(
                headword_order + _freq_order() + ["length(simplified)", "id"]
            )
            + " LIMIT ?"
        )
        connection = connect()
        try:
            headword_rows = _fetchall(
                connection,
                headword_sql,
                [*headword_params, *headword_order_params, limit],
                "headword_lookup",
            )
        finally:
            connection.close()
    if (
        headword_rows
        and os.environ.get("YABAI_SOURCE_HEADWORD_SKIP") == "1"
    ):
        # Experimental: source-only headwords can be real results, so this is
        # off by default. The golden diff showed 50 changed queries.
        source_headwords = []
    elif part == "primary":
        source_headwords = []
    else:
        with pd.stage("source_only_headwords"):
            source_headwords = _source_only_entries(query, limit=limit)
    if headword_rows or source_headwords:
        entries = [dict(row) for row in headword_rows] + source_headwords
        decorated = decorate(entries)
        # A headword can have several readings (德: dé, Dé). Show the one with
        # the most senses first, matching lookup()'s rule. Group by simplified
        # form so other headwords keep their search order.
        if decorated:
            _distinct = {e.get("simplified") for e in decorated if e.get("simplified")}
            _counts_all = _cedict_reading_counts(list(_distinct))
            import sys as _dbg_sys
            for e in decorated:
                e["_sense_count"] = _entry_sense_count(_counts_all, e)
            _group_order = {}
            for _i, _e in enumerate(decorated):
                if _e.get("simplified") not in _group_order:
                    _group_order[_e.get("simplified")] = _i
            decorated.sort(
                key=lambda e: (
                    _group_order.get(e.get("simplified"), 0),
                    -e.get("_sense_count", 0),
                )
            )
            for e in decorated:
                e.pop("_sense_count", None)
        if part == "primary":
            return decorated[: len(headword_rows)], False

        # Keep the CEDICT order (it already handles exact, prefix, contains and
        # number variants) and only move source prefixes ahead of the first
        # CEDICT contains-only row.
        def chinese_rank(entry: dict) -> int:
            words = (entry.get("simplified", ""), entry.get("traditional", ""))
            if any(word == form for word in words for form in headword_forms):
                return 0
            if any(word.startswith(form) for word in words for form in headword_forms):
                return 1
            return 2

        if not source_headwords:
            return decorated, False
        cedict_count = len(headword_rows)
        cedict = decorated[:cedict_count]
        sources = decorated[cedict_count:]
        source_prefix = [e for e in sources if chinese_rank(e) <= 1]
        source_contains = [e for e in sources if chinese_rank(e) == 2]
        insert_at = next(
            (i for i, entry in enumerate(cedict) if chinese_rank(entry) == 2),
            len(cedict),
        )
        return (
            cedict[:insert_at]
            + source_prefix
            + cedict[insert_at:]
            + source_contains,
            False,
        )

    # Definitions can use a different numeral spelling too (2026 vs 二零二六).
    if "*" in query:
        definition_patterns = [_like_pattern(query)]
    else:
        definition_patterns = [
            _like_pattern(f"*{form}*") for form in headword_forms
        ]
    # For a plain ASCII English word, use the trigram index only to find
    # candidates. The original lower(...) LIKE test remains the exact filter,
    # so mixed-case definitions and case_sensitive_like=ON behave as before.
    defs_legacy = os.environ.get("YABAI_DEFS_LEGACY") == "1"
    use_definition_trigram = (
        _has_cedict_trigram()
        and not defs_legacy
        and not is_chinese
        and not is_numeric
        and "*" not in query
        and len(query) >= 3
        and query.isascii()
        and not any(char in query for char in "%_\\")
        and re.fullmatch(r"[A-Za-z0-9üÜ' .\-]+", query) is not None
    )
    if use_definition_trigram:
        match_query = 'english_normalized : "' + query.replace('"', '""') + '"'
        definition_clause = (
            "id IN (SELECT rowid FROM entries_fts_trigram "
            "WHERE english_normalized MATCH ?) "
            "AND lower(english_normalized) LIKE ? ESCAPE '\\'"
        )
        definition_patterns = [match_query, _like_pattern(f"*{query}*")]
    else:
        definition_clause = "(" + " OR ".join(
            "lower(english_normalized) LIKE ? ESCAPE '\\'" for _ in definition_patterns
        ) + ")"

    # WHERE: the headword (hanzi or numeric queries) or the definitions.
    where_parts = [definition_clause]
    where_params = list(definition_patterns)
    if is_chinese:
        clause = "(" + " OR ".join(
            "simplified LIKE ? ESCAPE '\\'" for _ in headword_forms
        ) + ")"
        where_parts.insert(0, clause)
        where_params[0:0] = contains_patterns
    elif is_numeric:
        clause = "(" + " OR ".join(
            "(simplified LIKE ? ESCAPE '\\' OR traditional LIKE ? ESCAPE '\\')"
            for _ in headword_forms
        ) + ")"
        headword_params = []
        for pattern in contains_patterns:
            headword_params += [pattern, pattern]
        where_parts.insert(0, clause)
        where_params[0:0] = headword_params

    # ORDER BY tiers (with their own parameters).
    order_parts = []
    order_params = []
    if is_chinese:
        if len(headword_forms) > 1:
            # Prefer the spelling the user typed (e.g. 二十一) over the other
            # number variants (21, 二一, 廿一).
            order_parts.append("(simplified = ?) DESC")
            order_params.append(headword_forms[0])
            order_parts.append("(simplified LIKE ? ESCAPE '\\') DESC")
            order_params.append(prefix_patterns[0])
        clause = "(" + " OR ".join(
            "simplified = ?" for _ in headword_forms
        ) + ")"
        order_parts.append(clause + " DESC")  # exact Chinese word
        order_params.extend(headword_forms)
        clause = "(" + " OR ".join(
            "simplified LIKE ? ESCAPE '\\'" for _ in headword_forms
        ) + ")"
        order_parts.append(clause + " DESC")  # starts with
        order_params.extend(prefix_patterns)
    elif is_numeric:
        if len(headword_forms) > 1:
            # Prefer the Arabic spelling as typed (21) over its Chinese
            # variants (二十一, 二一, 廿一), including substring matches so a
            # headword like 421 stays above variant-only matches.
            order_parts.append("(simplified = ? OR traditional = ?) DESC")
            order_params += [headword_forms[0], headword_forms[0]]
            order_parts.append(
                "(simplified LIKE ? ESCAPE '\\' "
                "OR traditional LIKE ? ESCAPE '\\') DESC"
            )
            order_params += [prefix_patterns[0], prefix_patterns[0]]
            order_parts.append(
                "(simplified LIKE ? ESCAPE '\\' "
                "OR traditional LIKE ? ESCAPE '\\') DESC"
            )
            order_params += [contains_patterns[0], contains_patterns[0]]
        clause = "(" + " OR ".join(
            "(simplified = ? OR traditional = ?)" for _ in headword_forms
        ) + ")"
        order_parts.append(clause + " DESC")  # exact number
        for form in headword_forms:
            order_params += [form, form]
        clause = "(" + " OR ".join(
            "(simplified LIKE ? ESCAPE '\\' OR traditional LIKE ? ESCAPE '\\')"
            for _ in headword_forms
        ) + ")"
        order_parts.append(clause + " DESC")  # starts with
        for pattern in prefix_patterns:
            order_params += [pattern, pattern]
        # Any headword containing the number ranks above definition-only hits.
        order_parts.append(clause + " DESC")
        for pattern in contains_patterns:
            order_params += [pattern, pattern]
    order_parts.append("(lower(english_normalized) LIKE ? ESCAPE '\\') DESC")
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
    language_query = _is_language_query(query)
    # For a language name, fetch extra rows so note-only matches cannot crowd
    # normal definition matches out of the page before the stable partition.
    fetch_limit = limit * 3 if language_query else limit
    params = [*where_params, *order_params, fetch_limit]

    connection = connect()
    try:
        rows = _fetchall(connection, sql, params, "definition_fts")
    finally:
        connection.close()  # always close, even if the query raised

    entries = [dict(row) for row in rows]
    if (
        not language_query
        and not is_chinese
        and not is_numeric
        and os.environ.get("YABAI_RELEVANCE", "1") != "0"
    ):
        _attach_freq(entries)
        _attach_relevance(entries, original_query)
        entries = relevance.rank_entries(
            entries,
            original_query,
            boost_exact=original_query[:1].isupper() and original_query[:1].isalpha(),
        )
    if language_query:
        entries = _rank_language_matches(entries, query)[:limit]
    # Also surface headwords that only the secondary sources or the frequency
    # list have, so a word CEDICT lacks is still findable.
    # The non-exact headword scan already ran above (and found nothing, or the
    # search would have returned), and the pinyin branches ran too unless
    # pinyin was explicitly skipped. Tell _source_matches not to repeat them.
    if part == "primary":
        source_rows = []
    else:
        with pd.stage("source_matches"):
            source_rows = _source_matches(
                query,
                limit=_cap(20),
                heads_checked=True,
                pinyin_checked=not _skip_pinyin,
            )
    # A CEDICT definition match and a source gloss match can be the same word.
    # Keep the CEDICT row, and append only genuinely source-only words.
    if source_rows:
        entry_keys = {entry["simplified"] for entry in entries}
        source_rows = [
            entry for entry in source_rows if entry["simplified"] not in entry_keys
        ]
    with pd.stage("search_decorate"):
        decorated = decorate(entries + source_rows)
    if part == "primary":
        return decorated[: len(entries)], False
    if not decorated:
        # Nothing matched at all: allow a headword that only exists without a
        # definition (e.g. 18禁), so an exact search still shows it with its
        # source link instead of an empty page.
        return decorate(
            _source_only_entries(
                query, limit=_cap(10), exact=True, allow_no_gloss=True
            )
        ), False
    return decorated, False


# Rows whose headword or reading can match a Latin query at tier 0. The set is
# tiny (a few hundred CEDICT rows), so it is built once and scanned in memory
# instead of adding a GLOB scan to every search.
_LATIN_CANDIDATE_CACHE: list[dict] | None = None
_LATIN_CANDIDATE_LOCK = threading.Lock()


def _latin_reading_runs(reading: str) -> list[str]:
    """Join each run of one-letter pinyin tokens (``w o r d`` -> ``word``)."""
    runs: list[str] = []
    run: list[str] = []
    for token in (reading or "").split():
        base = to_toneless(token)
        if len(base) == 1:
            run.append(base)
        else:
            if run:
                runs.append("".join(run))
                run = []
    if run:
        runs.append("".join(run))
    return runs


def _latin_candidate_rows() -> list[dict]:
    """CEDICT rows that can match a Latin query by headword or reading."""
    global _LATIN_CANDIDATE_CACHE
    if _LATIN_CANDIDATE_CACHE is not None:
        return _LATIN_CANDIDATE_CACHE
    with _LATIN_CANDIDATE_LOCK:
        if _LATIN_CANDIDATE_CACHE is not None:
            return _LATIN_CANDIDATE_CACHE
        connection = connect()
        try:
            headword_rows = connection.execute(
                "SELECT id, simplified, traditional, pinyin, english, "
                "english_normalized FROM entries "
                "WHERE simplified GLOB '*[A-Za-z]*' "
                "OR traditional GLOB '*[A-Za-z]*'"
            ).fetchall()
            # GLOB is only a cheap prefilter: it can match the tail of one
            # syllable and the head of the next ("jīn r ge"), so the Python
            # classifier still decides which readings are really spelled-out
            # Latin words.
            reading_rows = connection.execute(
                "SELECT id, simplified, traditional, pinyin, english, "
                "english_normalized FROM entries "
                "WHERE pinyin GLOB '*[A-Za-z] [A-Za-z] [A-Za-z]*'"
            ).fetchall()
            rows: dict[int, dict] = {}
            for row in headword_rows:
                rows[row["id"]] = dict(row)
            for row in reading_rows:
                if not is_standard_pinyin(row["pinyin"]):
                    rows[row["id"]] = dict(row)
        finally:
            connection.close()
        _LATIN_CANDIDATE_CACHE = list(rows.values())
        return _LATIN_CANDIDATE_CACHE


def _latin_candidates(query: str, limit: int) -> list[dict]:
    """Rows whose headword or reading contains `query` as Latin text.

    A headword such as ``word姐`` matches by its headword. A reading that
    spells a Latin word out token by token (``w o r d jiě``) matches by the
    joined one-letter run. Both are tier 0.
    """
    normalized = unicodedata.normalize("NFKC", query).strip().lower()
    if not normalized or not relevance.is_latin_query(normalized):
        return []
    matches: list[tuple[relevance.Features, dict]] = []
    for row in _latin_candidate_rows():
        entry = dict(row)
        features = None
        for headword in (entry.get("simplified"), entry.get("traditional")):
            features = relevance.headword_features(headword or "", normalized)
            if features is not None:
                break
        if features is None:
            runs = _latin_reading_runs(entry.get("pinyin", ""))
            # A headword match outranks a reading-only match; inside the
            # reading group an exact run sorts ahead of a mere substring.
            if any(normalized == run for run in runs):
                features = relevance.Features(1, 1, 0)
            elif any(normalized in run for run in runs):
                features = relevance.Features(1, 1, 1)
        if features is not None:
            matches.append((features, entry))
    if not matches:
        return []
    entries = [entry for _features, entry in matches]
    _attach_freq(entries)
    matches.sort(key=lambda item: relevance.relevance_key(item[0], item[1]))
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for _features, entry in matches:
        key = (entry.get("traditional", ""), entry.get("pinyin", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
        if len(unique) >= limit:
            break
    return unique


def search(
    query: str,
    limit: int = 50,
    taiwan: bool = False,
    part: str = "all",
    _skip_pinyin: bool = False,
    hydrate: int | None = None,
) -> tuple[list[dict], bool]:
    """Search the dictionary, adding tier-0 Latin headword/reading matches.

    The core search finds headwords, readings and definitions. For an unquoted
    Latin query, rows whose headword or reading contains the query as Latin
    text are prepended at tier 0 (``word`` finds ``word姐`` first). Quoted
    queries stay definition-only. `part="secondary"` never prepends them, so
    the primary and secondary halves still merge into the same list.
    """
    entries, pinyin_match = _search_core(
        query,
        limit,
        taiwan,
        part=part,
        _skip_pinyin=_skip_pinyin,
        hydrate=hydrate,
    )
    text = query.strip()
    quoted = len(text) >= 2 and text.startswith('"') and text.endswith('"')
    if (
        part != "secondary"
        and not quoted
        and text
        and os.environ.get("YABAI_RELEVANCE", "1") != "0"
        and relevance.is_latin_query(unicodedata.normalize("NFKC", text))
    ):
        latin = _latin_candidates(text, min(50, limit))
        if latin:
            seen = {
                (entry.get("traditional", ""), entry.get("pinyin", ""))
                for entry in entries
            }
            latin = [
                entry
                for entry in latin
                if (entry.get("traditional", ""), entry.get("pinyin", ""))
                not in seen
            ]
            for entry in latin:
                entry["tier"] = 1
                # Keep rank_by_category from re-sorting the prepended rows.
                entry["_relevance"] = True
                entry["category"] = 1
                # A headword/reading match, not a definition match: it keeps
                # the headword rule when rank_by_category picks its category.
                entry["_head_match"] = True
            entries = latin + entries
    return entries, pinyin_match


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


# Hot files first: the page-cache warm-up stops when it reaches its byte budget,
# so the dictionaries every search touches are read and the large, rarely used
# ones (MOE revised, gram words, Taiwan readings) are left to demand paging.
_PAGE_CACHE_FILES = (
    DB_PATH,
    FREQ_DB_PATH,
    SOURCE_GLOSS_DB_PATH,
    HEAD_GRAMS_DB_PATH,
    DEFINITION_GRAMS_DB_PATH,
    ENGLISH_WORDS_DB_PATH,
    HSK_DB_PATH,
    SEARCH_INDEX_DB_PATH,
    MOE_DB_PATH,
    SUBTLEX_DB_PATH,
    WIKTIONARY_DB_PATH,
    MOE_REVISED_DB_PATH,
    ZHWIKTIONARY_DB_PATH,
    JPWIKTIONARY_DB_PATH,
    GRAM_WORDS_DB_PATH,
    TAIWAN_DB_PATH,
)

# Default page-cache budget. Reading more than this on a phone fills RAM that
# the search itself needs, so the kernel evicts it again and the warm-up is
# worse than useless. Override with YABAI_WARM_PAGE_CACHE_BYTES.
_DEFAULT_PAGE_CACHE_BUDGET = 256 * 1024 * 1024
_MIN_FREE_BYTES = 512 * 1024 * 1024

# One warm-up per process. The thread is daemonised so a failed read never
# keeps the server alive at shutdown.
_WARM_LOCK = threading.Lock()
_WARM_STATE = {
    "started": False,
    "done": False,
    "elapsed_ms": None,
    "bytes": 0,
    "budget": None,
    "skipped": [],
    "error": "",
}


def warm_mode() -> str:
    """Return the page-cache warm-up mode: background, legacy or off.

    `background` (default) reads the data files in a daemon thread so startup
    does not block. `legacy` keeps the old synchronous behaviour. `off` skips
    the warm-up, which is useful when measuring a genuinely cold start.
    """
    mode = os.environ.get("YABAI_WARM_PAGE_CACHE", "1").strip().lower()
    if mode in ("0", "off", "false", "no"):
        return "off"
    if mode in ("legacy", "sync"):
        return "legacy"
    return "background"


def page_cache_status() -> dict:
    """Snapshot used by tests and /__debug/status."""
    with _WARM_LOCK:
        return dict(_WARM_STATE)


def _available_memory_bytes() -> int | None:
    """MemAvailable in bytes, or None where /proc/meminfo is not available."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _page_cache_budget() -> int | None:
    """How many bytes the page-cache warm-up may read (None = every file)."""
    raw = os.environ.get("YABAI_WARM_PAGE_CACHE_BYTES", "").strip().lower()
    if raw in ("all", "unlimited"):
        return None
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            return _DEFAULT_PAGE_CACHE_BUDGET
    return _DEFAULT_PAGE_CACHE_BUDGET


def _warm_page_cache_files(budget: int | None = None) -> int:
    """Read the hot dictionary files once and ask the kernel to read ahead.

    `posix_fadvise(WILLNEED)` alone is only a hint and can return before the
    pages are resident. Reading the file in 1 MiB chunks makes the warm-up
    deterministic; the data is mapped by SQLite anyway, so this does not keep
    an extra copy in the Python heap.

    `budget` caps the total bytes read; files after the cap are skipped. On a
    phone the old "read everything" warm-up filled RAM that the search needed,
    so the kernel evicted it and the next query was cold anyway.
    """
    total = 0
    skipped: list[str] = []
    seen: set[str] = set()
    available = _available_memory_bytes()
    if available is not None and available < _MIN_FREE_BYTES:
        with _WARM_LOCK:
            _WARM_STATE["skipped"] = ["available memory below 512 MiB"]
        return 0
    for path in _PAGE_CACHE_FILES:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if not path.exists():
                continue
            size = path.stat().st_size
            if budget is not None and total + size > budget:
                skipped.append(path.name)
                continue
            with path.open("rb") as handle:
                try:
                    os.posix_fadvise(
                        handle.fileno(), 0, 0, os.POSIX_FADV_WILLNEED
                    )
                except (AttributeError, OSError):
                    pass
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
        except OSError as exc:
            with _WARM_LOCK:
                _WARM_STATE["error"] = f"{path.name}: {exc}"
    with _WARM_LOCK:
        _WARM_STATE["skipped"] = skipped
    return total


def _warm_page_cache_worker() -> None:
    """Background body: page cache first, then representative searches."""
    start = time.perf_counter()
    try:
        budget = _page_cache_budget()
        total = _warm_page_cache_files(budget)
        available = _available_memory_bytes()
        # Run the representative searches only when there is memory to spare;
        # on a phone they compete with the first real visitor.
        if available is None or available > 2 * 1024 * 1024 * 1024:
            warm_search_queries()
        with _WARM_LOCK:
            _WARM_STATE.update(
                done=True,
                elapsed_ms=round((time.perf_counter() - start) * 1000, 1),
                bytes=total,
                budget=budget,
            )
        print(
            "page cache warm: "
            f"{total / (1024 * 1024):.0f} MiB in "
            f"{_WARM_STATE['elapsed_ms']:.0f} ms"
            + (f" (skipped {len(_WARM_STATE['skipped'])} files)" if _WARM_STATE["skipped"] else ""),
            flush=True,
        )
    except Exception as exc:  # pragma: no cover - best-effort warm-up
        with _WARM_LOCK:
            _WARM_STATE.update(
                done=True,
                elapsed_ms=round((time.perf_counter() - start) * 1000, 1),
                error=repr(exc),
            )
        print(f"page cache warm failed: {exc!r}", flush=True)


def _start_page_cache_warm() -> None:
    """Start the one background warm-up thread for this process."""
    with _WARM_LOCK:
        if _WARM_STATE["started"]:
            return
        _WARM_STATE["started"] = True
    threading.Thread(
        target=_warm_page_cache_worker,
        name="page-cache-warm",
        daemon=True,
    ).start()


def _warm_step(name: str, func, *args):
    """Run one cache builder and optionally log how long it took."""
    started = time.perf_counter()
    result = func(*args)
    if os.environ.get("YABAI_WARM_LOG") == "1":
        print(
            f"warm {name}: {(time.perf_counter() - started) * 1000:.0f} ms",
            flush=True,
        )
    return result


def warm_caches() -> None:
    """Build the per-process caches before the first search.

    These only change when a dictionary is re-imported, but each reads a large
    table or builds a converter. Running them at startup keeps the first request
    fast, including the multi-second first "*" listing and the first bopomofo
    query.
    """
    started = time.perf_counter()
    _warm_step("opencc_t2s", _opencc, "t2s")
    _warm_step("opencc_s2t", _opencc, "s2t")
    _warm_step("cedict_heads", _cedict_heads)
    _warm_step("cedict_syllables", _cedict_syllables)
    if MOE_DB_PATH.exists():
        _warm_step("moe_toneless", _moe_toneless_map)
        _warm_step("moe_chars", _moe_char_map)
    _warm_step("reading_counts", _reading_counts)
    _warm_step("ids_entries", _ids_entries)
    _warm_step("zhuyin", _zhuyin_map)
    _warm_step("definition_grams", _definition_grams)
    _warm_step("latin_candidates", _latin_candidate_rows)
    # The "*" headword list and the source pinyin rows are not built here: they
    # cost tens of seconds of OpenCC work on a phone. The prebuilt search index
    # answers both, and without it they are built on first use.
    # Hash the CEDICT file at start-up, while no visitor is waiting.
    _warm_step("source_gloss_hash", _source_gloss_version_ok)
    _warm_step("head_grams_hash", _head_grams_version_ok)
    _warm_step("gram_words_hash", _gram_words_version_ok)
    print(
        "search index: "
        + (
            "ok"
            if _search_index_ok()
            else "missing or stale (run scripts/build_search_index.py)"
        ),
        flush=True,
    )
    if os.environ.get("YABAI_WARM_LOG") == "1":
        print(
            f"warm total: {(time.perf_counter() - started) * 1000:.0f} ms",
            flush=True,
        )
    mode = warm_mode()
    if mode == "legacy":
        _warm_page_cache_files(None)
        with _WARM_LOCK:
            _WARM_STATE["budget"] = None
        warm_search_queries()
    elif mode == "background":
        _start_page_cache_warm()


def warm_search_queries() -> None:
    """Run representative searches once so their SQLite pages are hot.

    The common English/pinyin queries are included because real visitors type
    them; leaving them out made the first `the` request pay the cold-read cost.
    """
    for query in (
        "ruan3",
        "ruan4",
        "tetragram",
        "tetragram of the",
        "d",
        "shi",
        "ing",
        "the",
        "档案",
    ):
        try:
            search(query, limit=25)
        except Exception:
            # Warm-up is best effort; a real search must still surface errors.
            pass
