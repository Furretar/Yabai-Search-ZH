"""Give CC-CEDICT words with Latin letters Wiktionary's pinyin reading (written into cedict.sqlite).

CC-CEDICT keeps the letters of a word like K歌 or AA制 as they are ("K gē", "A A zhì"). Wiktionary
spells them out in pinyin ("kēigē", "ēi'ēi zhì"), which is how the word is said, so the site shows that
reading instead: the CEDICT row's reading is overwritten with Wiktionary's first all-pinyin reading.

Both importers run this (import_cedict.py after a new cedict.sqlite, import_wiktionary.py after a new
wiktionary.sqlite), so the result does not depend on which one ran last. It is idempotent: a row
already overwritten has no Latin letters left in its reading.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from yabai_search.pinyin import is_pinyin_token, to_numeric, to_toneless

# A reading token that is a Latin letter or word kept as written ("K", "AA", "OK"); pinyin is lowercase
# except a proper noun's first letter, so an all-uppercase token is not pinyin.
_LATIN_TOKEN_RE = re.compile(r"(?:^|[\s'\-])[A-Z]+(?=$|[\s'\-])")
_LATIN_IN_WORD_RE = re.compile(r"[A-Za-z]")


def _best_reading(readings: list[str]) -> str | None:
    """Wiktionary's first reading made only of real pinyin syllables, preferring one without erhua.

    A spelled-out letter that is not a pinyin syllable (Q = "kiū") would split into nonsense
    ("k i ū"), so such a reading is not used. The erhua form ("ēi piàn r") is used only when it is the
    only one.
    """
    usable = [
        reading for reading in readings
        if to_toneless(reading) and all(is_pinyin_token(token) for token in reading.split())
    ]
    plain = [reading for reading in usable if reading.split()[-1] != "r"]
    return (plain or usable or [None])[0]


def apply(cedict_path: Path, wiktionary_path: Path) -> int:
    """Overwrite the readings; returns how many CEDICT rows changed (0 when either database is missing)."""
    if not cedict_path.exists() or not wiktionary_path.exists():
        return 0
    connection = sqlite3.connect(cedict_path)
    try:
        connection.execute("ATTACH DATABASE ? AS wikt", (str(wiktionary_path),))
        rows = connection.execute(
            "SELECT id, traditional, simplified, pinyin FROM entries "
            "WHERE traditional GLOB '*[A-Za-z]*' OR simplified GLOB '*[A-Za-z]*'"
        ).fetchall()
        # Only a word with a single Latin-reading row: two CEDICT rows would end up with the same reading.
        latin_rows: dict[tuple[str, str], list[tuple]] = {}
        for row in rows:
            if _LATIN_TOKEN_RE.search(row[3]):
                latin_rows.setdefault((row[1], row[2]), []).append(row)
        changed = 0
        for (traditional, simplified), group in latin_rows.items():
            if len(group) != 1 or not _LATIN_IN_WORD_RE.search(traditional + simplified):
                continue
            entry_id, _, _, old = group[0]
            found = connection.execute(
                "SELECT COALESCE(NULLIF(pinyin_spaced, ''), pinyin) FROM wikt.readings "
                "WHERE word IN (?, ?) AND kind = 'mandarin' AND pinyin <> '' ORDER BY id",
                (traditional, simplified),
            ).fetchall()
            new = _best_reading([p for (p,) in found])
            if not new:
                continue
            connection.execute(
                "UPDATE entries SET pinyin = ?, pinyin_numeric = ?, pinyin_toneless = ? WHERE id = ?",
                (new, to_numeric(new), to_toneless(new), entry_id),
            )
            # The senses are keyed by the reading too.
            connection.execute(
                "UPDATE senses SET reading = ? WHERE word = ? AND reading = ?", (new, simplified, old)
            )
            changed += 1
        if changed:
            # The trigram index covers pinyin_toneless (external content), so rebuild it from the table.
            connection.execute("INSERT INTO entries_fts_trigram(entries_fts_trigram) VALUES ('rebuild')")
        connection.commit()
        connection.execute("DETACH DATABASE wikt")
        return changed
    finally:
        connection.close()
