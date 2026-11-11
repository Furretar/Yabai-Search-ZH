"""Default dictionaries for each search language.

When the user picks a language in the search dropdown (EN / ZH / TW / JP),
the dictionaries listed here are switched on and every other dictionary is
switched off. Each list holds every dictionary in that language category. Edit this mapping to change the defaults.

Keys are the dictionary "anchors" from ``_SOURCE_QUERIES`` in ``db.py``
(e.g. "cedict", "moe", "wiktionary").

Importing this module is cheap and Python caches it, so the values are read from
memory at runtime - no per-request disk access.
"""

# Search language -> every dictionary anchor in that language category.
DEFAULTS: dict[str, list[str]] = {
    "EN": ["cedict", "wiktionary", "unihan"],
    "ZH": ["zhwiktionary", "baidu"],
    "TW": ["moe", "moe_revised"],
    "JP": ["jpwiktionary"],
}
