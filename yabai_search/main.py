"""FastAPI app for Yabai Search."""

from __future__ import annotations

import collections
import ipaddress
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import markdown
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from yabai_search import db, segment
from yabai_search.default_dictionaries import DEFAULTS
from yabai_search.overrides import PREFERRED_READINGS
from yabai_search.pinyin import to_toneless

# Directory of this file (the yabai_search package). Used to locate assets
# regardless of the directory the server is launched from.
BASE = Path(__file__).resolve().parent

# The About page is rendered from this file on every request, so editing the
# README updates the page without restarting the server.
_README_PATH = BASE.parent / "README.md"

# Directory holding the static assets served under /static/.
_STATIC_DIR = BASE / "static"

# Normal (non-wildcard) searches are paged as well. Fetch up to this many
# matches so the total and the requested page can be computed in one pass.
_MAX_NORMAL_RESULTS = 5000

# Cache of the local device's Tailscale IPs, used to decide whether a report
# request came from this machine.
_local_ips_cache: tuple[float, set[str]] | None = None


def _local_tailscale_ips() -> set[str]:
    """Return this machine's own Tailscale IPs, refreshed once a minute."""
    global _local_ips_cache
    now = time.monotonic()
    if _local_ips_cache and now - _local_ips_cache[0] < 60:
        return _local_ips_cache[1]
    ips: set[str] = set()
    tailscale = shutil.which("tailscale") or "/run/current-system/sw/bin/tailscale"
    try:
        result = subprocess.run(
            [tailscale, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            ips = set(data.get("Self", {}).get("TailscaleIPs", []))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    _local_ips_cache = (now, ips)
    return ips


def _ip_is_local(host: str) -> bool:
    """True for loopback or one of this device's Tailscale addresses."""
    host = (host or "").strip().strip("[]")
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() == "localhost"
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    for candidate in _local_tailscale_ips():
        try:
            if address == ipaddress.ip_address(candidate):
                return True
        except ValueError:
            continue
    return False


def _request_is_local(request: Request) -> bool:
    """Whether the request came from this machine, directly or via Serve."""
    client = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "")
    # Only trust a forwarded address when the immediate peer is local (the
    # Tailscale Serve proxy runs on this machine).
    if forwarded and _ip_is_local(client):
        return _ip_is_local(forwarded.split(",")[0])
    return _ip_is_local(client)


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

# The application object that uvicorn imports and runs.
app = FastAPI(title="Yabai Search")


@app.middleware("http")
async def cache_headers(request, call_next):
    """Set cache headers so static assets reload only when they change.

    HTML pages and the service worker are never cached (the app must re-read
    them, and the theme must apply before paint). Versioned static assets are
    immutable, because asset_version() changes every ?v= URL on any edit.
    """
    response = await call_next(request)
    path = request.url.path
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    elif path == "/sw.js":
        response.headers["Cache-Control"] = "no-cache"
    elif path.startswith("/static/") and response.status_code in (200, 304):
        if "v" in request.query_params or path.startswith("/static/ids_images/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith("/static/vendor/"):
            response.headers["Cache-Control"] = "public, max-age=604800"
        else:
            response.headers["Cache-Control"] = "no-cache"
    elif path.startswith("/moe-glyphs/") and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=604800"
    return response


app.add_middleware(GZipMiddleware, minimum_size=500)


@app.on_event("startup")
def _warm_caches() -> None:
    """Build the SQL and segmenter caches now, not during the first search."""
    db.warm_caches()
    segment.warm()

# Serve files from yabai_search/static/ at the URL prefix /static/...
# (e.g. /static/style.css, /static/app.js). Read-only, served verbatim.
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# Serve the web app manifest at the root so its scope covers the whole site.
@app.get("/manifest.webmanifest", include_in_schema=False)
def pwa_manifest() -> FileResponse:
    """Return the manifest used when installing Yabai Search as an app."""
    return FileResponse(
        _STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


# Serve the service worker at the root so it can control every page. The
# Service-Worker-Allowed header permits the root scope even without this route.
@app.get("/sw.js", include_in_schema=False)
def pwa_service_worker() -> FileResponse:
    """Return the service worker used for installable web-app behaviour."""
    return FileResponse(
        _STATIC_DIR / "sw.js",
        media_type="text/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


# MOE glyph pictures (data/moe_glyphs/, unpacked by scripts/import_moe.py from
# the 字圖 zip). Definitions reference them as "&8ff0._104_0.gif;", which the
# frontend swaps for one of these images so the rare character shows inline.
_GLYPH_DIR = BASE.parent / "data" / "moe_glyphs"
# Names are MOE's own hex codes, e.g. "8ff0._104_0.gif" or "f4ff2_.png" - no
# path separators, so they cannot escape the directory.
_GLYPH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(?:gif|png)$")


@app.get("/moe-glyphs/{name}")
def moe_glyph(name: str):
    """Serve one MOE glyph picture, or 404 when the name or file is unknown."""
    if not _GLYPH_NAME_RE.match(name):
        raise HTTPException(status_code=404)
    path = _GLYPH_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)

# Tell Jinja2 where to find HTML templates.
templates = Jinja2Templates(directory=str(BASE / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Home page: render the (currently blank) index.html template."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "asset_version": asset_version(),
            "sources": db.sources(),
            "dictionaries": db.dictionaries(),
            "defaults": DEFAULTS,
            "show_reports": _request_is_local(request),
            # The debug log button is only for the local admin, not every visitor.
            "show_debug": _request_is_local(request),
        },
    )


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    """About page: the README rendered on every request."""
    content = markdown.markdown(
        _README_PATH.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    return templates.TemplateResponse(
        request,
        "about.html",
        {
            "asset_version": asset_version(),
            "content": content,
        },
    )


# External lookups for the dictionary headers above each definition. These must
# match definitionsBlock() in static/app.js, so the word page links to the same
# places the search results do.
_CC_CEDICT_SEARCH = (
    "https://cc-cedict.org/editor/editor.php?handler=QueryDictionary"
    "&querydictionary_search="
)
_WIKTIONARY_PAGE = "https://en.wiktionary.org/wiki/"
_JPWIKTIONARY_PAGE = "https://ja.wiktionary.org/wiki/"


def _link_definitions(entries: list[dict], word: str) -> None:
    """Give every definition block the href of its dictionary header.

    Mirrors definitionsBlock(): CC-CEDICT and the Wiktionaries open the word at
    the source (a new tab), custom dictionaries have no link, everything else
    links to its section on the About page. `definition["href"]` is None when
    the header should render as a plain label.
    """
    for entry in entries:
        for definition in entry.get("definitions", ()):
            anchor = definition.get("anchor", "")
            headword = definition.get("headword") or word
            if anchor == "cedict":
                definition["href"] = _CC_CEDICT_SEARCH + quote(headword)
                definition["external"] = True
            elif anchor == "wiktionary":
                definition["href"] = _WIKTIONARY_PAGE + quote(headword)
                definition["external"] = True
            elif anchor == "jpwiktionary":
                definition["href"] = _JPWIKTIONARY_PAGE + quote(headword)
                definition["external"] = True
            elif anchor.startswith("custom-"):
                definition["href"] = None
                definition["external"] = False
            else:
                definition["href"] = f"/about#{quote(anchor)}"
                definition["external"] = False


@app.get("/word/{word:path}", response_class=HTMLResponse)
def word_page(request: Request, word: str):
    """A standalone page for one word, with no search controls."""
    entries = db.lookup(word)
    if not entries:
        # Words that only a secondary dictionary has do not live in CEDICT.
        entries = db.source_entries(word)
    if not entries:
        raise HTTPException(status_code=404)
    _link_definitions(entries, word)
    return templates.TemplateResponse(
        request,
        "word.html",
        {
            "asset_version": asset_version(),
            "word": word,
            "entries": entries,
        },
    )


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    """Show locally stored bug reports. Only this device may open this page."""
    if not _request_is_local(request):
        raise HTTPException(status_code=404)
    reports = _load_reports()
    # The template links each reported word to a search using that reporter's
    # language, script, phonetic, and dictionary choices.
    for report in reports:
        report["search_url"] = _report_search_url(report)
    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "asset_version": asset_version(),
            "reports": reports,
            "labels": {
                "incorrect": "Incorrect information",
                "bug": "Bug report",
                "suggestion": "Suggestions/Requests",
            },
        },
    )


@app.post("/reports/delete", response_class=RedirectResponse, status_code=303)
def delete_report(request: Request, received_at: str = Query(...)):
    """Delete one locally stored report. Only this device may do it."""
    if not _request_is_local(request):
        raise HTTPException(status_code=404)
    path = BASE.parent / "data" / "bug_reports.jsonl"
    if path.exists():
        try:
            kept = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(line)
                    continue
                if record.get("received_at") == received_at:
                    continue
                kept.append(line)
            path.write_text(
                "".join(line + "\n" for line in kept), encoding="utf-8"
            )
        except OSError as error:
            raise HTTPException(
                status_code=500, detail="Could not delete the report."
            ) from error
    return RedirectResponse(url="/reports", status_code=303)

def _reading_count(entry: dict) -> int:
    """How many CEDICT definitions this entry's own reading has.

    A lazy-hydration row (see db.search's `hydrate`) already carries the count
    as `_reading_count`; rows from the normal, fully decorated path do not, and
    the count is read from their definitions instead.
    """
    if "_reading_count" in entry:
        return entry["_reading_count"]
    for definition in entry.get("definitions", []):
        if definition.get("anchor") == "cedict":
            for group in definition.get("groups", []):
                if group.get("pinyin") == entry.get("pinyin"):
                    return len(group.get("senses", []))
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
    if preferred and to_toneless(entry.get("pinyin", "")) == to_toneless(preferred):
        return 1_000_000
    return 0


def _has_explicit_tones(query: str) -> bool:
    """True when the query spells out tones (digits or Bopomofo marks)."""
    return any(char.isdigit() for char in query) or any(
        0x3100 <= ord(char) <= 0x312F for char in query
    )


def _expand_variants(results: list[dict]) -> list[dict]:
    """Collapse results to one entry per headword.

    "see X" / "variant of X" entries become X's own entries (other variants are
    shown under X's "Other forms"). When a headword has several readings, keep the
    one with the most definitions, so e.g. 落 shows the standard `luò` (12 senses)
    rather than the rare `lào` (1 sense).
    """
    best: dict[tuple, tuple[int, dict]] = {}
    order: list[tuple] = []
    for entry in results:
        if entry.get("main"):
            shown = [e for e in db.lookup(entry["main"]) if not e.get("main")]
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
    return [best[key][1] for key in order]


# Debug log: the last 10 searches, with each SQL statement's timing (see
# db.start_log). The per-request instrumentation is on by default.
_SEARCH_LOG_PATH = BASE.parent / "data" / "search.log"
_recent_searches: "collections.deque[dict]" = collections.deque(maxlen=10)


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
    _recent_searches.append(
        {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
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
                    f"[{entry['at']}] {entry['query']!r} -> "
                    f"{entry['results']} results in {entry['ms']} ms\n"
                )
                for statement in entry["sql"]:
                    handle.write(f"    {statement.strip()}\n")
                handle.write("\n")
    except OSError:
        pass  # logging must never break a search


@app.get("/api/search")
def api_search(
    response: Response,
    q: str = "",
    limit: int = Query(25, ge=1, le=25),
    taiwan: bool = False,
    offset: int = Query(0, ge=0),
    part: str = Query("all"),
):
    """JSON search endpoint. `q` is the term; `limit` is clamped to 1..25.

    `taiwan` lets a pinyin search also match Taiwan (MOE) readings.
    `pinyin_match` is true when the query was an exact Chinese reading - in which
    case `results` holds only those readings, no English-definition matches.
    A "*" query lists every headword from every source, `ALL_PAGE_SIZE` per page;
    `offset` selects the page and the response's `total` is the full count. Every
    other search returns at most 25 rows per page; tone-insensitive "fuzzy"
    matches are placed after all official results and paginated with them.

    `part="primary"` returns only the fast CEDICT tier, so the page can render
    results progressively; the plain request returns the complete final list.
    """
    db.start_log()  # capture the SQL this search runs (for data/search.log)
    started = time.perf_counter()
    total = None
    timings: dict[str, float] = {}
    counters: dict[str, object] = {"part": part, "query_length": len(q)}

    def mark(name: str, at: float) -> None:
        """Add one debug phase duration without changing the search itself."""
        timings[name] = round(timings.get(name, 0.0) + (time.perf_counter() - at) * 1000, 2)

    try:
        if not q.strip().strip("*"):
            # "*": every headword across all sources, 50 per page.
            at = time.perf_counter()
            heads, total = db.all_headwords(offset, db.ALL_PAGE_SIZE)
            mark("headwords", at)
            at = time.perf_counter()
            results = db.entries_for_headwords(heads)
            mark("hydrate", at)
            counters["raw"] = len(heads)
            counters["expanded"] = len(results)
            fuzzy = []
            pinyin_match = False
            english_match = False
        elif ("*" in q or "?" in q) and not (
            len(q) >= 2 and q.startswith('"') and q.endswith('"')
        ):
            # A wildcard pattern is paged: the first (and only) response is
            # the requested page, with its total for the pager.
            at = time.perf_counter()
            entries, total, pinyin_match = db.wildcard_search(
                q, db.ALL_PAGE_SIZE, offset
            )
            mark("db", at)
            at = time.perf_counter()
            results = _expand_variants(entries)
            mark("expand", at)
            counters["raw"] = len(entries)
            counters["expanded"] = len(results)
            fuzzy = []
            english_match = False
        elif part == "primary":
            at = time.perf_counter()
            entries, pinyin_match = db.search(q, limit, taiwan, part="primary")
            mark("db", at)
            at = time.perf_counter()
            results = _expand_variants(entries)
            mark("expand", at)
            counters["raw"] = len(entries)
            counters["expanded"] = len(results)
            fuzzy = []
            english_match = db.has_english_match(q.strip()) if pinyin_match else False
        else:
            at = time.perf_counter()
            # Lazy hydration: rank every match (so the total stays exact) but
            # only fetch definitions for the rows that reach the page.
            entries, pinyin_match = db.search(
                q, _MAX_NORMAL_RESULTS, taiwan, hydrate=0
            )
            mark("db", at)
            at = time.perf_counter()
            results = _expand_variants(entries)
            mark("expand", at)
            counters["raw"] = len(entries)
            counters["expanded"] = len(results)
            fuzzy = []
            variant = db.tonal_variant(q)
            if variant and not results:
                # If a tonal reading matched nothing, offer a tone-insensitive
                # "fuzzy" search.
                at = time.perf_counter()
                fuzzy, _ = db.search(variant, limit, taiwan)
                mark("fuzzy_db", at)
            elif variant and _has_explicit_tones(q):
                # A tonal search that did find results still shows the
                # tone-insensitive matches under "Fuzzy match".
                seen = {
                    (entry.get("traditional"), entry.get("simplified"))
                    for entry in results
                }
                at = time.perf_counter()
                variant_results, _ = db.search(variant, limit, taiwan)
                mark("fuzzy_db", at)
                at = time.perf_counter()
                fuzzy = [
                    entry
                    for entry in _expand_variants(variant_results)
                    if (entry.get("traditional"), entry.get("simplified")) not in seen
                ]
                mark("fuzzy_expand", at)
            # Definition-only matches are hidden when the query also matched a
            # headword or reading. Number queries try every numeral spelling,
            # so 21 also keeps 二十一世纪 and 廿一 headwords.
            if not pinyin_match:
                at = time.perf_counter()
                forms = db.headword_query_forms(q)
                if forms:
                    headword_matches = [
                        entry
                        for entry in results
                        if any(
                            form in word
                            for form in forms
                            for word in (
                                entry.get("simplified", ""),
                                entry.get("traditional", ""),
                            )
                            if word
                        )
                    ]
                    if headword_matches:
                        results = headword_matches
                mark("filter", at)
                counters["filtered"] = len(results)
            # Variety labels live on source readings, not on headwords or
            # glosses. Only search them when the query has no reading match:
            # an unquoted query with a reading never falls through to
            # definition-like searches.
            at = time.perf_counter()
            dialect = [] if pinyin_match else db.dialect_entries(q)
            mark("dialect", at)
            counters["dialect"] = len(dialect)
            if dialect:
                at = time.perf_counter()
                expanded = _expand_variants(dialect)
                # A place/variety label search (e.g. "Chengdu") is about the
                # labelled words, so show them first. For a language name the
                # language entry itself is already first, so append.
                if db._is_language_query(q):
                    results = db._merge_unique(results, expanded)
                else:
                    results = db._merge_unique(expanded, results)
                mark("dialect_merge", at)
            # Offer the English search only when it would actually find something.
            english_match = False
            if pinyin_match:
                english_match = db.has_english_match(q.strip())
            # Fuzzy matches are the tail of the combined list: they come after
            # every official result, so they only appear on the last pages.
            # The pager total covers both lists, and the requested page is a
            # slice of that combined list.
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
            results = db.decorate_entries(results)
    finally:
        statements = db.end_log()
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
        "ms": round((time.perf_counter() - started) * 1000),
    }


class AnnotateRequest(BaseModel):
    text: str = ""


class ConvertBatchRequest(BaseModel):
    texts: list[str] = []


class BugReportRequest(BaseModel):
    kind: str = "Incorrect information"
    word: str = ""
    message: str = ""
    url: str = ""
    user_agent: str = ""
    log: str = ""
    settings: dict = Field(default_factory=dict)


@app.get("/api/convert")
def api_convert(text: str = Query("", max_length=128)):
    """Return OpenCC simplified/traditional forms for a word.

    Used by the browser-side imported dictionaries, which only store one script
    form, so their headwords can follow the front-end script toggle.
    """
    text = text.strip()
    if not text:
        return {"simplified": "", "traditional": ""}
    return {
        "simplified": db.to_simplified(text),
        "traditional": db.to_traditional(text),
    }


@app.post("/api/convert-batch")
def api_convert_batch(request: ConvertBatchRequest):
    """Convert several imported-dictionary headwords in one request.

    A custom dictionary search can surface dozens of headwords at once; doing
    one /api/convert round-trip per headword made those searches wait on many
    small requests. Keep the batch bounded so a stray client cannot send an
    unbounded body.
    """
    texts = [text.strip()[:128] for text in request.texts[:200]]
    return {
        "results": [
            {
                "simplified": db.to_simplified(text),
                "traditional": db.to_traditional(text),
            }
            if text
            else {"simplified": "", "traditional": ""}
            for text in texts
        ]
    }



@app.post("/api/report")
def api_report(report: BugReportRequest):
    """Append a bug report, including the client's debug log, to a JSONL file.

    Reports are local data, not part of the dictionary. They are written under
    data/ so a re-import of CEDICT never touches them.
    """
    record = {
        "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": report.kind[:80],
        "word": report.word[:200],
        "message": report.message[:10000],
        "url": report.url[:2000],
        "user_agent": report.user_agent[:1000],
        "log": report.log[:200000],
        "settings": _clean_report_settings(report.settings),
    }
    path = BASE.parent / "data" / "bug_reports.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as error:
        raise HTTPException(
            status_code=500, detail="Could not save the report."
        ) from error
    return {"ok": True}


@app.post("/api/annotate")
def api_annotate(request: AnnotateRequest):
    """Segment pasted Chinese text and attach dictionary matches to each word.

    The whole sentence is converted to traditional and simplified up front, so
    each word carries both forms for the front-end script toggle. Converting the
    full sentence (not each word) lets OpenCC use the phrase context, e.g.
    "一只" -> "一隻". A segmented word with no dictionary entry is split into
    single characters so something still matches (e.g. 这是 -> 这 + 是).
    """
    text = request.text
    traditional = db.to_traditional(text)
    simplified = db.to_simplified(text)
    tokens = []
    pos = 0
    for word in segment.segment(text):
        end = pos + len(word)
        trad, simp = traditional[pos:end], simplified[pos:end]
        pos = end
        entries = db.lookup(word) if word.strip() else []
        if not entries and word.strip():
            # Not in CEDICT: use another dictionary's entry (e.g. MOE) so the
            # parsed word still gets a reading and is clickable.
            entries = db.source_entries(word)
        if (
            not entries
            and len(word) > 1
            and db.has_cjk(word)
            and not segment.is_headword(word)
        ):
            for i, char in enumerate(word):
                char_entries = db.lookup(char) if char.strip() else []
                tokens.append(
                    {
                        "text": char,
                        "traditional": trad[i : i + 1],
                        "simplified": simp[i : i + 1],
                        "entries": char_entries,
                    }
                )
        else:
            tokens.append(
                {"text": word, "traditional": trad, "simplified": simp, "entries": entries}
            )
    return {"tokens": tokens}
