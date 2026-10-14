"""Pinyin helpers: convert CEDICT's numeric pinyin to marked/toneless forms.

CEDICT stores pinyin as space-separated syllables with a trailing tone digit,
e.g. "han4 yu3" and "nu:3" (u: means ü).
"""

# Lets us use modern type-hint syntax on older Python versions.
from __future__ import annotations

import re

# Maps each base vowel to its four tone-marked forms, in tone order 1-4.
# Neutral tone (5, or no digit) leaves the vowel unmarked, so it isn't listed.
# Uppercase variants are needed because CEDICT capitalizes surnames/place names.
_TONE_MARKS = {
    "a": "āáǎà",
    "e": "ēéěè",
    "i": "īíǐì",
    "o": "ōóǒò",
    "u": "ūúǔù",
    "ü": "ǖǘǚǜ",
    "A": "ĀÁǍÀ",
    "E": "ĒÉĚÈ",
    "I": "ĪÍǏÌ",
    "O": "ŌÓǑÒ",
    "U": "ŪÚǓÙ",
    "Ü": "ǕǗǙǛ",
}

# Reverse of the map above: maps a tone-marked character (looked up by its
# Unicode code point) back to its plain base vowel, so we can strip tone marks.
_MARK_TO_BASE = {
    ord(marked): base for base, marks in _TONE_MARKS.items() for marked in marks
}

# Matches one pinyin syllable: letters (plus "u:" for ü) with an optional
# trailing tone digit 1-5. Used to separate the syllable body from its tone.
_SYLLABLE_RE = re.compile(r"^([A-Za-zü:]+)([1-5])?$")


def _mark_syllable(syllable: str) -> str:
    """Add the correct tone mark to one numeric syllable, e.g. 'han4' -> 'hàn'."""
    match = _SYLLABLE_RE.match(syllable)
    if not match:
        return syllable  # not a recognized syllable: leave it untouched

    # group(1) = the letters, group(2) = the tone digit (or None).
    body, tone = match.group(1), match.group(2)
    # Normalize the two ways ü can be written ("u:" and "v") to a real ü.
    body = body.replace("u:", "ü").replace("v", "ü").replace("V", "Ü")
    # No digit, or neutral tone (5): there is no mark to add.
    if tone is None or tone == "5":
        return body

    # Decide WHICH vowel receives the mark. The Hanyu Pinyin rule is:
    #   'a' or 'e' always wins; otherwise the 'o' in "ou"; otherwise the last vowel.
    idx = -1
    lower = body.lower()
    if "a" in lower:
        idx = lower.index("a")
    elif "e" in lower:
        idx = lower.index("e")
    elif "ou" in lower:
        idx = lower.index("o")
    else:
        # Fall back to the last vowel among i/o/u/ü
        # (this handles "iu" -> mark u, "ui" -> mark i, etc.).
        for i, ch in enumerate(lower):
            if ch in "iouü":
                idx = i
    if idx == -1:
        return body  # no vowel found: nothing to mark

    marks = _TONE_MARKS.get(body[idx], "")
    if len(marks) != 4:
        return body  # a vowel we can't mark (e.g. 'n'): leave as-is
    # Swap the chosen vowel for its tone-marked form. The digit is 1-4, so the
    # 0-based index into the 4-character string is tone-1.
    return body[:idx] + marks[int(tone) - 1] + body[idx + 1 :]


def numeric_to_marked(pinyin: str) -> str:
    """'han4 yu3' -> 'hàn yǔ'."""
    # Split on whitespace, mark each syllable, then rejoin with single spaces.
    return " ".join(_mark_syllable(tok) for tok in pinyin.split())


def to_toneless(pinyin: str) -> str:
    """Normalize any pinyin to a search key: 'hàn yǔ' / 'han4 yu3' -> 'hanyu'."""
    text = pinyin.strip().lower()  # case-insensitive
    text = text.replace("u:", "ü").replace("v", "ü")  # normalize ü spellings
    text = re.sub(r"[1-5]", "", text)  # drop tone numbers
    text = text.translate(_MARK_TO_BASE)  # strip tone marks
    text = re.sub(r"[^a-zü]", "", text)  # drop spaces and punctuation
    return text
