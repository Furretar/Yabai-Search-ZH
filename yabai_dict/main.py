"""FastAPI app for yabai-dict."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from yabai_dict import db

BASE = Path(__file__).resolve().parent

app = FastAPI(title="yabai-dict")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/search")
def api_search(q: str = "", limit: int = Query(50, ge=1, le=200)):
    return {"query": q, "results": db.search(q, limit)}
