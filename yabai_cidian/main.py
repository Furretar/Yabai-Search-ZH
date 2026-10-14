"""FastAPI app for yabai-cidian."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from yabai_cidian import db

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
