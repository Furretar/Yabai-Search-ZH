"""FastAPI app for Yabai Search."""

from __future__ import annotations

import asyncio
import collections
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode

import markdown
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from yabai_search import db, segment
from yabai_search import perf_debug as pd
from yabai_search.default_dictionaries import DEFAULTS
from yabai_search.overrides import PREFERRED_READINGS
from yabai_search.pinyin import to_numeric, to_toneless

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
# The full request ranks every match so `total` and later pages are exact.
# 1e9 is a sentinel larger than any possible result set, not a user-facing cap.
# YABAI_UNCAPPED=0 restores the old 5,000-row sample for A/B comparison only.
_MAX_NORMAL_RESULTS = (
    5000 if os.environ.get("YABAI_UNCAPPED") == "0" else db.UNBOUNDED
)

# Process start time, folded into the API cache version: a restart invalidates
# cached search responses even when the data files did not change (for example
# after editing overrides.py or ranking code).
_START_TS = int(time.time())


def search_version() -> str:
    """Cache version for /api/search: data fingerprint plus process start time."""
    return f"{db.data_version()}-{_START_TS:x}"

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

# Counts requests that have arrived but not finished. A high value explains a
# slow first byte (the threadpool was busy) without guessing.
_inflight = 0
_inflight_lock = threading.Lock()
# Optional load shedder: when this many search requests are already running,
# answer new ones with 503 + Retry-After instead of queueing them. Off by
# default (0), so the protected path is unchanged.
_ADMISSION_LIMIT = int(os.environ.get("YABAI_ADMISSION", "0") or 0)


@app.middleware("http")
async def _debug_stamp(request, call_next):
    """Record arrival time (to measure threadpool wait) and in-flight count.

    When YABAI_TRACE=1, also start a perf_debug trace, add its Server-Timing
    and X-Debug-ID headers, and append the request record to the trace file.
    With tracing off this is the original lightweight middleware.
    """
    global _inflight
    request.state.arrived = time.perf_counter()
    # Lag: client send time to this middleware receiving the request. The
    # frontend sends X-Client-Sent (epoch milliseconds) with each API request.
    client_sent = request.headers.get("x-client-sent")
    lag_ms = None
    if client_sent:
        try:
            lag_ms = max(0, round(time.time() * 1000 - float(client_sent)))
        except ValueError:
            pass
    trace = None
    debug_id = None
    if pd.ENABLED:
        debug_id = request.headers.get("cf-ray") or uuid.uuid4().hex[:12]
        trace = pd.start(debug_id, str(request.url.query))
        if client_sent and trace is not None:
            try:
                trace.client_sent_ms = float(client_sent)
            except ValueError:
                pass
        pd.mark("arrived")
    if (
        _ADMISSION_LIMIT > 0
        and request.url.path == "/api/search"
        and _inflight >= _ADMISSION_LIMIT
    ):
        headers = {"Retry-After": "1"}
        if lag_ms is not None:
            headers["X-Yabai-Lag"] = str(lag_ms)
        return JSONResponse(
            status_code=503,
            content={"detail": "busy", "retry_after": 1},
            headers=headers,
        )
    with _inflight_lock:
        _inflight += 1
        seen = _inflight
    try:
        response = await call_next(request)
    except BaseException:
        if trace is not None:
            pd.finish(
                trace,
                {
                    "path": request.url.path,
                    "status": 500,
                    "error": "unhandled",
                },
            )
        raise
    finally:
        with _inflight_lock:
            _inflight -= 1
    response.headers["X-Yabai-Inflight"] = str(seen)
    if lag_ms is not None:
        response.headers["X-Yabai-Lag"] = str(lag_ms)
    if trace is not None:
        timing = pd.server_timing(trace)
        if timing:
            old = response.headers.get("server-timing", "")
            response.headers["Server-Timing"] = ", ".join(
                value for value in (old, timing) if value
            )
        if debug_id:
            response.headers["X-Debug-ID"] = debug_id
        pd.finish(
            trace,
            {
                "path": request.url.path,
                "status": response.status_code,
                "inflight_seen": seen,
            },
        )
    return response


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
    elif (
        path == "/api/search"
        and response.status_code == 200
        and "dv" in request.query_params
    ):
        # Results are immutable for a given data version + query + page, so the
        # browser (and an edge cache) may reuse them. Requests without `dv`
        # stay uncached and errors above never reach this branch.
        response.headers["Cache-Control"] = (
            "public, max-age=86400, stale-while-revalidate=604800"
        )
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
    # Starlette runs sync endpoints in AnyIO's thread pool. The default is 40;
    # on a small host a lower cap keeps memory/queueing predictable. The default
    # preserves the previous behaviour.
    thread_limit = os.environ.get("YABAI_THREADS", "").strip()
    if thread_limit:
        try:
            limit = int(thread_limit)
        except ValueError:
            limit = 0
        if limit > 0:
            import anyio.to_thread

            anyio.to_thread.current_default_thread_limiter().total_tokens = limit
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


@app.on_event("startup")
async def _start_debug_trace() -> None:
    """Start the optional event-loop sampler and write the startup snapshot."""
    if not pd.ENABLED:
        return
    app.state.trace_task = asyncio.create_task(pd.loop_lag_sampler())
    pragmas = {}
    attached = {}
    try:
        connection = db.connect()
        for name in ("cache_size", "mmap_size", "journal_mode", "temp_store"):
            pragmas[name] = connection.execute(f"PRAGMA {name}").fetchone()[0]
        for row in connection.execute("PRAGMA database_list"):
            attached[row[1]] = str(row[2])
    except sqlite3.Error as exc:
        pragmas["error"] = str(exc)
    info = {
        "sqlite_version": sqlite3.sqlite_version,
        "python_version": os.sys.version.split()[0],
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "db_path": str(db.DB_PATH),
        "db_bytes": db.DB_PATH.stat().st_size if db.DB_PATH.exists() else 0,
        "moe_bytes": db.MOE_DB_PATH.stat().st_size if db.MOE_DB_PATH.exists() else 0,
        "freq_path": str(db.FREQ_DB_PATH),
        "freq_bytes": db.FREQ_DB_PATH.stat().st_size if db.FREQ_DB_PATH.exists() else 0,
        "source_gloss_path": str(db.SOURCE_GLOSS_DB_PATH),
        "source_gloss_bytes": (
            db.SOURCE_GLOSS_DB_PATH.stat().st_size
            if db.SOURCE_GLOSS_DB_PATH.exists()
            else 0
        ),
        "head_grams_path": str(db.HEAD_GRAMS_DB_PATH),
        "head_grams_bytes": (
            db.HEAD_GRAMS_DB_PATH.stat().st_size
            if db.HEAD_GRAMS_DB_PATH.exists()
            else 0
        ),
        "pragmas": pragmas,
        "attached": attached,
        "threads": threading.active_count(),
        "cpu_count": os.cpu_count(),
        "web_concurrency": os.environ.get("WEB_CONCURRENCY"),
        "uvicorn_workers": os.environ.get("UVICORN_WORKERS"),
        "trace_file": pd.TRACE_FILE,
        "plan_ms": pd.SLOW_PLAN_MS,
        "always_plan": pd.ALWAYS_PLAN,
        "connection_model": "one read-only connection per worker thread",
    }
    pd.startup(info)

# Serve files from yabai_search/static/ at the URL prefix /static/...
# (e.g. /static/style.css, /static/app.js). Read-only, served verbatim.
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.get("/__ping", include_in_schema=False)
def debug_ping(response: Response) -> dict:
    """A no-database liveness probe for measuring edge/tunnel/queue latency."""
    response.headers["Cache-Control"] = "no-store"
    return {"t": time.time(), "pid": os.getpid()}


@app.get("/__debug/status", include_in_schema=False)
def debug_status(request: Request) -> dict:
    """Return the active/fallback state of each derived fast path."""
    token = os.environ.get("YABAI_DEBUG_TOKEN")
    if token:
        if request.headers.get("x-debug-token") != token:
            raise HTTPException(status_code=404)
    elif not _ip_is_local(request.client.host if request.client else ""):
        raise HTTPException(status_code=404)
    # Keep the established flat fast-path keys; page_cache is additive.
    status = db.fast_path_status()
    status["page_cache"] = db.page_cache_status()
    return status


@app.get("/__debug/lag", include_in_schema=False)
def debug_lag(request: Request) -> dict:
    """Return recent event-loop lag samples; local or token protected."""
    if not pd.ENABLED:
        raise HTTPException(status_code=404)
    token = os.environ.get("YABAI_DEBUG_TOKEN")
    if token:
        if request.headers.get("x-debug-token") != token:
            raise HTTPException(status_code=404)
    elif not _ip_is_local(request.client.host if request.client else ""):
        raise HTTPException(status_code=404)
    return {
        "now": time.time(),
        "lag": list(pd.LOOP_LAG),
        "trace_file": pd.TRACE_FILE,
    }


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


def _index_page(
    request: Request,
    q: str = "",
    page: str = "",
    lang: str = "EN",
    word_query: str = "",
    exact: bool = False,
):
    """Render index.html, optionally in exact-word mode for /word/<word>."""
    lang = (lang or "EN").strip().upper()
    if lang not in DEFAULTS:
        lang = "EN"
    try:
        page_number = int(page) if page else 1
    except ValueError:
        # app.js treats an invalid ?page= as page 1; mirror that here.
        page_number = 1
    initial_page = page_number <= 1
    initial_primary = None
    # Only the first page embeds the fast tier: later pages ask for an offset,
    # so the embedded payload would be unused and only add HTML weight. Exact
    # word pages fetch their entries from the exact API instead.
    if q and initial_page and not exact:
        try:
            # FastAPI's Query() defaults are not resolved when the route is
            # called directly, so pass every parameter explicitly.
            from fastapi.responses import Response as _Resp

            initial_primary = api_search(
                _Resp(),
                q=q,
                limit=25,
                taiwan=False,
                offset=0,
                part="primary",
                lang=lang,
            )
        except Exception:
            # The page still works without the embedded tier: app.js fetches it.
            initial_primary = None
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "asset_version": asset_version(),
            "sources": db.sources(),
            "dictionaries": db.dictionaries(),
            "defaults": DEFAULTS,
            "variables_css": (_STATIC_DIR / "variables.css").read_text(encoding="utf-8"),
            "q": q,
            "lang": lang,
            "initial_page": initial_page,
            "initial_primary": initial_primary,
            "word_query": word_query,
            "exact": exact,
            "data_version": search_version(),
            "show_reports": _request_is_local(request),
            # The debug log button is useful for every client, not just local.
            "show_debug": True,
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", page: str = "", lang: str = "EN"):
    """Home page: render the index.html template."""
    return _index_page(request, q=q, page=page, lang=lang)


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
            "variables_css": (_STATIC_DIR / "variables.css").read_text(encoding="utf-8"),
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


@app.get("/word/{word:path}", response_class=HTMLResponse)
def word_page(request: Request, word: str):
    """Open the exact-word view inside the normal search page."""
    entries = db.lookup(word)
    if not entries:
        # Words that only a secondary dictionary has do not live in CEDICT.
        entries = db.source_entries(word)
    if not entries:
        raise HTTPException(status_code=404)
    return _index_page(request, word_query=word, exact=True)


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
            "variables_css": (_STATIC_DIR / "variables.css").read_text(encoding="utf-8"),
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
    key = db.reading_key(entry.get("pinyin"))
    for definition in entry.get("definitions", []):
        if definition.get("anchor") == "cedict":
            for group in definition.get("groups", []):
                if db.reading_key(group.get("pinyin")) == key:
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
                looked = [e for e in db.lookup(target) if not e.get("main")]
                on_target = [e for e in looked if e["traditional"] == target] or looked
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


# Debug log: the last 10 searches, with each SQL statement's timing (see
# db.start_log). The per-request instrumentation is on by default.
_SEARCH_LOG_PATH = BASE.parent / "data" / "search.log"
_recent_searches: "collections.deque[dict]" = collections.deque(maxlen=10)
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
    lang: str = "EN",
    exact: bool = False,
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
            entries = [dict(entry) for entry in entries]
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
            at = time.perf_counter()
            with pd.stage("wildcard"):
                entries, total, pinyin_match = db.wildcard_search(
                    q, db.ALL_PAGE_SIZE, offset
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
            if db.has_cjk(q) and not quoted:
                anchor = DEFAULTS[lang][0]
                counters["primary_anchor"] = anchor
                with pd.stage("db_search"):
                    entries = db.prefix_entries(q, anchor, limit)
                pinyin_match = False
            else:
                with pd.stage("db_search"):
                    entries, pinyin_match = db.search(q, limit, taiwan, part="primary")
            mark("db", at)
            at = time.perf_counter()
            with pd.stage("expand"):
                results = _expand_variants(entries)
            mark("expand", at)
            counters["raw"] = len(entries)
            counters["expanded"] = len(results)
            fuzzy = []
            if pinyin_match:
                with pd.stage("english_hint"):
                    english_match = db.has_english_match(q.strip())
            else:
                english_match = False
        else:
            at = time.perf_counter()
            # Lazy hydration: rank every match (so the total stays exact) but
            # only fetch definitions for the rows that reach the page.
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
            fuzzy = []
            variant = db.tonal_variant(q)
            if variant and not results:
                # If a tonal reading matched nothing, offer a tone-insensitive
                # "fuzzy" search.
                at = time.perf_counter()
                with pd.stage("fuzzy_db"):
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
                with pd.stage("english_hint"):
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
            with pd.stage("decorate_page"):
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


@app.post("/api/annotate")
def api_annotate(payload: AnnotateRequest, request: Request):
    """Segment pasted Chinese text and attach dictionary matches to each word.

    The whole sentence is converted to traditional and simplified up front, so
    each word carries both forms for the front-end script toggle. Converting the
    full sentence (not each word) lets OpenCC use the phrase context, e.g.
    "一只" -> "一隻". A segmented word with no dictionary entry is split into
    single characters so something still matches (e.g. 这是 -> 这 + 是).
    """
    started = time.perf_counter()
    pd.mark("handler_start")
    queue_ms = round((started - getattr(request.state, "arrived", started)) * 1000, 2)
    before = _annotate_text.cache_info()
    with pd.stage("annotate"):
        body, stats, counters = _annotate_text(payload.text)
    cache_hit = _annotate_text.cache_info().hits > before.hits
    pd.mark("handler_end")

    total_ms = round((time.perf_counter() - started) * 1000, 2)
    parts = [f"queue;dur={queue_ms}"]
    if not cache_hit:  # cached stats describe the original computation
        parts += [f"{name};dur={value}" for name, value in stats.items()]
    parts.append(f"total;dur={total_ms}")
    headers = {
        "Server-Timing": ", ".join(parts),
        "X-Yabai-Debug": json.dumps(
            {**counters, "cache_hit": cache_hit, "thread": threading.current_thread().name},
            separators=(",", ":"),
            ensure_ascii=True,
        ),
    }
    return Response(content=body, media_type="application/json", headers=headers)
