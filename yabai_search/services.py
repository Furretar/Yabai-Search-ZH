"""Search logic for Yabai Search, independent of any web framework.

This is the former yabai_search/main.py with the FastAPI routing removed. The
functions keep their original names and behaviour; the Django views in
dictionary/views.py call them. Nothing in here imports Django or FastAPI.
"""

from __future__ import annotations

import collections
import copy
import hashlib
import json
import os


import threading
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode

from yabai_search import db, merge as merge_results, segment
from yabai_search import perf_debug as pd
from yabai_search.default_dictionaries import DEFAULTS
from yabai_search.overrides import PREFERRED_READINGS
from yabai_search.pinyin import to_numeric, to_toneless

# Search-language dropdown value -> the source's `lang` field. The fast preview
# picks the first selected dictionary in this group, so drag order matters.
_LANG_GROUP = {
    "EN": "english",
    "ZH": "mainland",
    "TW": "taiwan",
    "JP": "japanese",
}


class HeaderBag:
    """Collects response headers a handler wants set; the view copies them over."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


# Directory of this file (the yabai_search package). Used to locate assets
# regardless of the directory the server is launched from.
BASE = Path(__file__).resolve().parent

# The About page is rendered from this file on every request, so editing the
# README updates the page without restarting the server.
_README_PATH = BASE.parent / "README.md"

# Directory holding the static assets served under /static/.
_STATIC_DIR = BASE / "static"


def served_by() -> str:
    """Which copy answers: laptop, tablet or dev, set by deploy/run.sh.

    Used by the X-Served-By header, the hamburger menu and the search log, so
    the fallback name lives here instead of being repeated at each call site.
    """
    return os.environ.get("YABAI_SERVED_BY", "unknown")

# Normal (non-wildcard) searches are paged as well. Fetch up to this many
# matches so the total and the requested page can be computed in one pass.
# The full request ranks every match so `total` and later pages are exact.
# 1e9 is a sentinel larger than any possible result set, not a user-facing cap.
# YABAI_UNCAPPED=0 restores the old 5,000-row sample for A/B comparison only.
_MAX_NORMAL_RESULTS = (
    5000 if os.environ.get("YABAI_UNCAPPED") == "0" else db.UNBOUNDED
)

# Env flags that change which rows are returned or how they are ordered.
_RESULT_ENV_FLAGS = (
    "YABAI_UNCAPPED",
    "YABAI_SERVER_ORDER",
    "YABAI_SERVER_MERGE",
    "YABAI_SEARCH_INDEX",
    "YABAI_GLOSS_LEGACY",
    "YABAI_RELEVANCE",
)


def _code_fingerprint() -> str:
    """Short hash of the search code and result-affecting env flags.

    A restart must not invalidate Cloudflare's edge cache if no file changed;
    editing a module or flipping one of these flags must invalidate it.
    """
    digest = hashlib.sha256()
    for path in sorted(BASE.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    for name in _RESULT_ENV_FLAGS:
        digest.update(f"{name}={os.environ.get(name, '')}\n".encode())
    return digest.hexdigest()[:8]


_CODE_VERSION = _code_fingerprint()


def search_version() -> str:
    """Cache version for /api/search: data fingerprint plus code fingerprint."""
    return f"{db.data_version()}-{_CODE_VERSION}"


# The two merged layouts for a page, cached across identical requests. The key
# covers everything that changes the ranked rows, plus the data version.
_page_layout_cache: "collections.OrderedDict[tuple, tuple[list[dict], list[dict], bool]]" = collections.OrderedDict()
_page_layout_lock = threading.Lock()
_PAGE_LAYOUT_CACHE_MAX = 128


def _page_layouts_cached(
    key: tuple, rows: list[dict], taiwan: bool
) -> tuple[list[dict], list[dict], bool]:
    """Both script layouts for one page, cached across identical requests."""
    with _page_layout_lock:
        hit = _page_layout_cache.get(key)
        if hit is not None:
            _page_layout_cache.move_to_end(key)
            return hit
    simplified, traditional, alt_same = merge_results.page_layouts(rows, taiwan)
    with _page_layout_lock:
        _page_layout_cache[key] = (simplified, traditional, alt_same)
        _page_layout_cache.move_to_end(key)
        while len(_page_layout_cache) > _PAGE_LAYOUT_CACHE_MAX:
            _page_layout_cache.popitem(last=False)
    return simplified, traditional, alt_same


# --- Rank cache ------------------------------------------------------------
# The full ranked list for a query is expensive to rebuild (db.search, variant
# expansion, dialect merge, category ranking). Cache that whole list and slice
# it per page; only the page slice is copied and decorated.
_RANK_CACHE_ROW_BYTES = 2048
_RANK_CACHE_ENABLED = os.environ.get("YABAI_RANK_CACHE", "1") != "0"


def _rank_cache_budget_rows() -> int:
    """Row budget from MemAvailable, capped at 128 MB (override in MB)."""
    if not _RANK_CACHE_ENABLED:
        return 0
    override = os.environ.get("YABAI_RANK_CACHE_MB")
    if override:
        try:
            mb = float(override)
        except ValueError:
            mb = 64.0
    else:
        mb = 64.0
        try:
            with open("/proc/meminfo", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        available_kb = int(line.split()[1])
                        mb = min(128.0, available_kb / 1024.0 * 0.04)
                        break
        except (OSError, ValueError):
            pass
    return max(0, int(mb * 1024 * 1024 / _RANK_CACHE_ROW_BYTES))


_RANK_CACHE_MAX_ROWS = _rank_cache_budget_rows()
# Lists larger than half the budget are still returned, just not stored.
_RANK_CACHE_STORE_ROWS = _RANK_CACHE_MAX_ROWS // 2
_rank_cache: "collections.OrderedDict[tuple, tuple]" = collections.OrderedDict()
_rank_cache_lock = threading.Lock()
_rank_cache_events: dict[tuple, threading.Event] = {}


def _rank_cache_total_rows_locked() -> int:
    return sum(len(value[0]) + len(value[1]) for value in _rank_cache.values())


def _evict_rank_cache_locked() -> None:
    total = _rank_cache_total_rows_locked()
    while total > _RANK_CACHE_MAX_ROWS and _rank_cache:
        _, value = _rank_cache.popitem(last=False)
        total -= len(value[0]) + len(value[1])


def _rank_cache_get_or_compute(key: tuple, compute):
    """Return (state, status) with single-flight computation per key.

    Status is "hit", "miss", or "skipped" (cache disabled or budget zero).
    Identical concurrent requests wait for the leader instead of each running
    the whole search.
    """
    if not _RANK_CACHE_ENABLED or _RANK_CACHE_MAX_ROWS <= 0:
        return compute(), "skipped"
    while True:
        with _rank_cache_lock:
            hit = _rank_cache.get(key)
            if hit is not None:
                _rank_cache.move_to_end(key)
                return hit, "hit"
            event = _rank_cache_events.get(key)
            if event is None:
                event = threading.Event()
                _rank_cache_events[key] = event
                break
        event.wait()
    try:
        value = compute()
    except BaseException:
        with _rank_cache_lock:
            _rank_cache_events.pop(key, None)
            event.set()
        raise
    with _rank_cache_lock:
        _rank_cache_events.pop(key, None)
        event.set()
        rows, fuzzy = value[0], value[1]
        if len(rows) + len(fuzzy) <= _RANK_CACHE_STORE_ROWS:
            _rank_cache[key] = value
            _rank_cache.move_to_end(key)
            _evict_rank_cache_locked()
    return value, "miss"


def _page_groups_copy(
    rows: list[dict], offset: int, limit: int
) -> tuple[list[dict], int]:
    """Same result as db.page_groups, without mutating the cached rows."""
    groups = db.group_rows(rows)
    page = groups[offset : offset + limit]
    page_rows: list[dict] = []
    for group in page:
        key = group[0].get("simplified") or group[0].get("traditional") or ""
        for row in group:
            copied = copy.deepcopy(row)
            copied["group"] = key
            page_rows.append(copied)
    return page_rows, len(groups)


def _normal_search_state(
    q: str, limit: int, taiwan: bool, selected_dicts: list[str]
) -> tuple[list[dict], list[dict], bool, bool, dict, dict]:
    """Everything a normal search computes before paging, ready to cache."""
    timings: dict[str, float] = {}
    counters: dict[str, object] = {}

    def mark(name: str, at: float) -> None:
        timings[name] = round(
            timings.get(name, 0.0) + (time.perf_counter() - at) * 1000, 2
        )

    at = time.perf_counter()
    with pd.stage("db_search"):
        entries, pinyin_match = db.search(
            q, _MAX_NORMAL_RESULTS, taiwan, hydrate=0
        )
    mark("db", at)
    at = time.perf_counter()
    with pd.stage("expand"):
        results = _expand_variants(entries)
    mark("expand", at)
    counters["raw"] = len(entries)
    counters["expanded"] = len(results)
    fuzzy: list[dict] = []
    variant = db.tonal_variant(q)
    if variant and not results:
        at = time.perf_counter()
        with pd.stage("fuzzy_db"):
            fuzzy, _ = db.search(variant, limit, taiwan)
        mark("fuzzy_db", at)
    elif variant and _has_explicit_tones(q):
        seen = {
            (entry.get("traditional"), entry.get("simplified"))
            for entry in results
        }
        at = time.perf_counter()
        with pd.stage("fuzzy_db"):
            variant_results, _ = db.search(variant, limit, taiwan)
        mark("fuzzy_db", at)
        at = time.perf_counter()
        fuzzy = [
            entry
            for entry in _expand_variants(variant_results)
            if (entry.get("traditional"), entry.get("simplified")) not in seen
        ]
        mark("fuzzy_expand", at)
    # Only a Chinese or numeric headword match should hide definition-only
    # matches. A Latin headword such as "word姐" must not collapse an English
    # definition search to that single row.
    if not pinyin_match and (db.has_cjk(q) or q.isdigit()):
        at = time.perf_counter()
        forms = db.headword_query_forms(q)
        if forms:
            def _entry_words(entry):
                words = [
                    entry.get("simplified", ""),
                    entry.get("traditional", ""),
                ]
                for form in entry.get("forms", []) or []:
                    words.append(form.get("traditional", ""))
                    words.append(form.get("simplified", ""))
                return [word for word in words if word]

            headword_matches = [
                entry
                for entry in results
                if any(
                    form in word
                    for form in forms
                    for word in _entry_words(entry)
                )
            ]
            if headword_matches:
                results = headword_matches
        mark("filter", at)
        counters["filtered"] = len(results)
    at = time.perf_counter()
    if pinyin_match:
        dialect = []
    else:
        with pd.stage("dialect_entries"):
            dialect = db.dialect_entries(q)
    mark("dialect", at)
    counters["dialect"] = len(dialect)
    if dialect:
        at = time.perf_counter()
        expanded = _expand_variants(dialect)
        if db._is_language_query(q):
            results = db._merge_unique(results, expanded)
        else:
            results = db._merge_unique(expanded, results)
        mark("dialect_merge", at)
    english_match = False
    if pinyin_match:
        with pd.stage("english_hint"):
            english_match = db.has_english_match(q.strip())
    if selected_dicts:
        at = time.perf_counter()
        with pd.stage("category_rank"):
            results = db.rank_by_category(
                results,
                selected_dicts,
                q,
                taiwan,
                definition_search=not pinyin_match,
            )
        mark("category_rank", at)
        counters["selected_dicts"] = ",".join(selected_dicts)
    return results, fuzzy, pinyin_match, english_match, counters, timings


def _is_chinese_wildcard(q: str) -> bool:
    """True for a "*"/"?" pattern whose literal part contains Han characters."""
    text = (q or "").strip()
    if not any(c in text for c in "*?＊？"):
        return False
    literal = db.to_simplified(text)
    for char in ("*", "?", "＊", "？"):
        literal = literal.replace(char, "")
    return db.has_cjk(literal)


def _wildcard_search_state(
    q: str, taiwan: bool, selected_dicts: list[str]
) -> tuple[list[dict], list[dict], bool, bool, dict, dict]:
    """Ranked rows for a Chinese wildcard pattern, ready to cache."""
    timings: dict[str, float] = {}
    counters: dict[str, object] = {}

    def mark(name: str, at: float) -> None:
        timings[name] = round(
            timings.get(name, 0.0) + (time.perf_counter() - at) * 1000, 2
        )

    at = time.perf_counter()
    with pd.stage("wildcard"):
        candidates = db.wildcard_candidates(q) or []
    mark("wildcard", at)
    at = time.perf_counter()
    with pd.stage("expand"):
        results = _expand_variants(candidates)
    mark("expand", at)
    counters["raw"] = len(candidates)
    counters["expanded"] = len(results)
    if selected_dicts:
        at = time.perf_counter()
        with pd.stage("category_rank"):
            results = db.rank_by_category(results, selected_dicts, q, taiwan)
        mark("category_rank", at)
        counters["selected_dicts"] = ",".join(selected_dicts)
    return results, [], False, False, counters, timings


def _load_reports(limit: int = 200) -> list[dict]:
    """Read the newest JSONL bug reports, newest first."""
    path = BASE.parent / "data" / "bug_reports.jsonl"
    if not path.exists():
        return []
    reports: collections.deque[dict] = collections.deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    reports.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return list(reversed(reports))


# Display settings that a report can carry back to the search page. Keep the
# list small and fixed so a report cannot inject arbitrary query parameters.
_REPORT_SETTING_KEYS = (
    "script",
    "toneColoring",
    "phonetic",
    "taiwan",
    "customDicts",
    "compact",
    "lang",
    "selectedDictionaries",
)


def _clean_report_settings(settings: object) -> dict:
    """Keep only the known, bounded display settings from a report."""
    if not isinstance(settings, dict):
        return {}
    cleaned: dict = {}
    for key in _REPORT_SETTING_KEYS:
        if key not in settings:
            continue
        value = settings[key]
        if key == "selectedDictionaries":
            if isinstance(value, list):
                cleaned[key] = [
                    str(anchor)[:80]
                    for anchor in value[:100]
                    if str(anchor).strip()
                ]
        elif isinstance(value, bool):
            cleaned[key] = value
        elif isinstance(value, (int, float)):
            cleaned[key] = value
        elif isinstance(value, str):
            cleaned[key] = value[:80]
    return cleaned


def _report_settings(report: dict) -> dict:
    """Return the display settings stored with a report.

    New reports carry a settings object. Older reports only have the debug log,
    which embeds the same values under currentState; read that as a fallback so
    old report links still restore the reporter's view.
    """
    settings = _clean_report_settings(report.get("settings"))
    if settings:
        return settings
    log = report.get("log")
    if not isinstance(log, str):
        return {}
    start = log.find("{")
    if start == -1:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(log[start:])
    except (json.JSONDecodeError, ValueError):
        return {}
    state = data.get("currentState") if isinstance(data, dict) else None
    return _clean_report_settings(state)


def _report_search_url(report: dict) -> str:
    """Build a search link that restores the reporter's display settings."""
    word = str(report.get("word") or "").strip()
    if not word:
        return ""
    settings = _report_settings(report)
    params: dict[str, str] = {"q": word}
    # Map the stored setting names to short, stable URL parameter names.
    mapping = {
        "script": "script",
        "toneColoring": "tone",
        "phonetic": "phonetic",
        "taiwan": "taiwan",
        "customDicts": "custom",
        "compact": "compact",
        "lang": "lang",
    }
    for source_key, param_key in mapping.items():
        if source_key not in settings:
            continue
        value = settings[source_key]
        if isinstance(value, bool):
            params[param_key] = "1" if value else "0"
        elif value not in (None, ""):
            params[param_key] = str(value)[:80]
    dictionaries = settings.get("selectedDictionaries")
    if isinstance(dictionaries, list) and dictionaries:
        params["dicts"] = ",".join(str(anchor)[:80] for anchor in dictionaries)
    return "/?" + urlencode(params)


def asset_version() -> str:
    """A cache-busting tag for the static assets: the newest mtime in static/.

    Computed on every request (not just at startup), so editing style.css or
    app.js changes the URL the browser asks for and it fetches the fresh file -
    no server restart needed. Covers every file in static/, not just app.js.
    """
    try:
        newest = max(path.stat().st_mtime for path in _STATIC_DIR.iterdir() if path.is_file())
    except (FileNotFoundError, ValueError):
        return "0"
    return str(int(newest))



def warm_caches() -> None:
    """Build the SQL and segmenter caches now, not during the first search."""
    # warm_caches starts the page-cache read in a daemon thread (default);
    # YABAI_WARM_PAGE_CACHE=legacy restores the old synchronous warm-up.
    db.warm_caches()
    segment.warm()
    # Fill the parser's reading cache in the background, so common pasted words
    # skip the cold lookup path without delaying startup. Tests disable it to
    # keep the timing-sensitive browser test stable.
    if os.environ.get("YABAI_WARM_LOOKUPS", "1") != "0":
        threading.Thread(target=db.warm_lookup_readings, daemon=True).start()
    print(
        "fast paths: "
        + json.dumps(db.fast_path_status(), ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )
    # The Taiwan toggle searches the prefilled table; say so loudly if it is
    # missing or was built from different sources, overrides or logic.
    taiwan_status = db.taiwan_table_status()
    if taiwan_status != "ok":
        print(
            f"WARNING: data/taiwan_readings.sqlite is {taiwan_status}; the Taiwan "
            "toggle falls back to slower, less complete matching. Rebuild it with "
            "`python scripts/build_taiwan_readings.py` (see AGENTS.md).",
            flush=True,
        )




# CC0 male-voice MP3s from github.com/cjhoward/cedict-tts, served by jsDelivr
# with the right audio content type, CORS, and range requests.
_AUDIO_BASE = "https://cdn.jsdelivr.net/gh/cjhoward/cedict-tts@master/male/"


def _audio_slug(traditional: str, pinyin: str) -> str | None:
    """Filename used by cjhoward/cedict-tts, or None when it was skipped.

    Files are named from numeric pinyin with no spaces ("lv4shi1.mp3"). The
    generator only produced audio when there was one syllable per character,
    and it applies the 一/不 tone-change rules first.
    """
    if not traditional or not pinyin:
        return None
    numeric = to_numeric(pinyin).lower().replace("u:", "v").replace("ü", "v")
    syllables = numeric.split()
    if len(syllables) != len(traditional):
        return None
    corrected: list[str] = []
    for index, char in enumerate(traditional):
        syllable = syllables[index]
        if (
            char in "一不"
            and index + 1 < len(syllables)
            and syllable[-1:].isdigit()
        ):
            tone = int(syllable[-1])
            next_tone = 0
            if db._is_han(traditional[index + 1]):
                following = syllables[index + 1]
                next_tone = int(following[-1]) if following[-1:].isdigit() else 5
            if next_tone == 4:
                tone = 2
            elif next_tone != 5:
                tone = 4
            syllable = syllable[:-1] + str(tone)
        if syllable == "r5":
            syllable = "er5"
        corrected.append(syllable)
    slug = ""
    for index, syllable in enumerate(corrected):
        if len(syllable) >= 2 and syllable[-1:].isdigit():
            slug += syllable
        else:
            if index > 0 and not slug.endswith("-"):
                slug += "-"
            slug += syllable
            if index < len(corrected) - 1:
                slug += "-"
    return slug or None



def _reading_count(entry: dict) -> int:
    """How many CEDICT definitions this entry's own reading has.

    A lazy-hydration row (see db.search's `hydrate`) already carries the count
    as `_reading_count`; rows from the normal, fully decorated path do not, and
    the count is read from their definitions instead.
    """
    if "_reading_count" in entry:
        return entry["_reading_count"]
    pinyin = entry.get("pinyin") or ""
    key = db.reading_key(pinyin)
    for definition in entry.get("definitions", []):
        if definition.get("anchor") == "cedict":
            groups = definition.get("groups", [])
            # Exact reading first: "Gān" (surname) and "gān" are different groups.
            group = next((g for g in groups if g.get("pinyin") == pinyin), None)
            if group is None:
                group = next(
                    (g for g in groups if db.reading_key(g.get("pinyin")) == key), None
                )
            if group is not None:
                # "variant of X" senses are pointers, not meanings.
                return sum(
                    1
                    for sense in group.get("senses", [])
                    if not db.is_xref_sense(sense)
                )
    return 0


def _preferred_bonus(entry: dict) -> int:
    """A large score bonus when an entry matches a manual preferred reading.

    Lets overrides.py decide which reading represents a headword (e.g. 了 -> le)
    even when another reading has more definitions.
    """
    preferred = (
        PREFERRED_READINGS.get(entry["traditional"])
        or PREFERRED_READINGS.get(entry["simplified"])
    )
    if preferred and db.reading_key(entry.get("pinyin", "")) == db.reading_key(
        preferred
    ):
        return 1_000_000
    return 0


def _has_explicit_tones(query: str) -> bool:
    """True when the query spells out tones (digits or Bopomofo marks)."""
    return any(char.isdigit() for char in query) or any(
        0x3100 <= ord(char) <= 0x312F for char in query
    )


def _readings_like(candidates: list[dict], pinyin: str, strict: bool) -> list[dict]:
    """The candidates that carry `pinyin`, comparing tones first.

    Exact tone-aware match first (zhī only matches zhī). Unless `strict`, fall
    back to the toneless match (a variant whose own tone differs from its
    target's, e.g. a neutral-tone variant) and finally to an empty list so the
    caller can decide what to do.
    """
    key = db.reading_key(pinyin)
    exact = [c for c in candidates if db.reading_key(c.get("pinyin")) == key]
    if exact or strict:
        return exact
    loose = to_toneless(pinyin or "")
    return [c for c in candidates if to_toneless(c.get("pinyin") or "") == loose]


def _expand_variants(results: list[dict]) -> list[dict]:
    """Collapse results to one entry per headword.

    "see X" / "variant of X" entries become X's own entries (other variants are
    shown under X's "Other forms"). A redirect lands on the target's *same
    reading*, compared with its tone: 祇 [zhǐ] -> 只 [zhǐ], never 隻 [zhī]. When
    a headword has several readings, keep the one with the most definitions, so
    e.g. 落 shows the standard `luò` (12 senses) rather than the rare `lào`
    (1 sense).

    Each returned entry carries `reading_score`: how strongly its reading
    represents the headword. The front end uses it to pick which reading a
    merged simplified-script row shows (only rows with the same simplified form
    merge).
    """
    best: dict[tuple, tuple[int, dict]] = {}
    order: list[tuple] = []
    # Pre-index results by traditional form so a redirect whose target is already
    # in this result set can reuse the decorated entry instead of triggering
    # another db.lookup() (which fully decorates it).
    by_trad: dict[str, list[dict]] = {}
    for entry in results:
        if entry.get("traditional"):
            by_trad.setdefault(entry["traditional"], []).append(entry)
    # First pass: collect every redirect target that is not already in this
    # result set. One batched lookup replaces one query per target.
    targets: set[str] = set()
    for entry in results:
        if not entry.get("main"):
            continue
        target = entry["main"]
        want = entry.get("pinyin") or ""
        local = [c for c in by_trad.get(target, []) if not c.get("main")]
        if not (_readings_like(local, want, strict=True) if want else []):
            targets.add(target)
    looked_by_target = db.lookup_many(sorted(targets)) if targets else {}
    for entry in results:
        if entry.get("main"):
            target = entry["main"]
            want = entry.get("pinyin") or ""
            # The target is a *traditional* form, so only its own rows count:
            # looking it up by simplified too would pull in every spelling that
            # simplifies to the same character (only 只 is meant, not 隻 / 秖).
            local = [c for c in by_trad.get(target, []) if not c.get("main")]
            shown = _readings_like(local, want, strict=True) if want else []
            if not shown:
                looked = [
                    e for e in looked_by_target.get(target, []) if not e.get("main")
                ]
                on_target = [
                    e for e in looked
                    if e["traditional"] == target or e["simplified"] == target
                ] or looked
                # A variant only redirects within its own reading: for "gan", the
                # gān old-variant 乹 must not pull in 乾's unrelated qián readings.
                shown = (
                    _readings_like(on_target, want, strict=False) if want else []
                ) or on_target
        else:
            shown = [entry]
        for e in shown:
            # De-duplicate by headword, not reading: the capitalized (surname) and
            # plain versions of a word (e.g. 阮 Ruǎn / ruǎn) collapse to one.
            key = (e["traditional"], e["simplified"])
            score = _reading_count(e) + _preferred_bonus(e)
            if key not in best:
                best[key] = (score, e)
                order.append(key)
            elif score > best[key][0]:
                best[key] = (score, e)
    expanded = []
    for key in order:
        score, e = best[key]
        # A copy, so a cached lookup() entry is never mutated.
        expanded.append({**e, "reading_score": score})
    return expanded


# Debug log: the last few searches, with each SQL statement's timing (see
# db.start_log). The per-request instrumentation is on by default.
SEARCH_LOG_ENTRIES = 5  # how many searches data/search.log keeps
_SEARCH_LOG_PATH = db.DATA_DIR / "search.log"
_recent_searches: "collections.deque[dict]" = collections.deque(
    maxlen=SEARCH_LOG_ENTRIES
)
_recent_searches_lock = threading.Lock()


def _record_search(
    query: str,
    duration_ms: float,
    count: int,
    statements: list[str],
    part: str = "all",
) -> None:
    """Append a complete search to the rolling log and rewrite the log file."""
    if not db.SQL_LOG:
        return  # disabled with YABAI_SQL_LOG=0
    if part == "primary":
        # The preliminary CEDICT request is repeated by the complete request;
        # logging only the complete one keeps one entry per user search.
        return
    with _recent_searches_lock:
        _recent_searches.append(
            {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "host": served_by(),
                "query": query,
                "ms": round(duration_ms, 1),
                "results": count,
                "sql": statements,
            }
        )
        try:
            with _SEARCH_LOG_PATH.open("w", encoding="utf-8") as handle:
                for entry in _recent_searches:
                    handle.write(
                        f"[{entry['at']}] [{entry['host']}] {entry['query']!r} -> "
                        f"{entry['results']} results in {entry['ms']} ms\n"
                    )
                    for statement in entry["sql"]:
                        handle.write(f"    {statement.strip()}\n")
                    handle.write("\n")
        except OSError:
            pass  # logging must never break a search



def recent_searches() -> dict:
    """The last few searches with their SQL timings (for the debug log)."""
    with _recent_searches_lock:
        return {"entries": [dict(entry) for entry in _recent_searches]}


def api_search(
    response: HeaderBag,
    q: str = "",
    limit: int = 25,
    taiwan: bool = False,
    offset: int = 0,
    part: str = "all",
    lang: str = "EN",
    exact: bool = False,
    dicts: str = "",
    script: str = "simplified",
    merge: bool = False,
):
    """JSON search endpoint. `q` is the term; `limit` is clamped to 1..25.

    `script` is the display script ("simplified" or "traditional"). It decides
    which merged layout is returned when the server merge is enabled.
    `taiwan` lets a pinyin search also match Taiwan (MOE) readings.
    `pinyin_match` is true when the query was an exact Chinese reading - in which
    case `results` holds only those readings, no English-definition matches.
    A "*" query lists every headword from every source, `ALL_PAGE_SIZE` per page;
    `offset` selects the page and the response's `total` is the full count. Every
    other search returns at most 25 rows per page; tone-insensitive "fuzzy"
    matches are placed after all official results and paginated with them.

    `part="primary"` returns only the fast tier (the first dictionary of the
    selected search language), so the page can render results progressively; the
    plain request returns the complete final list.

    `dicts` is a comma-separated list of the dictionary anchors the user has
    selected. When given, the complete request orders every match by category
    (a definition in a selected dictionary first) before it is paged, so the
    category order holds across pages and not only inside one page.
    """
    # Normalize CJK punctuation once, so every tier (exact, prefix, wildcard,
    # definitions) matches the same spelling of the query.
    q = db.normalize_cjk_punct(q)
    pd.mark("handler_start")
    db.start_log()  # capture the SQL this search runs (for data/search.log)
    started = time.perf_counter()
    total = None
    timings: dict[str, float] = {}
    counters: dict[str, object] = {"part": part, "query_length": len(q)}
    # The search-language dropdown picks the primary tier's dictionary.
    lang = (lang or "EN").strip().upper()
    if lang not in DEFAULTS:
        lang = "EN"
    # Only anchors this server knows; local imported dictionaries are ignored.
    # Keep the user's drag order: the fast preview uses the first selected
    # dictionary in the chosen language.
    known_anchors = {item["anchor"]: item["lang"] for item in db._SOURCE_QUERIES}
    selected_dicts = list(
        dict.fromkeys(
            anchor.strip()
            for anchor in (dicts or "").split(",")
            if anchor.strip() in known_anchors
        )
    )

    def mark(name: str, at: float) -> None:
        """Add one debug phase duration without changing the search itself."""
        timings[name] = round(timings.get(name, 0.0) + (time.perf_counter() - at) * 1000, 2)

    try:
        label = (
            None
            if exact or part == "primary"
            else db.label_page(q, offset, limit)
        )
        if exact:
            # The /word/<word> view: the exact entries for that headword, with
            # the same JSON shape as a normal search.
            word = q.strip()
            entries = db.lookup(word) or db.source_entries(word)
            # Follow "see X" / "variant of X" redirects, like a normal search.
            # Without this, a cross-reference such as 没sei了 -> 沒誰了 showed
            # its CC-CEDICT chip but none of the target's definitions.
            entries = _expand_variants([dict(entry) for entry in entries])
            for entry in entries:
                slug = _audio_slug(
                    entry.get("traditional", ""), entry.get("pinyin", "")
                )
                entry["audio"] = _AUDIO_BASE + quote(slug) + ".mp3" if slug else None
                # Let the merged exact row pick the best reading (只 zhǐ, not 隻 zhī).
                entry["reading_score"] = _reading_count(entry) + _preferred_bonus(entry)
            results = entries
            total = len(results)
            fuzzy = []
            pinyin_match = False
            english_match = False
            counters["raw"] = len(results)
            counters["expanded"] = len(results)
            counters["exact"] = True
        elif label is not None:
            page_entries, total = label
            results = db.decorate_entries(page_entries)
            fuzzy = []
            pinyin_match = False
            english_match = False
            counters["raw"] = len(page_entries)
            counters["expanded"] = len(results)
            counters["label_page"] = True
        elif not q.strip().strip("*"):
            # "*": every headword across all sources, 50 per page.
            at = time.perf_counter()
            with pd.stage("headwords"):
                heads, total = db.all_headwords(offset, db.ALL_PAGE_SIZE)
            mark("headwords", at)
            at = time.perf_counter()
            with pd.stage("decorate"):
                results = db.entries_for_headwords(heads)
            mark("hydrate", at)
            counters["raw"] = len(heads)
            counters["expanded"] = len(results)
            fuzzy = []
            pinyin_match = False
            english_match = False
        elif ("*" in q or "?" in q or "＊" in q or "？" in q) and not (
            len(q) >= 2 and q.startswith('"') and q.endswith('"')
        ):
            # A wildcard pattern is paged: the first (and only) response is
            # the requested page, with its total for the pager.
            if _is_chinese_wildcard(q):
                # Same ranking as a normal search: exact matches first, then
                # words that start with a match, then words that contain one
                # (each by frequency). Sorting the whole list before slicing
                # keeps the order identical on every page.
                cache_key = (
                    q,
                    taiwan,
                    tuple(sorted(selected_dicts)),
                    db.ALL_PAGE_SIZE,
                    search_version(),
                    tuple(os.environ.get(name, "") for name in _RESULT_ENV_FLAGS),
                    db.relevance.RELEVANCE_VERSION,
                )
                state, rank_cache_status = _rank_cache_get_or_compute(
                    cache_key,
                    lambda: _wildcard_search_state(q, taiwan, selected_dicts),
                )
                (
                    results,
                    fuzzy,
                    pinyin_match,
                    english_match,
                    cached_counters,
                    cached_timings,
                ) = state
                counters.update(cached_counters)
                timings.update(cached_timings)
                counters["rank_cache"] = rank_cache_status
                # Page whole simplified-headword groups, like a normal search,
                # so a wildcard search cannot split 了/瞭 across two pages.
                if os.environ.get("YABAI_SERVER_ORDER", "1") != "0":
                    results, total = _page_groups_copy(
                        results, offset, db.ALL_PAGE_SIZE
                    )
                else:
                    total = len(results)
                    results = results[offset : offset + db.ALL_PAGE_SIZE]
                # Only the rows on the requested page need their definitions.
                with pd.stage("decorate_page"):
                    results = db.decorate_entries(results)
            else:
                at = time.perf_counter()
                with pd.stage("wildcard"):
                    entries, total, pinyin_match = db.wildcard_search(
                        q, db.ALL_PAGE_SIZE, offset, taiwan=taiwan
                    )
                mark("db", at)
                at = time.perf_counter()
                with pd.stage("expand"):
                    results = _expand_variants(entries)
                mark("expand", at)
                counters["raw"] = len(entries)
                counters["expanded"] = len(results)
                fuzzy = []
                english_match = False
        elif part == "primary":
            at = time.perf_counter()
            # A Chinese query previews the first dictionary for the selected
            # search language (EN -> CEDICT, TW -> MOE, ...). Only words that
            # start with the query are fetched; the full tier adds the rest.
            quoted = len(q) >= 2 and q.startswith('"') and q.endswith('"')
            # Use the first selected dictionary in this language. In custom
            # mode that is the row the user dragged to the top; otherwise it
            # matches the language default.
            group = _LANG_GROUP.get(lang, "")
            anchor = next(
                (
                    item
                    for item in selected_dicts
                    if known_anchors.get(item) == group
                ),
                DEFAULTS[lang][0],
            )
            counters["primary_anchor"] = anchor
            entries = []
            pinyin_match = False
            if db.has_cjk(q) and not quoted:
                with pd.stage("db_search"):
                    entries = db.prefix_entries(q, anchor, limit)
            else:
                # A pinyin query previews the same dictionary: TW -> MOE
                # readings, ZH -> Chinese Wiktionary. CC-CEDICT is both the EN
                # default and the fallback when that dictionary has no reading
                # for the query (English words, for example).
                if anchor != "cedict" and not quoted:
                    with pd.stage("db_search"):
                        entries = db.pinyin_prefix_entries(q, anchor, limit)
                    pinyin_match = bool(entries)
                if not entries:
                    with pd.stage("db_search"):
                        entries, pinyin_match = db.search(
                            q, limit, taiwan, part="primary"
                        )
            mark("db", at)
            at = time.perf_counter()
            with pd.stage("expand"):
                results = _expand_variants(entries)
            mark("expand", at)
            counters["raw"] = len(entries)
            counters["expanded"] = len(results)
            # Rank and page the preview by whole groups too, so its rows are
            # already in their final order and spelling when they appear.
            if selected_dicts:
                results = db.rank_by_category(
                    results,
                    selected_dicts,
                    q,
                    taiwan,
                    definition_search=not pinyin_match,
                )
                counters["selected_dicts"] = ",".join(selected_dicts)
            if os.environ.get("YABAI_SERVER_ORDER", "1") != "0":
                results, _ = db.page_groups(results, 0, limit)
            # D4 limitation: this preview is not yet a prefix of the full list.
            # It previews one dictionary (the language default), so an exact
            # match from another selected dictionary can still sort above a row
            # shown here when the full response arrives. The exact-primary step
            # (query every selected dictionary, keep category 1 tier 1) is not
            # implemented yet.
            fuzzy = []
            if pinyin_match:
                with pd.stage("english_hint"):
                    english_match = db.has_english_match(q.strip())
            else:
                english_match = False
        else:
            # The whole ranked list (official rows plus fuzzy tail) is expensive
            # to rebuild; cache it and let every page slice the same list.
            cache_key = (
                q,
                taiwan,
                tuple(sorted(selected_dicts)),
                limit,
                search_version(),
                tuple(os.environ.get(name, "") for name in _RESULT_ENV_FLAGS),
                db.relevance.RELEVANCE_VERSION,
            )
            state, rank_cache_status = _rank_cache_get_or_compute(
                cache_key,
                lambda: _normal_search_state(q, limit, taiwan, selected_dicts),
            )
            (
                results,
                fuzzy,
                pinyin_match,
                english_match,
                cached_counters,
                cached_timings,
            ) = state
            counters.update(cached_counters)
            timings.update(cached_timings)
            counters["rank_cache"] = rank_cache_status
            # Page whole simplified-headword groups, so one word (e.g. 了 with
            # 瞭) cannot appear on two pages and the total counts merged words.
            # YABAI_SERVER_ORDER=0 restores the old raw-row paging.
            if os.environ.get("YABAI_SERVER_ORDER", "1") != "0":
                results, official_total = _page_groups_copy(results, offset, limit)
                counters["official_total"] = official_total
                counters["fuzzy_total"] = len(fuzzy)
                total = official_total + len(fuzzy)
                # Fuzzy rows start only after every official group. The first
                # pages are entirely official, so this slice is empty there.
                fuzzy_start = max(0, offset - official_total)
                fuzzy_end = max(0, offset + limit - official_total)
                fuzzy = fuzzy[fuzzy_start:fuzzy_end]
            else:
                official_total = len(results)
                counters["official_total"] = official_total
                counters["fuzzy_total"] = len(fuzzy)
                total = official_total + len(fuzzy)
                page_end = offset + limit
                results = results[offset : min(page_end, official_total)]
                fuzzy = fuzzy[
                    max(0, offset - official_total) : max(0, page_end - official_total)
                ]
            # Lazy hydration only ranked the official rows; fetch the
            # definitions for the page we are actually returning.
            with pd.stage("decorate_page"):
                results = db.decorate_entries(results)
    finally:
        statements = db.end_log()
    script = "traditional" if script == "traditional" else "simplified"
    alt_results: list[dict] = []
    alt_same = False
    if (
        (merge or os.environ.get("YABAI_SERVER_MERGE") == "1")
        and not exact
    ):
        # Build both display scripts in one pass. The browser reuses the page
        # when no group splits, and otherwise already has the other layout.
        # The exact-word view keeps its separate rows (it is not paged).
        layout_key = (
            q,
            offset,
            limit,
            lang,
            tuple(sorted(selected_dicts)),
            taiwan,
            part,
            db.data_version(),
        )
        simplified, traditional, alt_same = _page_layouts_cached(
            layout_key, results, taiwan
        )
        if script == "traditional":
            results, alt_results = traditional, simplified
        else:
            results, alt_results = simplified, traditional
        if alt_same:
            alt_results = []
    if total is None:
        # The primary tier never returns more than the 25-result cap.
        results = results[:limit]
        fuzzy = fuzzy[:limit]
    _record_search(q, (time.perf_counter() - started) * 1000, len(results), statements, part)
    counters["total"] = total
    counters["page"] = len(results)
    counters["fuzzy"] = len(fuzzy)
    counters["pinyin_match"] = pinyin_match
    counters["english_match"] = english_match
    total_ms = round((time.perf_counter() - started) * 1000, 2)
    timing_parts = [f"{name};dur={value}" for name, value in timings.items()]
    timing_parts.append(f"total;dur={total_ms}")
    if "rank_cache" in counters:
        timing_parts.append(f"rank_cache;desc={counters['rank_cache']}")
    pd.mark("handler_end")
    response.headers["Server-Timing"] = ", ".join(timing_parts)
    response.headers["X-Yabai-Debug"] = json.dumps(
        counters, separators=(",", ":"), ensure_ascii=True
    )
    return {
        "query": q,
        "pinyin_match": pinyin_match,
        "english_match": english_match,
        "results": results,
        "fuzzy": fuzzy,
        "total": total,
        "offset": offset,
        "page_size": db.ALL_PAGE_SIZE,
        "script": script,
        "alt_same": alt_same,
        "alt_results": alt_results,
        "ms": round((time.perf_counter() - started) * 1000),
    }



def _tok(text: str, trad: str, simp: str, entries: list[dict]) -> dict:
    """One parsed token; drop script fields that equal the text itself.

    The client's scriptText() falls back to token.text, so sending identical
    traditional/simplified strings only inflates the response.
    """
    token: dict = {"text": text, "entries": entries}
    if trad != text:
        token["traditional"] = trad
    if simp != text:
        token["simplified"] = simp
    return token


@lru_cache(maxsize=32)
def _annotate_text(text: str) -> tuple[bytes, dict, dict]:
    """Segment `text` and attach dictionary matches. Cached by exact text.

    Returns (encoded JSON body, ms timings, counters). Timings are filled by
    segment(), OpenCC, the lookup loop and the final json.dumps.
    """
    stats: dict[str, float] = {}
    counters: dict[str, object] = {"chars": len(text)}

    def tick(name: str, at: float) -> None:
        stats[name] = round(stats.get(name, 0.0) + (time.perf_counter() - at) * 1000, 2)

    at = time.perf_counter()
    traditional = db.to_traditional(text)
    tick("opencc_trad", at)
    at = time.perf_counter()
    simplified = db.to_simplified(text)
    tick("opencc_simp", at)

    seg: dict[str, float] = {}
    at = time.perf_counter()
    words = segment.segment(text, seg)
    tick("segment", at)
    for key, value in seg.items():
        if key.startswith("n_"):
            counters[key] = value
        else:
            stats[f"seg_{key}"] = value

    lookup_before = db.lookup_reading.cache_info()
    lookup_memo: dict[str, list[dict]] = {}
    source_memo: dict[str, list[dict]] = {}
    slow: list[tuple[float, str]] = []

    def look(word: str) -> list[dict]:
        if word in lookup_memo:
            return lookup_memo[word]
        at = time.perf_counter()
        result = db.lookup_reading(word) if word.strip() else []
        spent = (time.perf_counter() - at) * 1000
        stats["db_lookup"] = round(stats.get("db_lookup", 0.0) + spent, 2)
        slow.append((spent, f"lookup:{word}"))
        lookup_memo[word] = result
        return result

    def source(word: str) -> list[dict]:
        if word in source_memo:
            return source_memo[word]
        at = time.perf_counter()
        result = (
            [db.slim_entry(entry) for entry in db.source_entries(word)]
            if word.strip()
            else []
        )
        spent = (time.perf_counter() - at) * 1000
        stats["db_source"] = round(stats.get("db_source", 0.0) + spent, 2)
        counters["source_calls"] = int(counters.get("source_calls", 0)) + 1
        slow.append((spent, f"source:{word}"))
        source_memo[word] = result
        return result

    tokens: list[dict] = []
    pos = 0
    at_loop = time.perf_counter()
    for word in words:
        end = pos + len(word)
        trad, simp = traditional[pos:end], simplified[pos:end]
        pos = end
        entries = look(word)
        if not entries and word.strip():
            # Not in CEDICT: use another dictionary's entry (e.g. MOE) so the
            # parsed word still gets a reading and is clickable.
            entries = source(word)
        if not entries and word.strip():
            # A mixed-script spelling such as 什麽 (麽 U+9EBD, not 麼 U+9EBC)
            # is not a headword itself. Retry with the sentence-level OpenCC
            # forms, which normalise the variant without an extra conversion.
            for form in (trad, simp):
                if form and form != word:
                    entries = look(form) or source(form)
                    if entries:
                        break
        if (
            not entries
            and len(word) > 1
            and db.has_cjk(word)
            and not segment.is_headword(word)
        ):
            for i, char in enumerate(word):
                tokens.append(
                    _tok(char, trad[i : i + 1], simp[i : i + 1], look(char))
                )
        else:
            tokens.append(_tok(word, trad, simp, entries))
    tick("token_loop_total", at_loop)

    lookup_after = db.lookup_reading.cache_info()
    counters["tokens"] = len(tokens)
    counters["unique_words"] = len(set(words))
    counters["lookup_cache_hits"] = lookup_after.hits - lookup_before.hits
    counters["lookup_cache_misses"] = lookup_after.misses - lookup_before.misses
    counters["slowest"] = [
        f"{ms:.1f}ms {label}" for ms, label in sorted(slow, reverse=True)[:5]
    ]
    at = time.perf_counter()
    body = json.dumps(
        {"tokens": tokens}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    tick("encode", at)
    counters["body_bytes"] = len(body)
    return body, stats, counters


