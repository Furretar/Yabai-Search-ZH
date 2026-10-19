"""Contracts for home-page markup and frontend features.

The Python suite cannot drive the browser, so these tests guard the specific
markup and source hooks that the recent UI changes rely on.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_home_page_has_search_title_and_debug_button(client):
    """The home page has the corrected placeholder, linked title and debug log."""
    html = client.get("/").text
    assert 'placeholder="Search with English, Chinese, or Pinyin"' in html
    assert 'id="home-link"' in html
    assert 'class="site-subtitle"' in html
    # The debug log is available to every client, not only local requests.
    assert 'id="copy-log-button"' in html
    assert "Copy debug log" in html
    assert "glyphs.js" in html
    # The submit button is an icon button with an accessible label.
    assert 'class="search-button" aria-label="Search"' in html
    assert 'viewBox="0 0 24 24"' in html


def test_frontend_resets_dictionaries_and_forces_compact_mode():
    """Turning custom dictionaries off resets choices and enables compact mode."""
    js = (ROOT / "yabai_chaci/static/app.js").read_text(encoding="utf-8")
    assert "function defaultDictsForLanguage" in js
    assert "function resetDictSettings" in js
    assert js.count("state.compact = true;") >= 2
    assert "resetDictSettings();" in js
    # Compact/detailed is unavailable with custom dictionaries off, so the
    # selection logic must use the search language instead of anchor defaults.
    assert "In default mode the search language decides" in js
    assert "sourceLang === lang" in js


def test_definition_copy_button_does_not_add_line_height():
    """The copy icon collapses to zero width and expands after the text."""
    css = (ROOT / "yabai_chaci/static/style.css").read_text(encoding="utf-8")
    assert ".def-copy {" in css
    assert "width: 0;" in css
    assert "width: 1em;" in css
    assert "margin-left: .25rem;" in css
    assert "padding-right: 1.5rem;" not in css
    assert "transition: opacity .12s ease, color .12s ease;" in css
    assert "width .12s ease" not in css


def test_clear_button_keeps_scroll_in_pulled_mode():
    """Clearing the search in the pop-out panel preserves the scroll position."""
    js = (ROOT / "yabai_chaci/static/app.js").read_text(encoding="utf-8")
    assert "function releaseClearScrollGuard" in js
    assert 'document.body.dataset.clearScrollGuard = "true"' in js
    assert 'goHome(searchPanel.classList.contains("pulled"))' in js
    assert js.count("releaseClearScrollGuard();") >= 3


def test_frontend_wraps_long_headwords_as_pairs():
    """Long headwords use the paired furigana layout with normal reading size."""
    js = (ROOT / "yabai_chaci/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "yabai_chaci/static/style.css").read_text(encoding="utf-8")
    assert "allowTones = true, wrapPairs = false" in js
    assert 'node.classList.add("wrap")' in js
    assert "longHeadword" in js
    assert ".furigana.wrap {" in css
    assert ".furigana.wrap .furi-pair {" in css
    assert ".furigana.wrap .reading-cell {" in css
    assert "font-size: var(--furigana-reading);" in css
    assert ".entry.long-headword .hanzi {" in css
