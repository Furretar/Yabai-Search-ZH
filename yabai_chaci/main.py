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
    app.js changes the URL the browser asks for and it fetches the fresh file —
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
    # anchor -> updated date, for the per-source "Updated ..." lines.
    updated = {source["anchor"]: source["updated"] for source in sources}
    return templates.TemplateResponse(
        request,
        "about.html",
        {"asset_version": asset_version(), "updated": updated},
    )


def _build_matches(results: list[dict], query: str) -> list[dict]:
    """Every dictionary's entry for each result headword (and a Chinese query).

    Backs the "Matches in other dictionaries" section: the same words the search
    found, but shown from the dictionaries that aren't currently selected.
    """
    matches: list[dict] = []
    seen: set[tuple] = set()

    def add(anchor, label, lang, traditional, simplified, pinyin, senses, classifiers):
        key = (anchor, traditional, simplified)
        if key in seen or (not senses and not classifiers):
            return
        seen.add(key)
        matches.append(
            {
                "anchor": anchor,
                "label": label,
                "lang": lang,
                "traditional": traditional,
                "simplified": simplified,
                "pinyin": pinyin,
                "senses": senses,
                "classifiers": classifiers,
            }
        )

    # From the results themselves (no extra database work).
    for entry in results:
        for definition in entry["definitions"]:
            add(
                definition["anchor"],
                definition["label"],
                definition["lang"],
                entry["traditional"],
                entry["simplified"],
                entry["pinyin"],
                definition["defs"],
                definition["classifiers"],
            )

    # A Chinese query that found no result may still be in other dictionaries, so
    # look it up explicitly (only then — otherwise it would duplicate the results).
    phrase = query.strip()
    if not results and db.has_cjk(phrase):
        for match in db.dictionary_matches(phrase):
            add(
                match["anchor"],
                match["label"],
                match["lang"],
                match["traditional"],
                match["simplified"],
                match["pinyin"],
                match["senses"],
                match["classifiers"],
            )
    return matches


def _expand_variants(results: list[dict]) -> list[dict]:
    """Replace "see X" variant results with X's own entries.

    A CEDICT entry whose definition is only "see X" is a variant form; searching
    it should simply show the main word's normal result.
    """
    expanded: list[dict] = []
    seen: set[tuple] = set()
    for entry in results:
        shown = db.lookup(entry["main"]) if entry.get("main") else [entry]
        for e in shown:
            # De-duplicate by headword, not reading: the capitalized (surname) and
            # plain versions of a word (e.g. 阮 Ruǎn / ruǎn) show once.
            key = (e["traditional"], e["simplified"])
            if key not in seen:
                seen.add(key)
                expanded.append(e)
    return expanded


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
    `pinyin_match` is true when the query was an exact Chinese reading — in which
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
        # "Matches in other dictionaries": every result's headword, plus a Chinese
        # query itself, as each dictionary defines it.
        matches = _build_matches(results, q)
    finally:
        statements = db.end_log()
    _record_search(q, (time.perf_counter() - started) * 1000, len(results), statements)
    return {
        "query": q,
        "pinyin_match": pinyin_match,
        "english_match": english_match,
        "results": results,
        "fuzzy": fuzzy,
        "matches": matches,
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
