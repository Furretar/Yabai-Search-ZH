"""Every default of the website, in one place: which dictionaries and which display settings.

Edit the values here, not in the page's scripts or the views. The server reads them for the
search API's defaults, and the page receives both tables (JSON in the page) and uses them for
a first visit and for any setting the visitor has not saved. Importing this module is cheap and
Python caches it, so the values are read from memory at runtime - no per-request disk access.
"""

# Search language -> every dictionary anchor in that language category.
#
# When the user picks a language in the search dropdown (EN / ZH / TW / JP), the dictionaries listed
# here are switched on and every other dictionary is switched off. Keys of the inner lists are the
# dictionary "anchors" from ``_SOURCE_QUERIES`` in ``db.py`` (e.g. "cedict", "moe", "wiktionary").
DEFAULTS: dict[str, list[str]] = {
    "EN": ["cedict", "wiktionary", "unihan"],
    "ZH": ["zhwiktionary", "baidu"],
    "TW": ["moe", "moe_revised"],
    "JP": ["jpwiktionary"],
}

# Display settings for a visitor who has not changed them. Names are the page's own
# (the options panel and the URL parameters use the same words).
SETTINGS: dict[str, object] = {
    "lang": "EN",  # the search-language dropdown: a key of DEFAULTS
    "script": "simplified",  # "simplified" or "traditional"
    "phonetic": "pinyin",  # "pinyin" or "zhuyin" (Bopomofo)
    "taiwan": False,  # show Taiwan readings instead of Mainland ones
    "tone_coloring": True,  # colour each syllable by its tone
    "custom_dicts": False,  # the custom-dictionary picker; off means the language's DEFAULTS
    "compact": False,  # one definition per language (always on while custom_dicts is off)
    "auto_clipboard": False,  # search whatever is copied; always asks the browser first
}

# The same settings under the names the page's scripts use (state.toneColoring, ...).
PAGE_SETTINGS: dict[str, object] = {
    "lang": SETTINGS["lang"],
    "script": SETTINGS["script"],
    "phonetic": SETTINGS["phonetic"],
    "taiwan": SETTINGS["taiwan"],
    "toneColoring": SETTINGS["tone_coloring"],
    "customDicts": SETTINGS["custom_dicts"],
    "compact": SETTINGS["compact"],
    "autoClipboard": SETTINGS["auto_clipboard"],
}
