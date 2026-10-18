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
from yabai_chaci.overrides import PREFERRED_READINGS
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


@app.on_event("startup")
def _warm_caches() -> None:
    """Build the SQL and segmenter caches now, not during the first search."""
    db.warm_caches()
    segment.warm()

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
    q: str = "",
    limit: int = Query(50, ge=1, le=50),
    taiwan: bool = False,
    offset: int = Query(0, ge=0),
    part: str = Query("all"),
):
    """JSON search endpoint. `q` is the term; `limit` is clamped to 1..50.

    `taiwan` lets a pinyin search also match Taiwan (MOE) readings.
    `pinyin_match` is true when the query was an exact Chinese reading - in which
    case `results` holds only those readings, no English-definition matches.
    A "*" query lists every headword from every source, `ALL_PAGE_SIZE` per page;
    `offset` selects the page and the response's `total` is the full count. Every
    other search is capped at 50 results.

    `part="primary"` returns only the fast CEDICT tier, so the page can render
    results progressively; the plain request returns the complete final list.
    """
    db.start_log()  # capture the SQL this search runs (for data/search.log)
    started = time.perf_counter()
    total = None
    try:
        if not q.strip().strip("*"):
            # "*": every headword across all sources, 50 per page.
            heads, total = db.all_headwords(offset, db.ALL_PAGE_SIZE)
            results = db.entries_for_headwords(heads)
            fuzzy = []
            pinyin_match = False
            english_match = False
        elif "*" in q and not (len(q) >= 2 and q.startswith('"') and q.endswith('"')):
            # A wildcard pattern is paged: the first (and only) response is
            # the requested page, with its total for the pager.
            entries, total, pinyin_match = db.wildcard_search(
                q, db.ALL_PAGE_SIZE, offset
            )
            results = _expand_variants(entries)
            fuzzy = []
            english_match = False
        elif part == "primary":
            entries, pinyin_match = db.search(q, limit, taiwan, part="primary")
            results = _expand_variants(entries)
            fuzzy = []
            english_match = db.has_english_match(q.strip()) if pinyin_match else False
        else:
            entries, pinyin_match = db.search(q, limit, taiwan)
            results = _expand_variants(entries)
            fuzzy = []
            variant = db.tonal_variant(q)
            if variant and not results:
                # If a tonal reading matched nothing, offer a tone-insensitive
                # "fuzzy" search.
                fuzzy, _ = db.search(variant, limit, taiwan)
            elif variant and _has_explicit_tones(q):
                # A tonal search that did find results still shows the
                # tone-insensitive matches under "Fuzzy match".
                seen = {
                    (entry.get("traditional"), entry.get("simplified"))
                    for entry in results
                }
                variant_results, _ = db.search(variant, limit, taiwan)
                fuzzy = [
                    entry
                    for entry in _expand_variants(variant_results)
                    if (entry.get("traditional"), entry.get("simplified")) not in seen
                ]
            elif db.has_cjk(q):
                # Definition-only matches are hidden when the query also matched
                # headwords; only show the headword matches.
                literal = db.to_simplified(q.replace("*", "")).strip()
                if literal:
                    headword_matches = [
                        entry
                        for entry in results
                        if any(
                            literal in word
                            for word in (
                                entry.get("simplified", ""),
                                entry.get("traditional", ""),
                            )
                            if word
                        )
                    ]
                    if headword_matches:
                        results = headword_matches
            # Offer the English search only when it would actually find something.
            english_match = False
            if pinyin_match:
                english_match = db.has_english_match(q.strip())
    finally:
        statements = db.end_log()
    if total is None:
        # Normal searches never return more than the 50-result cap.
        results = results[:limit]
        fuzzy = fuzzy[:limit]
    _record_search(q, (time.perf_counter() - started) * 1000, len(results), statements, part)
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
