"""Pinyin helpers: convert CEDICT's numeric pinyin to marked/toneless forms.

CEDICT stores pinyin as space-separated syllables with a trailing tone digit,
e.g. "han4 yu3" and "nu:3" (u: means ü).
"""

# Lets us use modern type-hint syntax on older Python versions.
from __future__ import annotations

import re
from functools import lru_cache

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

# Maps a tone-marked character back to its tone number (1-4).
_MARK_TONE = {
    ord(mark): tone
    for marks in _TONE_MARKS.values()
    for tone, mark in enumerate(marks, start=1)
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


@lru_cache(maxsize=100_000)
def numeric_to_marked(pinyin: str) -> str:
    """'han4 yu3' -> 'hàn yǔ'."""
    # Split on whitespace, mark each syllable, then rejoin with single spaces.
    return " ".join(_mark_syllable(tok) for tok in pinyin.split())


@lru_cache(maxsize=200_000)
def to_numeric(pinyin: str) -> str:
    """'hàn yǔ' -> 'han4 yu3' (the inverse of numeric_to_marked)."""
    out = []
    for token in pinyin.split():
        tone = 5  # neutral when the syllable carries no mark
        base = []
        for char in token:
            if ord(char) in _MARK_TONE:
                tone = _MARK_TONE[ord(char)]
                base.append(_MARK_TO_BASE[ord(char)])
            else:
                base.append(char)
        out.append("".join(base) + str(tone))
    return " ".join(out)


_DIGIT_RE = re.compile(r"[1-5]")
_NON_LETTER_RE = re.compile(r"[^a-zü]")


@lru_cache(maxsize=300_000)
def to_toneless(pinyin: str) -> str:
    """Normalize any pinyin to a search key: 'hàn yǔ' / 'han4 yu3' -> 'hanyu'."""
    text = pinyin.strip().lower()
    text = text.replace("u:", "ü").replace("v", "ü")
    text = _DIGIT_RE.sub("", text)
    text = text.translate(_MARK_TO_BASE)
    text = _NON_LETTER_RE.sub("", text)
    return text


# The same syllable tables app.js uses. Kept here so the server and browser
# classify Latin tokens the same way.
_PINYIN_INITIALS = (
    "zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l",
    "g", "k", "h", "j", "q", "x", "r", "z", "c", "s",
)
_PINYIN_FINALS = {
    "a", "o", "e", "ê", "er", "ai", "ei", "ao", "ou", "an", "en", "ang",
    "eng", "i", "ia", "ie", "iao", "iu", "ian", "in", "iang", "ing",
    "iong", "u", "ua", "uo", "uai", "ui", "uan", "un", "uang", "ong",
    "ueng", "ü", "üe", "üan", "ün", "ue",
}
_PINYIN_ZERO = {
    "a", "o", "e", "ai", "ei", "ao", "ou", "an", "en", "ang", "eng",
    "er", "yo", "ê",
}
_PINYIN_EXTRA = {"r", "m", "n", "ng", "hm", "hng", "ê"}
_PINYIN_Y = {
    "yi", "ya", "ye", "yao", "you", "yan", "yin", "yang", "ying", "yong",
    "yu", "yue", "yuan", "yun",
}
_PINYIN_W = {
    "wu", "wa", "wo", "wai", "wei", "wan", "wen", "wang", "weng",
}


def is_pinyin_token(token: str, allow_uppercase: bool = False) -> bool:
    """True when one token is a plausible Mandarin pinyin syllable.

    A single uppercase letter is treated as a Latin initial (A股, K歌, T恤)
    unless `allow_uppercase` is set. The erhua ``r`` and syllabic
    ``m``/``n``/``ng`` are valid; ``w``/``d`` are not.
    """
    raw = (token or "").strip()
    if not raw:
        return False
    if len(raw) == 1 and raw.isupper() and not allow_uppercase:
        return False
    base = to_toneless(raw)
    if not base:
        return False
    if base in _PINYIN_EXTRA or base in _PINYIN_ZERO:
        return True
    if base in _PINYIN_Y or base in _PINYIN_W:
        return True
    for initial in _PINYIN_INITIALS:
        if base.startswith(initial) and base[len(initial) :] in _PINYIN_FINALS:
            return True
    return False


def latin_tokens(pinyin: str) -> list[str]:
    """The tokens of a reading that are not Mandarin pinyin."""
    return [
        token
        for token in (pinyin or "").split()
        if not is_pinyin_token(token)
    ]


def is_standard_pinyin(pinyin: str) -> bool:
    """True when every token is Mandarin pinyin (no Latin word spelled out)."""
    tokens = (pinyin or "").split()
    return bool(tokens) and not latin_tokens(pinyin)


def split_syllables(chunk: str, syllables: set[str]) -> list[str]:
    """Split one chunk into the fewest valid pinyin syllables.

    A small dynamic program: best[i] is the shortest list of syllables covering
    chunk[:i]. If no full split exists, every character is kept separate. Shared
    by the app and the Wiktionary importer so both produce identical readings.
    """
    length = len(chunk)
    best: list[list[str] | None] = [None] * (length + 1)
    best[0] = []
    for start in range(length):
        if best[start] is None:
            continue
        # The longest pinyin syllable is six letters ("zhuang").
        for end in range(start + 1, min(start + 6, length) + 1):
            if to_toneless(chunk[start:end]) not in syllables:
                continue
            candidate = best[start] + [chunk[start:end]]
            if best[end] is None or len(candidate) < len(best[end]):
                best[end] = candidate
    return best[length] if best[length] is not None else list(chunk)


def split_pinyin(text: str, syllables: set[str]) -> str:
    """Space out a run-together reading, e.g. 'língcìyuán' -> 'líng cì yuán'.

    Each whitespace/apostrophe-separated chunk is split into the FEWEST valid
    syllables from `syllables`. Greedy longest matching is not enough:
    "Sānguózhì" would become "Sāng u ó zhì" instead of "Sān guó zhì". Unknown
    pieces are kept as they are, so nothing is dropped.
    """
    parts: list[str] = []
    for chunk in re.split(r"[\s'’\-–]+", text.strip()):
        if chunk:
            parts.extend(split_syllables(chunk, syllables))
    return " ".join(parts)
