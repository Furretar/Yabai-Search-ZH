"""Segment Chinese text with jieba, seeded with the dictionary headwords."""

from __future__ import annotations

import hashlib
import os
import pickle
import sqlite3
import threading
import time
from pathlib import Path

import jieba

from yabai_search import db
from yabai_search.db import has_cjk

# The jieba user-dictionary we generate from the dictionary headwords.
USERDICT_PATH = Path(__file__).resolve().parents[1] / "data" / "dl-base-dictionaries" / "cedict_userdict.txt"
# Bump when the exported headword set or file format changes. Without this, an
# old user-dict with the same source databases would never be rebuilt.
USERDICT_VERSION = 3  # 3: the SUBTLEX word list was removed from the segmenter's words
USERDICT_VERSION_PATH = USERDICT_PATH.with_suffix(".version")

# Word lists that are not dictionaries in the db.py registry: (database, SQL
# returning one headword per row). None at present: the dictionaries are the only source of words.
EXTRA_HEADWORD_SOURCES: list[tuple[Path, str]] = []

_loaded = False
_headword_cache: set[str] | None = None
_max_len_cache: int | None = None

# How many jieba tokens to try merging into a single headword.
_MAX_MERGE_TOKENS = 8


def _headwords_from(db_path: Path, sql: str) -> set[str]:
    """Run a `SELECT word` query, returning an empty set if the DB is missing."""
    if not db_path.exists():
        return set()
    connection = sqlite3.connect(db_path)
    try:
        return {row[0] for row in connection.execute(sql) if row[0]}
    finally:
        connection.close()


def _headwords() -> set[str]:
    """Every multi-character headword across all dictionaries, cached.

    The dictionaries come from the registry in db.py (db.source_headwords), so
    adding one there is enough for the segmenter to keep its words together.
    A word list that is not a dictionary can be added to EXTRA_HEADWORD_SOURCES.
    """
    global _headword_cache
    if _headword_cache is None:
        words = db.source_headwords()
        for path, sql in EXTRA_HEADWORD_SOURCES:
            # Extra lists are not in the db.py registry, so give
            # their words both scripts here too.
            words |= db.script_variants(_headwords_from(path, sql))
        _headword_cache = words
    return _headword_cache


def build_userdict() -> int:
    """Write every known headword to a jieba user-dict file.

    Each line carries an explicit frequency because a bare word gets a tiny
    frequency (observed: 1) that jieba then prefers to split. Returns the number
    of words written.
    """
    # jieba reads each line as "word frequency [tag]", so a headword with
    # whitespace (e.g. Wiktionary's "bi bu车") would be split into fake entries.
    words = sorted(
        word for word in _headwords() if not any(ch.isspace() for ch in word)
    )
    USERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    USERDICT_PATH.write_text(
        "\n".join(f"{word} 20000" for word in words) + "\n", encoding="utf-8"
    )
    USERDICT_VERSION_PATH.write_text(f"{USERDICT_VERSION}\n", encoding="utf-8")
    return len(words)


def _userdict_is_stale() -> bool:
    """True when the user dict is missing, outdated or older than its sources (see sources_changed)."""
    return _userdict_missing() or sources_changed()


def _userdict_missing() -> bool:
    """True when there is no user dict of the current version on disk."""
    if not USERDICT_PATH.exists() or not USERDICT_VERSION_PATH.exists():
        return True
    try:
        version = USERDICT_VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    return version != str(USERDICT_VERSION)


def sources_changed() -> bool:
    """True when a source database is newer than the user dict (for regenerate_everything.py --check)."""
    if not USERDICT_PATH.exists():
        return True
    built = USERDICT_PATH.stat().st_mtime
    sources = [*db.source_database_paths(), *(path for path, _ in EXTRA_HEADWORD_SOURCES)]
    return any(p.exists() and p.stat().st_mtime > built for p in sources)


# The loaded segmenter state (jieba's word table with the user dict merged in, plus the headword set),
# pickled so a start is one file read. Merging 1.2 million user-dict lines into jieba and converting
# every headword to both scripts takes minutes on a tablet, longer than gunicorn's worker timeout.
# The shipped copy is built by regenerate_everything.py; a machine without a matching one builds its own under
# deploy/run/, which Syncthing ignores (a serving machine must never write into the synced data).
CACHE_PATH = db.GENERATED_DIR / "segmenter.pickle"
LOCAL_CACHE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "run" / "segmenter.pickle"
_CACHE_FORMAT = 1
_load_lock = threading.Lock()


def _userdict_stamp() -> list:
    """What a cache must have been built from: the user dict's content and jieba's version."""
    digest = hashlib.sha256(USERDICT_PATH.read_bytes()).hexdigest()
    return [_CACHE_FORMAT, USERDICT_VERSION, digest, getattr(jieba, "__version__", "")]


def _read_cache(path: Path, stamp: list) -> dict | None:
    try:
        with path.open("rb") as handle:
            if pickle.load(handle) != stamp:
                return None
            return pickle.load(handle)
    except (OSError, pickle.PickleError, EOFError, ValueError, AttributeError):
        return None


def _write_cache(path: Path, stamp: list, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("wb") as handle:
        pickle.dump(stamp, handle, protocol=4)
        pickle.dump(state, handle, protocol=4)
    os.replace(temp, path)


def _build_state() -> dict:
    """Load the user dict into jieba the slow way and return everything a later start needs."""
    jieba.load_userdict(str(USERDICT_PATH))
    words = _headwords()
    return {
        "freq": jieba.dt.FREQ,
        "total": jieba.dt.total,
        "headwords": words,
        "max_len": max((len(word) for word in words), default=1),
    }


def build_cache() -> Path:
    """Write the shipped segmenter cache (regenerate_everything.py runs this after the user dict is rebuilt)."""
    global _loaded
    stamp = _userdict_stamp()
    _write_cache(CACHE_PATH, stamp, _build_state())
    _loaded = True
    return CACHE_PATH


def cache_current() -> bool:
    """True when the shipped cache matches the user dict on disk."""
    if _userdict_missing():
        return False
    try:
        with CACHE_PATH.open("rb") as handle:
            return pickle.load(handle) == _userdict_stamp()
    except (OSError, pickle.PickleError, EOFError, ValueError):
        return False


def _ensure_loaded() -> None:
    """Load the segmenter once: from a cache when one matches, the slow way otherwise.

    The user dict is only built here when it is missing. It is rebuilt with the other derived data
    (regenerate_everything.py), never because a source file looks newer: on a serving machine that rewrote a
    file Syncthing delivers, and the folder then stopped syncing.
    """
    global _loaded, _headword_cache, _max_len_cache
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        if _userdict_missing():
            build_userdict()
        stamp = _userdict_stamp()
        state = _read_cache(CACHE_PATH, stamp) or _read_cache(LOCAL_CACHE_PATH, stamp)
        if state is None:
            state = _build_state()
            try:
                _write_cache(LOCAL_CACHE_PATH, stamp, state)
            except OSError:
                pass  # a read-only checkout still segments, it just loads slowly next time
        else:
            jieba.dt.FREQ = state["freq"]
            jieba.dt.total = state["total"]
            jieba.dt.initialized = True
        if _headword_cache is None:
            _headword_cache = state["headwords"]
            _max_len_cache = state["max_len"]
        _loaded = True


def _max_headword_len() -> int:
    """Length of the longest known headword, cached."""
    global _max_len_cache
    if _max_len_cache is None:
        _max_len_cache = max((len(word) for word in _headwords()), default=1)
    return _max_len_cache


def warm(background: bool = True) -> None:
    """Load jieba and the headword list before the first annotate request.

    In the background by default, so the server answers searches while the segmenter loads (tens of
    seconds on a tablet). /api/annotate waits for it.
    """

    def load() -> None:
        try:
            _ensure_loaded()
            _headwords()
            _max_headword_len()
        except Exception as exc:  # noqa: BLE001  (the next annotate request retries and reports)
            print(f"segmenter warm-up failed: {exc!r}", flush=True)

    if background:
        threading.Thread(target=load, name="segmenter-warm", daemon=True).start()
    else:
        load()


def is_headword(word: str) -> bool:
    """True when `word` is a known multi-character headword in any source.

    Used by the annotate endpoint so it does not re-split a word that is a real
    headword but happens to have no CEDICT entry (e.g. 一償宿願 from MOE).
    """
    return len(word) > 1 and word in _headwords()


def _join_foreign(tokens: list[str]) -> list[str]:
    """Join runs of non-Chinese alphanumeric tokens into one word.

    jieba splits Latin text into single characters (e.g. "y", "ī"), which then
    each became a separate "word" in the parsed input. Whitespace, punctuation
    and Chinese characters act as separators, so "y", "ī", "一" becomes "yī", "一".
    """
    joined: list[str] = []
    buffer = ""
    for token in tokens:
        if token and not has_cjk(token) and all(ch.isalnum() for ch in token):
            buffer += token
            continue
        if buffer:
            joined.append(buffer)
            buffer = ""
        joined.append(token)
    if buffer:
        joined.append(buffer)
    return joined


def _split_unknown(tokens: list[str]) -> list[str]:
    """Split tokens no dictionary lists into known headwords.

    jieba can return a whole run (e.g. 一点钟) even when no source has it, which
    used to be split into single characters and lost the word 点钟. Taking the
    longest headword from the right matches how Chinese compounds are built
    (一点钟 = 一 + 点钟, 不知道 = 不 + 知道).
    """
    words = _headwords()
    max_len = _max_headword_len()
    out: list[str] = []
    for token in tokens:
        if len(token) <= 1 or token in words or not has_cjk(token):
            out.append(token)
            continue
        pieces: list[str] = []
        end = len(token)
        while end > 0:
            # Longest known headword ending at `end` (single characters otherwise).
            take = 1
            for size in range(min(max_len, end), 0, -1):
                if token[end - size : end] in words:
                    take = size
                    break
            pieces.append(token[end - take : end])
            end -= take
        out.extend(reversed(pieces))
    return out


def segment(text: str, timings: dict[str, float] | None = None) -> list[str]:
    """Split Chinese text into words, keeping known headwords together.

    jieba sometimes splits a known headword (e.g. 一償宿願) even when it is in the
    user dict, so afterwards we greedily merge runs of adjacent tokens into the
    longest known headword. This only ever joins tokens whose concatenation is an
    exact dictionary headword, so it cannot merge across real word boundaries.

    The optional `timings` dict is filled with milliseconds and n_* counts; it
    never changes the returned tokens.
    """

    def tick(name: str, at: float) -> None:
        if timings is not None:
            timings[name] = round(
                timings.get(name, 0.0) + (time.perf_counter() - at) * 1000, 2
            )

    at = time.perf_counter()
    _ensure_loaded()
    tick("ensure_loaded", at)

    at = time.perf_counter()
    tokens = list(jieba.cut(text))
    tick("jieba_cut", at)
    words = _headwords()

    at = time.perf_counter()
    merged: list[str] = []
    i = 0
    while i < len(tokens):
        # Find the longest run of following tokens that is a known headword.
        best_end = None
        joined = ""
        for j in range(i, min(i + _MAX_MERGE_TOKENS, len(tokens))):
            joined += tokens[j]
            if len(joined) > 1 and joined in words:
                best_end = j
        if best_end is not None:
            merged.append("".join(tokens[i : best_end + 1]))
            i = best_end + 1
        else:
            merged.append(tokens[i])
            i += 1
    tick("merge", at)

    at = time.perf_counter()
    foreign = _join_foreign(merged)
    tick("join_foreign", at)

    at = time.perf_counter()
    result = _split_unknown(foreign)
    tick("split_unknown", at)

    if timings is not None:
        timings["n_jieba_tokens"] = len(tokens)
        timings["n_merged"] = len(merged)
        timings["n_final"] = len(result)
    return result
