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

# Words whose "Other forms" row should be hidden (e.g. when it just repeats a
# pronunciation already shown elsewhere).
HIDE_FORMS: set[str] = {
    "血",
}
