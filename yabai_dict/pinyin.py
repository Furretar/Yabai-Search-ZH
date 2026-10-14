"""Pinyin helpers: convert CEDICT's numeric pinyin to marked/toneless forms.

CEDICT stores pinyin as space-separated syllables with a trailing tone digit,
e.g. "han4 yu3" and "nu:3" (u: means ü).
"""

from __future__ import annotations

import re

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

_MARK_TO_BASE = {
    ord(marked): base for base, marks in _TONE_MARKS.items() for marked in marks
}

_SYLLABLE_RE = re.compile(r"^([A-Za-zü:]+)([1-5])?$")


def _mark_syllable(syllable: str) -> str:
    match = _SYLLABLE_RE.match(syllable)
    if not match:
        return syllable
    body, tone = match.group(1), match.group(2)
    body = body.replace("u:", "ü").replace("v", "ü").replace("V", "Ü")
    if tone is None or tone == "5":
        return body

    idx = -1
    lower = body.lower()
    if "a" in lower:
        idx = lower.index("a")
    elif "e" in lower:
        idx = lower.index("e")
    elif "ou" in lower:
        idx = lower.index("o")
    else:
        for i, ch in enumerate(lower):
            if ch in "iouü":
                idx = i
    if idx == -1:
        return body

    marks = _TONE_MARKS.get(body[idx], "")
    if len(marks) != 4:
        return body
    return body[:idx] + marks[int(tone) - 1] + body[idx + 1 :]


def numeric_to_marked(pinyin: str) -> str:
    """'han4 yu3' -> 'hàn yǔ'."""
    return " ".join(_mark_syllable(tok) for tok in pinyin.split())


def to_toneless(pinyin: str) -> str:
    """Normalize any pinyin to a search key: 'hàn yǔ' / 'han4 yu3' -> 'hanyu'."""
    text = pinyin.strip().lower()
    text = text.replace("u:", "ü").replace("v", "ü")
    text = re.sub(r"[1-5]", "", text)
    text = text.translate(_MARK_TO_BASE)
    text = re.sub(r"[^a-zü]", "", text)
    return text
