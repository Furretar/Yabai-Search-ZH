"""Manual per-word overrides - edit here rather than in the code.

Hand-picked corrections for specific headwords, kept declarative so they are easy
to review, add to, or remove. Keys are matched against both the traditional and
simplified form of a word.
"""

# Force a Taiwan (MOE) reading, ignoring whatever the MOE dictionary lists. The
# value is the tone-marked pinyin shown while the Taiwan toggle is on.
TAIWAN_READINGS: dict[str, str] = {
    # Taiwan reads 血 as xiě, not xuè.
    "血": "xiě",
}

# Force which reading a word shows when the dictionary offers several. The value
# is the tone-marked pinyin that the parsed input should display. Keys are
# matched against both the traditional and simplified form.
PREFERRED_READINGS: dict[str, str] = {
    # 了 on its own is the particle "le", not the verb liǎo, even though liǎo
    # has more senses.
    "了": "le",
    # 没 on its own is usually the negating méi, not the verb mò ("to sink"),
    # even though mò has more senses. Use méi when parsing single characters.
    "没": "méi",
}

# Words whose "Other forms" row should be hidden (e.g. when it just repeats a
# pronunciation already shown elsewhere).
HIDE_FORMS: set[str] = {
    "血",
}

# Commons images for IDS headwords that have no Unicode codepoint. Scraped from
# the Wiktionary entry pages (their headword is an image); the app shows these
# instead of the dashed-box "⿰" sequence. Longer headwords can be split into
# these components (e.g. ⿰男疾⿰男戶 -> ⿰男疾 + ⿰男戶).
IDS_IMAGES: dict[str, str] = {
    "⿰扌幸": "/static/ids_images/ids_0a1db781c220.png",
    "⿰男戶": "/static/ids_images/ids_1b5ecad2664e.png",
    "⿰男疾": "/static/ids_images/ids_712fdd95f307.png",
    "⿰禾𰢫": "/static/ids_images/ids_5867cda6a171.png",
    "⿰訁𪢱": "/static/ids_images/ids_c8f6c4dd873b.png",
    "⿱寶⿰寶寶": "/static/ids_images/ids_0d0f1a1c7626.png",
    "⿱𡩧⿺進⿰貝招": "/static/ids_images/ids_7567c4aaf284.png",
}


# ---------------------------------------------------------------------------
# English normalization (moved here from normalize.py).
# CC-CEDICT uses a compact style with abbreviations ("sb." = somebody, etc.).
# This module expands a small, explicit set of them so definitions read
# naturally and are searchable by the full words. The raw text is kept untouched
# in the `english` column; the normalized form is stored separately in
# `english_normalized`, so nothing is lost.
# ---------------------------------------------------------------------------
import re as _re

# Ordered list of (pattern, replacement). Each pattern is matched as a whole
# word (word boundaries) and case-sensitively, so we never rewrite text by
# accident. Add new rules here, grouped and commented.
NORMALIZE_RULES: list[tuple[str, str]] = [
    # people / things (CEDICT writes "sb" with no trailing period)
    (r"\bsb\b", "somebody"),
    # references
    (r"\babbr\.", "abbreviation"),
    # grammar / register
    (r"\bonom\.", "onomatopoeia"),
    (r"\bsth\b", "something"),
    # "cf." (compare) reads more naturally as "see".
    (r"\bcf\.", "see"),
]

# Compile the rules once, at import time (regex compilation is not free).
_NORMALIZE_COMPILED = [
    (_re.compile(pattern), replacement) for pattern, replacement in NORMALIZE_RULES
]


def normalize_english(text: str) -> str:
    """Apply every normalization rule to `text` and return the expanded version."""
    for pattern, replacement in _NORMALIZE_COMPILED:
        text = pattern.sub(replacement, text)
    return text
