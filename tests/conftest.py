"""Shared fixtures for the search and word-page test suite."""

from __future__ import annotations

import ctypes
import glob
import os
from pathlib import Path

import pytest

# Keep the tests from writing data/search.log.
os.environ.setdefault("YABAI_SQL_LOG", "0")
# The parser's background lookup warm-up adds CPU load that makes the browser
# selection test timing-flaky. The app's own tests do not need it.
os.environ.setdefault("YABAI_WARM_LOOKUPS", "0")
# Keep the test client's startup fast and deterministic; the page-cache warm-up
# is measured separately against a real uvicorn process.
os.environ.setdefault("YABAI_WARM_PAGE_CACHE", "0")


def _preload_libstdcpp() -> None:
    """Load Nix's libstdc++ so the opencc extension imports without env setup."""
    if Path("/nix/store").exists() and "LD_LIBRARY_PATH" not in os.environ:
        for path in sorted(glob.glob("/nix/store/*-gcc-*-lib/lib/libstdc++.so.6")):
            try:
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
                return
            except OSError:
                continue


_preload_libstdcpp()

from fastapi.testclient import TestClient  # noqa: E402
from yabai_search.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    """A FastAPI test client, with the app's startup caches built once."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def search(client):
    """Call /api/search and return its JSON, failing on an HTTP error."""

    def run(query: str, **params):
        params.setdefault("limit", 25)
        response = client.get("/api/search", params={"q": query, **params})
        assert response.status_code == 200, response.text
        return response.json()

    return run


@pytest.fixture(scope="session")
def find_result():
    """Find an entry by simplified or traditional form in an API response."""

    def find(data, word, section="results"):
        for entry in data.get(section, []):
            if word in (entry.get("simplified"), entry.get("traditional")):
                return entry
        return None

    return find
