"""FastAPI app for yabai-cidian."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from yabai_cidian import db, segment

# Directory of this file (the yabai_cidian package). Used to locate assets
# regardless of the directory the server is launched from.
BASE = Path(__file__).resolve().parent

# The application object that uvicorn imports and runs.
app = FastAPI(title="yabai-cidian")

# Serve files from yabai_cidian/static/ at the URL prefix /static/...
# (e.g. /static/style.css, /static/app.js). Read-only, served verbatim.
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

# Tell Jinja2 where to find HTML templates.
templates = Jinja2Templates(directory=str(BASE / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Home page: render the (currently blank) index.html template."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/search")
def api_search(q: str = "", limit: int = Query(50, ge=1, le=200)):
    """JSON search endpoint. `q` is the term; `limit` is clamped to 1..200."""
    return {"query": q, "results": db.search(q, limit)}


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
