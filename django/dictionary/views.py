"""Django views for the dictionary pages and JSON API.

These replace the FastAPI routes that used to live in yabai_search/main.py. The
search logic itself is unchanged and lives in yabai_search/services.py.

Access rules (they replace the old "only this device" IP checks):
  * /reports, /reports/delete, /api/search-log, /__debug/status need a logged-in
    staff user (or, for the debug paths, the X-Debug-Token header).
  * Everything else is public.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import threading
import time
from collections import defaultdict, deque
from functools import wraps

import markdown
from django.contrib.auth.views import redirect_to_login
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
)
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_safe

from yabai_search.overrides import NOTE_LABELS
from yabai_search import db, services
from yabai_search.default_config import DEFAULTS, PAGE_SETTINGS, SETTINGS
from yabai_site.middleware import client_ip

_STATIC_DIR = services.BASE / "static"
_REPORTS_PATH = services.BASE.parent / "data" / "bug_reports.jsonl"
# MOE glyph pictures (data/dictionary-databases/moe-taiwan/moe_glyphs/, unpacked by scripts/download-dictionaries/import_moe.py).
_GLYPH_DIR = db.MOE_DB_PATH.parent / "moe_glyphs"
# MOE's own hex names, e.g. "8ff0._104_0.gif": no path separators, so they
# cannot escape the directory.
_GLYPH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(?:gif|png)$")
# Pasted-text limit for /api/annotate (the old endpoint had none).
MAX_ANNOTATE_CHARS = 100_000


# --- small helpers -------------------------------------------------------------


def _json(payload, status: int = 200) -> HttpResponse:
    """Compact UTF-8 JSON, the same shape FastAPI produced."""
    return HttpResponse(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        status=status,
        content_type="application/json",
    )


def _invalid(name: str, message: str, where: str = "query") -> HttpResponse:
    """A 422 in FastAPI's error shape, which the tests and front end expect."""
    return _json(
        {"detail": [{"loc": [where, name], "msg": message, "type": "value_error"}]},
        status=422,
    )


def _int_param(params, name: str, default: int, low: int, high: int | None = None):
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < low or (high is not None and value > high):
        return None
    return value


_TRUE = {"1", "true", "t", "yes", "y", "on"}
_FALSE = {"0", "false", "f", "no", "n", "off"}


def _bool_param(params, name: str, default: bool = False):
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    raw = raw.strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return None


def _is_staff(user) -> bool:
    return bool(user.is_authenticated and user.is_active and user.is_staff)


def staff_required(view):
    """Anonymous visitors go to the login page; other accounts see a 404."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not _is_staff(request.user):
            raise Http404
        return view(request, *args, **kwargs)

    return wrapper


def _debug_allowed(request) -> bool:
    """Staff session, or the X-Debug-Token header when YABAI_DEBUG_TOKEN is set."""
    token = os.environ.get("YABAI_DEBUG_TOKEN", "")
    if token and hmac.compare_digest(request.headers.get("x-debug-token", ""), token):
        return True
    return _is_staff(request.user)


def _read_json_object(request):
    """Parse a JSON object body, or return None when it is not one."""
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# --- pages ---------------------------------------------------------------------


def _index_page(request, q="", page="", lang=SETTINGS["lang"], word_query="", exact=False):
    """Render index.html, optionally in exact-word mode for /word/<word>."""
    lang = (lang or SETTINGS["lang"]).strip().upper()
    if lang not in DEFAULTS:
        lang = SETTINGS["lang"]
    try:
        page_number = int(page) if page else 1
    except ValueError:
        # app.js treats an invalid ?page= as page 1; mirror that here.
        page_number = 1
    initial_page = page_number <= 1
    initial_primary = None
    # Only the first page embeds the fast tier; exact word pages fetch their
    # entries from the exact API instead.
    if q and initial_page and not exact:
        try:
            initial_primary = services.api_search(
                services.HeaderBag(),
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
    account_name = ""
    if request.user.is_authenticated:
        account_name = request.user.get_username()
        get_token(request)  # make sure the csrftoken cookie exists for fetch() calls
    context = {
        "asset_version": services.asset_version(),
        "static": services.static_paths(),
        "sources": db.sources(),
        "note_labels": sorted(NOTE_LABELS),
        "dictionaries": db.dictionaries(),
        "defaults": DEFAULTS,
        "settings": PAGE_SETTINGS,
        "variables_css": (_STATIC_DIR / "variables.css").read_text(encoding="utf-8"),
        "q": q,
        "lang": lang,
        "initial_page": initial_page,
        "initial_primary": initial_primary,
        "word_query": word_query,
        "exact": exact,
        "data_version": services.search_version(),
        "show_reports": _is_staff(request.user),
        # The debug log button is useful for every client, not just staff.
        "show_debug": True,
        "served_by": services.served_by(),
        "account_name": account_name,
    }
    return render(request, "index.html", context)


@require_safe
def index(request):
    return _index_page(
        request,
        q=request.GET.get("q", ""),
        page=request.GET.get("page", ""),
        lang=request.GET.get("lang", SETTINGS["lang"]),
    )


@require_safe
def about(request):
    """About page: the README rendered on every request."""
    content = markdown.markdown(
        services._README_PATH.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    return render(
        request,
        "about.html",
        {
            "asset_version": services.asset_version(),
            "static": services.static_paths(),
            "variables_css": (_STATIC_DIR / "variables.css").read_text(encoding="utf-8"),
            "content": content,
        },
    )


@require_safe
def word_page(request, word):
    """Open the exact-word view inside the normal search page."""
    entries = db.lookup(word)
    if not entries:
        # Words that only a secondary dictionary has do not live in CEDICT.
        entries = db.source_entries(word)
    if not entries:
        raise Http404
    return _index_page(request, word_query=word, exact=True)


@require_safe
@staff_required
def reports_page(request):
    """Show stored bug reports (staff only)."""
    reports = services._load_reports()
    # The template links each reported word to a search using that reporter's
    # language, script, phonetic, and dictionary choices.
    for report in reports:
        report["search_url"] = services._report_search_url(report)
    return render(
        request,
        "reports.html",
        {
            "asset_version": services.asset_version(),
            "static": services.static_paths(),
            "variables_css": (_STATIC_DIR / "variables.css").read_text(encoding="utf-8"),
            "reports": reports,
            "labels": {
                "incorrect": "Incorrect information",
                "bug": "Bug report",
                "suggestion": "Suggestions/Requests",
            },
        },
    )


@require_POST  # CSRF-protected: the form in reports.html carries the token
@staff_required
def delete_report(request):
    """Delete one stored report (staff only)."""
    received_at = request.GET.get("received_at")
    if received_at is None:
        return _invalid("received_at", "Field required")
    if _REPORTS_PATH.exists():
        try:
            kept = []
            for line in _REPORTS_PATH.read_text(encoding="utf-8").splitlines():
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
            _REPORTS_PATH.write_text(
                "".join(line + "\n" for line in kept), encoding="utf-8"
            )
        except OSError:
            return _json({"detail": "Could not delete the report."}, status=500)
    response = HttpResponse(status=303)
    response["Location"] = "/reports"
    return response


# --- PWA / static-ish routes -------------------------------------------------------


@require_safe
def pwa_manifest(request):
    return FileResponse(
        open(_STATIC_DIR / "manifest.webmanifest", "rb"),
        content_type="application/manifest+json",
    )


@require_safe
def pwa_service_worker(request):
    """Serve the service worker at the root so it can control every page."""
    response = FileResponse(
        open(_STATIC_DIR / "sw.js", "rb"), content_type="text/javascript"
    )
    response["Service-Worker-Allowed"] = "/"
    return response


@require_safe
def moe_glyph(request, name):
    """Serve one MOE glyph picture, or 404 when the name or file is unknown."""
    if not _GLYPH_NAME_RE.match(name):
        raise Http404
    path = _GLYPH_DIR / name
    if not path.is_file():
        raise Http404
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return FileResponse(open(path, "rb"), content_type=content_type)


# --- debug ---------------------------------------------------------------------


@require_safe
def debug_ping(request):
    """A no-database liveness probe for measuring edge/tunnel/queue latency."""
    response = _json({"t": time.time(), "pid": os.getpid()})
    response["Cache-Control"] = "no-store"
    return response


@require_safe
def debug_status(request):
    """The active/fallback state of each derived fast path (staff or token)."""
    if not _debug_allowed(request):
        raise Http404
    status = db.fast_path_status()
    status["page_cache"] = db.page_cache_status()
    status["search_index"] = db.search_index_status()
    return _json(status)


@require_safe
def api_search_log(request):
    """The last few searches with their SQL timings.

    This lists other visitors' search terms, so it is no longer public. The
    front end's "download debug log" button treats a refusal as "no server log".
    """
    if not _debug_allowed(request):
        return _json({"detail": "Forbidden"}, status=403)
    return _json(services.recent_searches())


# --- JSON API ------------------------------------------------------------------


@require_safe
def api_search(request):
    """JSON search endpoint; see services.api_search for the parameters."""
    params = request.GET
    limit = _int_param(params, "limit", 25, 1, 25)
    if limit is None:
        return _invalid("limit", "Input should be between 1 and 25")
    offset = _int_param(params, "offset", 0, 0)
    if offset is None:
        return _invalid("offset", "Input should be greater than or equal to 0")
    flags = {}
    for name in ("taiwan", "exact", "merge"):
        value = _bool_param(params, name)
        if value is None:
            return _invalid(name, "Input should be a valid boolean")
        flags[name] = value
    call = {
        "q": params.get("q", ""),
        "limit": limit,
        "taiwan": flags["taiwan"],
        "offset": offset,
        "part": params.get("part", "all"),
        "lang": params.get("lang", SETTINGS["lang"]),
        "exact": flags["exact"],
        "dicts": params.get("dicts", ""),
        "script": params.get("script", SETTINGS["script"]),
        "merge": flags["merge"],
    }
    # A repeat of the same page is answered from the finished response.
    key = services.response_cache_key(call)
    hit = services.response_cache_get(key)
    if hit is not None:
        response = HttpResponse(hit[0], content_type="application/json")
        for name, value in hit[1].items():
            response[name] = value
        response["X-Response-Cache"] = "hit"
        return response
    headers = services.HeaderBag()
    payload = services.api_search(headers, **call)
    response = _json(payload)
    for name, value in headers.headers.items():
        response[name] = value
    if response.status_code == 200:
        services.response_cache_put(key, response.content, headers.headers)
    return response


@require_safe
def api_convert(request):
    """OpenCC simplified/traditional forms for one word."""
    text = request.GET.get("text", "")
    if len(text) > 128:
        return _invalid("text", "String should have at most 128 characters")
    text = text.strip()
    if not text:
        return _json({"simplified": "", "traditional": ""})
    return _json(
        {"simplified": db.to_simplified(text), "traditional": db.to_traditional(text)}
    )


# The two endpoints below only compute from the posted text. They change nothing
# on the server and use no cookie, so a forged cross-site request gains nothing;
# that is why they are exempt from CSRF checks (a token would also break the
# existing front end). Account endpoints are NOT exempt.
@csrf_exempt
@require_POST
def api_convert_batch(request):
    """Convert several imported-dictionary headwords in one request."""
    body = _read_json_object(request)
    if body is None:
        return _invalid("body", "Input should be a valid dictionary", "body")
    raw = body.get("texts", [])
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        return _invalid("texts", "Input should be a valid list of strings", "body")
    texts = [text.strip()[:128] for text in raw[:200]]
    return _json(
        {
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
    )


@csrf_exempt
@require_POST
def api_annotate(request):
    """Segment pasted Chinese text and attach dictionary matches to each word."""
    started = time.perf_counter()
    body = _read_json_object(request)
    if body is None:
        return _invalid("body", "Input should be a valid dictionary", "body")
    text = body.get("text", "")
    if not isinstance(text, str):
        return _invalid("text", "Input should be a valid string", "body")
    if len(text) > MAX_ANNOTATE_CHARS:
        return _json({"detail": "Text is too long."}, status=413)
    # Normalize before the lru_cache call so the same text with ASCII or
    # full-width punctuation shares one cached result.
    text = db.normalize_cjk_punct(text)
    services.pd.mark("handler_start")
    queue_ms = round((started - getattr(request, "yabai_arrived", started)) * 1000, 2)
    before = services._annotate_text.cache_info()
    with services.pd.stage("annotate"):
        payload, stats, counters = services._annotate_text(text)
    cache_hit = services._annotate_text.cache_info().hits > before.hits
    services.pd.mark("handler_end")

    total_ms = round((time.perf_counter() - started) * 1000, 2)
    parts = [f"queue;dur={queue_ms}"]
    if not cache_hit:  # cached stats describe the original computation
        parts += [f"{name};dur={value}" for name, value in stats.items()]
    parts.append(f"total;dur={total_ms}")
    response = HttpResponse(payload, content_type="application/json")
    response["Server-Timing"] = ", ".join(parts)
    response["X-Yabai-Debug"] = json.dumps(
        {**counters, "cache_hit": cache_hit, "thread": threading.current_thread().name},
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return response


# Bug reports are anonymous writes to a file, so cap how fast one address can
# send them. In-memory and per process: it stops a loop, not a determined flood.
_REPORT_LIMIT = 10
_REPORT_WINDOW = 600  # seconds
_report_hits: dict[str, deque] = defaultdict(deque)
_report_lock = threading.Lock()


def _report_allowed(address: str) -> bool:
    now = time.monotonic()
    with _report_lock:
        hits = _report_hits[address]
        while hits and now - hits[0] > _REPORT_WINDOW:
            hits.popleft()
        if len(hits) >= _REPORT_LIMIT:
            return False
        hits.append(now)
        if len(_report_hits) > 5000:  # keep the table bounded
            for key in [k for k, v in _report_hits.items() if not v][:1000]:
                del _report_hits[key]
        return True


@csrf_exempt  # anonymous by design (no cookie authority); throttled instead
@require_POST
def api_report(request):
    """Append a bug report, including the client's debug log, to a JSONL file."""
    if not _report_allowed(client_ip(request)):
        response = _json({"detail": "Too many reports. Try again later."}, status=429)
        response["Retry-After"] = str(_REPORT_WINDOW)
        return response
    body = _read_json_object(request)
    if body is None:
        return _invalid("body", "Input should be a valid dictionary", "body")
    fields = {
        "kind": "Incorrect information",
        "word": "",
        "message": "",
        "url": "",
        "user_agent": "",
        "log": "",
    }
    for name, default in fields.items():
        value = body.get(name, default)
        if not isinstance(value, str):
            return _invalid(name, "Input should be a valid string", "body")
        fields[name] = value
    settings_value = body.get("settings", {})
    if not isinstance(settings_value, dict):
        return _invalid("settings", "Input should be a valid dictionary", "body")
    record = {
        "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": fields["kind"][:80],
        "word": fields["word"][:200],
        "message": fields["message"][:10000],
        "url": fields["url"][:2000],
        "user_agent": fields["user_agent"][:1000],
        "log": fields["log"][:200000],
        "settings": services._clean_report_settings(settings_value),
    }
    try:
        _REPORTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _REPORTS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return _json({"detail": "Could not save the report."}, status=500)
    return _json({"ok": True})
