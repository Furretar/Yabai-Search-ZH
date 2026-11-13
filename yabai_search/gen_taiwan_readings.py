"""The rules that turn a Mainland reading into a Taiwan reading.

Everything that GENERATES a Taiwan reading lives here and nowhere else. The
functions are pure: they read no database and import nothing from db.py, so the
same code runs when the Taiwan lists are built (scripts/generate-indexes-
optimizations/) and, for a row that is not in a prebuilt list yet, when a page
is decorated. What they need from the databases (MOE readings, how many CEDICT
readings a word has) is passed in.

The rules, in the order `entry_taiwan_reading` applies them:
1. a manual override in overrides.py (TAIWAN_READINGS) wins;
2. the whole-word MOE reading (see `taiwan_reading`);
3. CEDICT's own "Taiwan pr. [...]" note;
4. the per-character derivation from MOE (see `derive_taiwan`).

Changing anything in this file changes what the lists contain, and the stored
signature (db.taiwan_signature) hashes this file, so a stale list is reported.
"""

from __future__ import annotations

import re

from yabai_search.overrides import TAIWAN_READINGS
from yabai_search.pinyin import numeric_to_marked, to_toneless

# Words that never take a Taiwan reading - the word-level MOE reading does not
# fit their CEDICT sense. Add more here as needed.
TAIWAN_EXCEPTIONS = {"啊"}

# CEDICT notes a Taiwan pronunciation as "Taiwan pr. [xia4 hai2]".
TAIWAN_PR_RE = re.compile(r"(?:Taiwan|Taiwanese)\s+pr\.\s*\[([^\]]+)\]")


def word_taiwan(pinyin: str, options: list[str]) -> str | None:
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


def char_taiwan(syllable: str, options: list[str]) -> str | None:
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


def derive_taiwan(
    traditional: str,
    pinyin: str,
    char_map: dict[str, list[str]],
    counts: dict[str, int],
) -> str | None:
    """Build a Taiwan reading character by character from MOE.

    Used for words the MOE dictionary doesn't list whole. Each character is
    swapped for its MOE reading only when that reading is unambiguous (exactly
    one matches the syllable). `counts` is {word: number of CEDICT readings}.
    Returns None when nothing changed.
    """
    chars = list(traditional)
    syllables = pinyin.split()
    if len(chars) != len(syllables):
        return None
    changed = False
    result = []
    for char, syllable in zip(chars, syllables):
        # Don't guess for a polyphonic character: its MOE reading may belong to a
        # different sense (e.g. 腌 āng vs the 腌 ā in 腌臜).
        replacement = None
        if counts.get(char, 1) == 1:
            replacement = char_taiwan(syllable, char_map.get(char, []))
        if replacement:
            result.append(replacement)
            changed = True
        else:
            result.append(syllable)
    return " ".join(result) if changed else None


def cedict_taiwan_reading(text: str) -> str:
    """The marked pinyin from a CEDICT "Taiwan pr. [...]" note, or ""."""
    match = TAIWAN_PR_RE.search(text or "")
    if not match:
        return ""
    # CEDICT may run the syllables together ("xia4hai2"), so split on each
    # letter run + optional tone digit before marking.
    syllables = re.findall(r"[A-Za-zü:]+[0-9]?", match.group(1))
    return numeric_to_marked(" ".join(syllables))


def taiwan_reading(
    word: str, pinyin: str, options: list[str], counts: dict[str, int], char_map: dict
) -> str | None:
    """Return a word's Taiwan reading (marked pinyin), or None.

    Words in `TAIWAN_EXCEPTIONS` get none. A monophonic word may use its
    whole-word MOE reading; a polyphonic word keeps its specific CEDICT reading
    (the MOE reading is keyed by word, not sense). Everything else falls back to
    per-character derivation.
    """
    if word in TAIWAN_EXCEPTIONS:
        return None
    if counts.get(word, 1) == 1:
        reading = word_taiwan(pinyin, options)
        if reading is not None:
            return reading
    return derive_taiwan(word, pinyin, char_map, counts)


def entry_taiwan_reading(
    traditional: str,
    simplified: str,
    pinyin: str,
    english_normalized: str,
    options: list[str],
    counts: dict[str, int],
    char_map: dict,
) -> str | None:
    """The Taiwan reading of one headword row, or None when it has none.

    The single place where the rules are combined (see the module docstring).
    """
    override = TAIWAN_READINGS.get(traditional) or TAIWAN_READINGS.get(simplified)
    if override:
        return override
    if options:
        return taiwan_reading(traditional, pinyin, options, counts, char_map)
    return cedict_taiwan_reading(english_normalized or "") or taiwan_reading(
        traditional, pinyin, options, counts, char_map
    )
