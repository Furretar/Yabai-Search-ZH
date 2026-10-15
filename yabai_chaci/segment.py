"""Segment Chinese text with jieba, seeded with the CC-CEDICT headwords."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import jieba

from yabai_chaci.db import DB_PATH

# The jieba user-dictionary we generate from CEDICT headwords.
USERDICT_PATH = Path(__file__).resolve().parents[1] / "data" / "cedict_userdict.txt"

_loaded = False


def build_userdict(db_path: Path = DB_PATH) -> int:
    """Write every multi-character CEDICT headword to a jieba user-dict file.

    Single characters are skipped so jieba isn't nudged toward one-char splits.
    Returns the number of words written.
    """
    connection = sqlite3.connect(db_path)
    try:
        words = sorted(
            {
                row[0]
                for row in connection.execute(
                    "SELECT simplified FROM entries WHERE length(simplified) > 1"
                )
            }
        )
    finally:
        connection.close()

    USERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    USERDICT_PATH.write_text("\n".join(words) + "\n", encoding="utf-8")
    return len(words)


def _ensure_loaded() -> None:
    """Load the user dictionary into jieba once (building it if needed)."""
    global _loaded
    if not _loaded:
        if not USERDICT_PATH.exists():
            build_userdict()
        jieba.load_userdict(str(USERDICT_PATH))
        _loaded = True


def segment(text: str) -> list[str]:
    """Split Chinese text into words."""
    _ensure_loaded()
    return list(jieba.cut(text))
