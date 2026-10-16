"""Default dictionaries for each search language.

When the user picks a language in the search dropdown (EN / ZH / TW), the
dictionaries listed here are switched on and every other dictionary is switched
off. Edit this mapping to change the defaults.

Keys are the dictionary "anchors" from ``_SOURCE_QUERIES`` in ``db.py``
(e.g. "cedict", "moe", "wiktionary").

Importing this module is cheap and Python caches it, so the values are read from
memory at runtime — no per-request disk access.
"""

# Search language -> the dictionary anchors enabled by default.
DEFAULTS: dict[str, list[str]] = {
    "EN": ["cedict"],
    "ZH": [],  # Mainland Chinese: no dictionaries yet
    "TW": ["moe"],
}
