"""Normalize CEDICT English definitions for display and search.

CEDICT uses a compact style with abbreviations ("sb." = somebody, etc.). This
module expands a small, explicit set of them so definitions read naturally and
are searchable by the full words.

The raw text is kept untouched in the `english` column; the normalized form is
stored separately in `english_normalized`, so nothing is lost.
"""

from __future__ import annotations

import re

# Ordered list of (pattern, replacement). Each pattern is matched as a whole
# word (word boundaries) and case-sensitively, so we never rewrite text by
# accident. Add new rules here, grouped and commented.
RULES: list[tuple[str, str]] = [
    # people / things (CEDICT writes "sb" with no trailing period)
    (r"\bsb\b", "somebody"),
    # references
    (r"\babbr\.", "abbreviation"),
    # grammar / register
    (r"\bonom\.", "onomatopoeia"),
    (r"\bsth\b", "something"), 

]

# Compile the rules once, at import time (regex compilation is not free).
_COMPILED = [(re.compile(pattern), replacement) for pattern, replacement in RULES]


def normalize_english(text: str) -> str:
    """Apply every rule to `text` and return the expanded version."""
    for pattern, replacement in _COMPILED:
        text = pattern.sub(replacement, text)
    return text
