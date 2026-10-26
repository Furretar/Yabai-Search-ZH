"""Tests for the standalone word detail page."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "cedict.sqlite").exists(), reason="CEDICT database not built"
)


def test_word_page_has_no_search_controls(client):
    """The details page is information-only, with no search bar or panels."""
    response = client.get("/word/档案")
    assert response.status_code == 200
    assert 'id="search-form"' not in response.text
    assert "search-panel" not in response.text


def test_word_page_shows_frequency_hsk_and_revised_moe(client):
    """The page shows the same metadata and dictionary blocks as a result row."""
    response = client.get("/word/档案")
    assert response.status_code == 200
    match = re.search(r"Frequency: #([\d,]+)", response.text)
    assert match and int(match.group(1).replace(",", "")) > 0
    assert "Frequency under 20,000 is common. This number is not reliable past 100,000." in response.text
    assert "HSK 6" in response.text
    assert '<div class="sources-label">Dictionaries</div>' in response.text
    assert "重編國語辭典修訂本" in response.text


def test_word_page_works_for_source_only_words(client):
    """A word that only a secondary dictionary has still gets a details page."""
    response = client.get("/word/劣性")
    assert response.status_code == 200
    assert "劣性" in response.text
    assert "重編國語辭典修訂本" in response.text


def test_word_page_accepts_traditional_forms(client):
    """The route matches both script forms, not just simplified input."""
    response = client.get("/word/檔案")
    assert response.status_code == 200
    assert "檔案" in response.text
    assert "档案" in response.text


def test_word_page_returns_404_for_unknown_words(client):
    """An unknown non-Chinese word has no details page."""
    assert client.get("/word/qqzzxxnotaword").status_code == 404


def test_word_page_dictionary_headers_are_links(client):
    """Each dictionary header links to its source or the About-page anchor."""
    html = client.get("/word/档案").text
    assert 'class="word-source-name"' in html
    assert "https://cc-cedict.org/editor/" in html


GLYPH = DATA / "moe_glyphs" / "8ff0._104_0.gif"


@pytest.mark.skipif(not GLYPH.exists(), reason="MOE glyph images not extracted")
def test_moe_glyph_route_serves_a_picture(client):
    """The glyph route serves an extracted MOE glyph image."""
    response = client.get("/moe-glyphs/8ff0._104_0.gif")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/gif"
    assert response.content


@pytest.mark.skipif(not GLYPH.exists(), reason="MOE glyph images not extracted")
def test_moe_glyph_route_rejects_traversal_and_missing_files(client):
    """Only plain picture names are served; traversal and missing files 404."""
    assert client.get("/moe-glyphs/../cedict.sqlite").status_code == 404
    assert client.get("/moe-glyphs/does-not-exist.gif").status_code == 404
