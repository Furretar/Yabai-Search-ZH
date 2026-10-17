"""Segment Chinese text with jieba, seeded with the dictionary headwords."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import jieba

from yabai_chaci.db import (
    DB_PATH,
    MOE_DB_PATH,
    SUBTLEX_DB_PATH,
    WIKTIONARY_DB_PATH,
    has_cjk,
)

# The jieba user-dictionary we generate from the dictionary headwords.
USERDICT_PATH = Path(__file__).resolve().parents[1] / "data" / "cedict_userdict.txt"

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
    """Every multi-character headword across all sources, cached.

    CEDICT is the main list; MOE, Wiktionary and SUBTLEX add words jieba would
    otherwise split (e.g. 一償宿願 from MOE). Both scripts are included, and
    single characters are skipped.
    """
    global _headword_cache
    if _headword_cache is None:
        words: set[str] = set()
        words |= _headwords_from(
            DB_PATH, "SELECT traditional FROM entries WHERE length(traditional) > 1"
        )
        words |= _headwords_from(
            DB_PATH, "SELECT simplified FROM entries WHERE length(simplified) > 1"
        )
        words |= _headwords_from(
            MOE_DB_PATH, "SELECT word FROM moe WHERE length(word) > 1"
        )
        words |= _headwords_from(
            WIKTIONARY_DB_PATH,
            "SELECT DISTINCT word FROM wiktionary WHERE length(word) > 1",
        )
        words |= _headwords_from(
            SUBTLEX_DB_PATH, "SELECT word FROM freq WHERE length(word) > 1"
        )
        _headword_cache = words
    return _headword_cache


def build_userdict() -> int:
    """Write every known headword to a jieba user-dict file.

    Each line carries an explicit frequency because a bare word gets a tiny
    frequency (observed: 1) that jieba then prefers to split. Returns the number
    of words written.
    """
    words = _headwords()
    USERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    USERDICT_PATH.write_text(
        "\n".join(f"{word} 20000" for word in sorted(words)) + "\n", encoding="utf-8"
    )
    return len(words)


def _ensure_loaded() -> None:
    """Load the user dictionary into jieba once (building it if needed)."""
    global _loaded
    if not _loaded:
        if not USERDICT_PATH.exists():
            build_userdict()
        jieba.load_userdict(str(USERDICT_PATH))
        _loaded = True


def _max_headword_len() -> int:
    """Length of the longest known headword, cached."""
    global _max_len_cache
    if _max_len_cache is None:
        _max_len_cache = max((len(word) for word in _headwords()), default=1)
    return _max_len_cache


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


def segment(text: str) -> list[str]:
    """Split Chinese text into words, keeping known headwords together.

    jieba sometimes splits a known headword (e.g. 一償宿願) even when it is in the
    user dict, so afterwards we greedily merge runs of adjacent tokens into the
    longest known headword. This only ever joins tokens whose concatenation is an
    exact dictionary headword, so it cannot merge across real word boundaries.
    """
    _ensure_loaded()
    tokens = list(jieba.cut(text))
    words = _headwords()

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
    return _split_unknown(_join_foreign(merged))
