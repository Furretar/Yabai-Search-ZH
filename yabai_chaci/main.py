"""FastAPI app for Yabai-Chaci."""

from __future__ import annotations

import collections
import time
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from yabai_chaci import db, segment
from yabai_chaci.default_dictionaries import DEFAULTS
from yabai_chaci.pinyin import to_toneless

# Directory of this file (the yabai_chaci package). Used to locate assets
# regardless of the directory the server is launched from.
BASE = Path(__file__).resolve().parent

# Directory holding the static assets served under /static/.
_STATIC_DIR = BASE / "static"


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
app = FastAPI(title="Yabai-Chaci")

# Serve files from yabai_chaci/static/ at the URL prefix /static/...
# (e.g. /static/style.css, /static/app.js). Read-only, served verbatim.
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

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
        },
    )


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    """About page: sources, licences and search tips."""
    sources = db.sources()
    # Per-source "Updated ..." dates and entry counts.
    updated = {source["anchor"]: source["updated"] for source in sources}
    counts = {source["anchor"]: source["entries"] for source in sources}
    return templates.TemplateResponse(
        request,
        "about.html",
        {
            "asset_version": asset_version(),
            "updated": updated,
            "counts": counts,
            "freq": db.frequency_source(),
        },
    )


def _reading_count(entry: dict) -> int:
    """How many CEDICT definitions this entry's own reading has."""
    for definition in entry.get("definitions", []):
        if definition.get("anchor") == "cedict":
            for group in definition.get("groups", []):
                if group.get("pinyin") == entry.get("pinyin"):
                    return len(group.get("senses", []))
    return 0


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
            score = _reading_count(e)
            if key not in best:
                best[key] = (score, e)
                order.append(key)
            elif score > best[key][0]:
                best[key] = (score, e)
    return [best[key][1] for key in order]


# Debug log: the last 10 searches, with the SQL each one ran (see db.start_log).
_SEARCH_LOG_PATH = BASE.parent / "data" / "search.log"
_recent_searches: "collections.deque[dict]" = collections.deque(maxlen=10)


def _record_search(query: str, duration_ms: float, count: int, statements: list[str]) -> None:
    """Append a search to the rolling log and rewrite the log file."""
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
def api_search(q: str = "", limit: int = Query(50, ge=1, le=200), taiwan: bool = False):
    """JSON search endpoint. `q` is the term; `limit` is clamped to 1..200.

    `taiwan` lets a pinyin search also match Taiwan (MOE) readings.
    `pinyin_match` is true when the query was an exact Chinese reading - in which
    case `results` holds only those readings, no English-definition matches.
    """
    db.start_log()  # capture the SQL this search runs (for data/search.log)
    started = time.perf_counter()
    try:
        results = _expand_variants(db.search(q, limit, taiwan))
        # If a tonal reading matched nothing, offer a tone-insensitive "fuzzy" search.
        fuzzy = []
        if not results:
            variant = db.tonal_variant(q)
            if variant:
                fuzzy = db.search(variant, limit, taiwan)
        key = to_toneless(q)
        pinyin_match = bool(key) and any(
            to_toneless(entry["pinyin"]) == key for entry in results
        )
        # Offer the English search only when it would actually find something.
        english_match = False
        if pinyin_match:
            english_match = bool(db.search(f'"{q.strip()}"', limit=1))
    finally:
        statements = db.end_log()
    _record_search(q, (time.perf_counter() - started) * 1000, len(results), statements)
    return {
        "query": q,
        "pinyin_match": pinyin_match,
        "english_match": english_match,
        "results": results,
        "fuzzy": fuzzy,
    }


class AnnotateRequest(BaseModel):
    text: str = ""


@app.post("/api/annotate")
def api_annotate(request: AnnotateRequest):
    """Segment pasted Chinese text and attach dictionary matches to each word.

    A segmented word that has no dictionary entry is split into single
    characters so something still matches (e.g. 这是 -> 这 + 是).
    """
    tokens = []
    for word in segment.segment(request.text):
        entries = db.lookup(word) if word.strip() else []
        if not entries and len(word) > 1:
            for char in word:
                char_entries = db.lookup(char) if char.strip() else []
                tokens.append({"text": char, "entries": char_entries})
        else:
            tokens.append({"text": word, "entries": entries})
    return {"tokens": tokens}
