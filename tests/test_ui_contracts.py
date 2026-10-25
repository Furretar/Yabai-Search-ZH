"""Contracts for home-page markup and frontend features.

The Python suite cannot drive the browser, so these tests guard the specific
markup and source hooks that the recent UI changes rely on.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_about_page_renders_readme_live(client, monkeypatch, tmp_path):
    """The About page is the README as HTML and re-reads it on every request."""
    import yabai_search.main as app_main

    html = client.get("/about").text
    assert "Sources &amp; Licenses" in html
    assert 'id="cedict"' in html
    assert "<table>" in html

    readme = tmp_path / "README.md"
    readme.write_text("# First version\n", encoding="utf-8")
    monkeypatch.setattr(app_main, "_README_PATH", readme)
    assert "First version" in client.get("/about").text

    readme.write_text("# Second version\n", encoding="utf-8")
    assert "Second version" in client.get("/about").text


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
    # The clear button starts hidden and is shown by app.js when text exists.
    assert 'title="Clear" hidden' in html


def test_secondary_pages_keep_the_site_title_and_badges(client):
    """About and word pages show the same title, ZH tag and BETA version."""
    pages = [
        client.get("/about").text,
        client.get("/word/国家").text,
    ]
    for html in pages:
        assert 'class="site-subtitle"' in html
        assert 'class="site-zh"' in html
        assert ">ZH<" in html
        assert "BETA" in html

    # The reports page is local-only, so check its template directly.
    reports = (ROOT / "yabai_search/templates/reports.html").read_text(
        encoding="utf-8"
    )
    assert 'class="site-subtitle"' in reports
    assert 'class="site-zh"' in reports
    assert ">ZH<" in reports
    assert "BETA" in reports


def test_clear_button_and_search_icon_contracts():
    """The clear button follows the input and the search icon has no border."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert "function updateClearButton" in js
    assert 'input.addEventListener("input", () => {' in js
    assert "lastInputAt = performance.now();" in js
    assert "updateClearButton();" in js
    start = css.find(".search-button {")
    end = css.find("}", start)
    assert "border: none;" in css[start:end]


def test_frontend_resets_dictionaries_and_forces_compact_mode():
    """Turning custom dictionaries off resets choices and enables compact mode."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
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
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert ".def-copy {" in css
    assert "width: 0;" in css
    assert "width: 1em;" in css
    assert "margin-left: .25rem;" in css
    assert "padding-right: 1.5rem;" not in css
    assert "transition: opacity .12s ease, color .12s ease;" in css
    assert "width .12s ease" not in css


def test_clear_button_keeps_scroll_in_pulled_mode():
    """Clearing the search in the pop-out panel preserves the scroll position."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "function releaseClearScrollGuard" in js
    assert 'document.body.dataset.clearScrollGuard = "true"' in js
    assert 'goHome(searchPanel.classList.contains("pulled"))' in js
    assert js.count("releaseClearScrollGuard();") >= 3


def test_frontend_keeps_reading_and_character_lines_separate():
    """Readings and characters are separate rows, so double-clicking either
    line selects that whole line and copies only that line."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert "allowTones = true" in js
    # All readings are inserted before all characters, so a range over one kind
    # stays contiguous for the double-click handler and the copy handler.
    reading_row = js.index('reading.className = "reading";')
    char_row = js.index('char.className = "char";')
    assert reading_row < char_row
    assert 'event.target.closest?.(".reading")' in js
    assert 'event.target.closest?.(".char")' in js
    assert 'node.classList.add("wrap")' not in js
    assert ".furigana.wrap {" not in css
    assert ".furi-pair" not in css
    assert ".furigana .reading {" in css
    assert ".furigana .char {" in css
    assert ".entry.long-headword .hanzi {" in css
