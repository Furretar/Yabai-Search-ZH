"""The rules that turn a Mainland reading into a Taiwan reading.

Everything that GENERATES a Taiwan reading lives here and nowhere else. The
functions are pure: they read no database and import nothing from db.py, so the
same code runs when the Taiwan lists are built (scripts/generate-indexes-
optimizations/) and, for a row that is not in a prebuilt list yet, when a page
is decorated. What they need from the databases (the Taiwan dictionary's readings, how many readings a
word has in the headword table) is passed in; no dictionary is named here.

The rules, in the order `entry_taiwan_reading` applies them:
1. a manual override in overrides.py (TAIWAN_READINGS) wins;
2. the whole-word MOE reading (see `taiwan_reading`);
3. a "Taiwan pr. [...]" note in the row's own definition text (a convention some dictionaries use; see
   `taiwan_note_reading`);
4. the per-character derivation (see `derive_taiwan`), from the fixed list TAIWAN_CHAR_READINGS in overrides.py.
A single character's whole-word MOE reading that belongs to another character's sense (万 mò, where "ten
thousand" is 萬 wàn) is corrected by `other_sense_fix`.

Changing anything in this file changes what the lists contain, and the stored
signature (db.taiwan_signature) hashes this file, so a stale list is reported.
"""

from __future__ import annotations

import re

from yabai_search.overrides import TAIWAN_CHAR_READINGS, TAIWAN_READINGS
from yabai_search.pinyin import numeric_to_marked, to_toneless

_S2T = None


def _standard_form(char: str) -> str:
    """The traditional (Taiwan standard) form of one character: 万 -> 萬, 唇 -> 脣 (OpenCC s2t)."""
    global _S2T
    if _S2T is None:
        from opencc import OpenCC  # noqa: PLC0415 (only needed when a reading is checked)

        _S2T = OpenCC("s2t")
    return _S2T.convert(char) if char else char


def other_sense_fix(chars: list[str], syllable: str, taiwan: str, char_map: dict) -> str | None:
    """Correct a character's Taiwan reading that belongs to another character's sense.

    MOE lists a character under its own Taiwan sense, which can be a different word: 万 is mò (the surname
    万俟; "ten thousand" is 萬 in Taiwan), 唇 is zhēn ("lips" is 脣). When MOE's reading `taiwan` sounds
    different from the row's `syllable` and the character's standard form (any of `chars`, converted with
    OpenCC) is another character whose MOE readings include the row's sound, the row means that other
    character: its matching MOE reading is returned (萬 wàn), or `syllable` itself. Returns None when the
    reading is fine as it is.
    """
    if to_toneless(taiwan) == to_toneless(syllable):
        return None
    target = to_toneless(syllable)
    for char in chars:
        standard = _standard_form(char)
        if standard == char or len(standard) != 1:
            continue
        matches = [option for option in char_map.get(standard, []) if to_toneless(option) == target]
        if matches:
            return matches[0] if len(matches) == 1 else syllable
    return None


# Words that never take a Taiwan reading - the word-level MOE reading does not
# fit the sense of the row's own reading. Add more here as needed.
TAIWAN_EXCEPTIONS = {"啊"}

# A definition can note a Taiwan pronunciation as "Taiwan pr. [xia4 hai2]".
TAIWAN_PR_RE = re.compile(r"(?:Taiwan|Taiwanese)\s+pr\.\s*\[([^\]]+)\]")


def word_taiwan(pinyin: str, options: list[str]) -> str | None:
    """Return the Taiwan reading for a whole word from its MOE readings.

    An exact MOE word match always wins. A single reading is used as-is (even if
    it differs from the row's own reading). With several (polyphonic), prefer the one matching
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
    differs from the row's own syllable). With several readings, use one only if
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
    """Build a Taiwan reading character by character, for words the MOE dictionary doesn't list whole.

    A syllable is swapped only for a character in TAIWAN_CHAR_READINGS (overrides.py: one reading in
    CC-CEDICT, one different reading in MOE), and only when the syllable is that CC-CEDICT reading (tone
    aside), so 企 qǐ in 企業 becomes qì. `char_map` and `counts` are no longer read here; they stay in the
    signature for the callers. Returns None when nothing changed.
    """
    chars = list(traditional)
    syllables = pinyin.split()
    if len(chars) != len(syllables):
        return None
    changed = False
    result = []
    for char, syllable in zip(chars, syllables):
        entry = TAIWAN_CHAR_READINGS.get(char)
        if entry and to_toneless(syllable) == to_toneless(entry[0]):
            result.append(entry[1])
            changed = True
        else:
            result.append(syllable)
    return " ".join(result) if changed else None


def taiwan_note_reading(text: str) -> str:
    """The marked pinyin from a "Taiwan pr. [...]" note in a definition's text, or ""."""
    match = TAIWAN_PR_RE.search(text or "")
    if not match:
        return ""
    # The note may run the syllables together ("xia4hai2"), so split on each
    # letter run + optional tone digit before marking.
    syllables = re.findall(r"[A-Za-zü:]+[0-9]?", match.group(1))
    return numeric_to_marked(" ".join(syllables))


def taiwan_reading(
    word: str, pinyin: str, options: list[str], counts: dict[str, int], char_map: dict
) -> str | None:
    """Return a word's Taiwan reading (marked pinyin), or None.

    Words in `TAIWAN_EXCEPTIONS` get none. A monophonic word may use its
    whole-word MOE reading; a polyphonic word keeps its own specific reading
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
    note_text: str,
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
        reading = taiwan_reading(traditional, pinyin, options, counts, char_map)
    else:
        reading = taiwan_note_reading(note_text or "") or taiwan_reading(
            traditional, pinyin, options, counts, char_map
        )
    if reading and len(traditional) == 1:
        # A single character's whole-word MOE reading may be another character's sense (万 mò, 唇 zhēn).
        fixed = other_sense_fix([traditional, simplified], pinyin, reading, char_map)
        if fixed:
            return None if fixed.lower() == pinyin.lower() else fixed
    return reading
