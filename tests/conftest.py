"""Shared fixtures for the search and word-page test suite."""

from __future__ import annotations

import ctypes
import glob
import os
from pathlib import Path

import pytest

# Keep the tests from writing archives/search.log.
os.environ.setdefault("YABAI_SQL_LOG", "0")
# No results on disk during tests: a list kept from an earlier run would hide what a test changes.
os.environ.setdefault("YABAI_RESULT_CACHE_MB", "0")
# The parser's background lookup warm-up adds CPU load that makes the browser
# selection test timing-flaky. The app's own tests do not need it.
os.environ.setdefault("YABAI_WARM_LOOKUPS", "0")
# Keep the test client's startup fast and deterministic; the page-cache warm-up
# is measured separately against a real gunicorn process.
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

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "yabai_site.settings")
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key")
os.environ.setdefault("DJANGO_DEBUG", "1")

import django  # noqa: E402

django.setup()

import json as _json  # noqa: E402
from urllib.parse import urlencode  # noqa: E402

from django.test import Client  # noqa: E402

from yabai_search import services  # noqa: E402


class _Response:
    """The few response attributes the tests use, matching the old TestClient."""

    def __init__(self, response):
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers

    @property
    def content(self) -> bytes:
        if getattr(self._response, "streaming", False):
            return b"".join(self._response.streaming_content)
        return self._response.content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self):
        return _json.loads(self.content)


class ApiClient:
    """get(url, params=...) and post(url, json=...), like FastAPI's TestClient."""

    def __init__(self) -> None:
        self._client = Client()

    @staticmethod
    def _url(url, params):
        if not params:
            return url
        return f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"

    def get(self, url, params=None, headers=None, follow_redirects=False):
        return _Response(self._client.get(self._url(url, params), headers=headers or {}))

    def post(self, url, json=None, params=None, data=None, headers=None):
        body = _json.dumps(json) if json is not None else (data or "")
        return _Response(
            self._client.post(
                self._url(url, params),
                data=body,
                content_type="application/json" if json is not None else "text/plain",
                headers=headers or {},
            )
        )


@pytest.fixture(scope="session")
def client():
    """A test client, with the search caches built once (as wsgi.py does)."""
    services.warm_caches()
    return ApiClient()


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
