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

    readme = tmp_path / "README.md"
    readme.write_text("# First version\n", encoding="utf-8")
    monkeypatch.setattr(app_main, "_README_PATH", readme)
    assert "First version" in client.get("/about").text

    readme.write_text("# Second version\n", encoding="utf-8")
    assert "Second version" in client.get("/about").text


def test_home_page_has_search_title_and_debug_download(client):
    """The home page has the placeholder, linked title and menu download."""
    html = client.get("/").text
    assert 'placeholder="Search with English, Chinese, or Pinyin"' in html
    assert 'id="home-link"' in html
    assert 'class="site-subtitle"' in html
    # The debug log download lives in the hamburger menu for every client.
    assert 'id="download-log-button"' in html
    assert "Download debug log" in html
    assert 'class="menu-item"' in html
    assert 'id="copy-log-button"' not in html
    assert "Copy debug log" not in html
    assert "glyphs.js" in html
    # The submit button is an icon button with an accessible label.
    assert 'class="search-button" aria-label="Search"' in html
    assert 'viewBox="0 0 24 24"' in html
    # The clear button starts hidden and is shown by app.js when text exists.
    assert 'title="Clear" hidden' in html


def test_secondary_pages_keep_the_site_title_and_byline(client):
    """About and word pages show the title, author and version under it."""
    pages = [
        client.get("/about").text,
        client.get("/word/国家").text,
    ]
    for html in pages:
        assert 'class="site-subtitle"' in html
        assert 'class="site-byline"' in html
        assert "Made by Furretar" in html
        assert "Beta v0.1" in html
        assert 'class="site-zh"' not in html
        assert "BETA" not in html

    # The reports page is local-only, so check its template directly.
    reports = (ROOT / "yabai_search/templates/reports.html").read_text(
        encoding="utf-8"
    )
    assert 'class="site-subtitle"' in reports
    assert 'class="site-byline"' in reports
    assert "Made by Furretar" in reports
    assert "Beta v0.1" in reports
    assert 'class="site-zh"' not in reports
    assert "BETA" not in reports


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


def test_footer_separator_uses_the_last_result_tier():
    """The line below the results is the same hoverable tier separator."""
    html = (ROOT / "yabai_search/templates/index.html").read_text(encoding="utf-8")
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert 'class="footer-separator entry-separator"' in html
    assert "function updateFooterSeparator(tier)" in js
    assert "updateFooterSeparator(maxTier);" in js
    assert "updateFooterSeparator(fuzzyTier);" in js
    assert ".footer-separator + footer {" in css
    assert "border-top: 0;" in css


def test_definition_copy_converts_pinyin_to_zhuyin():
    """Copied definitions use zhuyin when the phonetic toggle is zhuyin."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "function pinyinToZhuyinForCopy(" in js
    assert "pinyinToZhuyinForCopy(definition, pinyin)" in js
    assert 'syllables.map((s) => toZhuyin(s)).join(" ")' in js


def test_definition_notes_share_the_same_top_spacing():
    """Other forms, Traditional forms and the other notes all use one spacing."""
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    start = css.index(".def-source-cls {")
    end = css.index("}", start)
    assert "margin-top: .4rem;" in css[start:end]
    assert ".def-source .def-source-cls {" not in css


def test_clear_button_keeps_scroll_in_pulled_mode():
    """Clearing the search in the pop-out panel preserves the scroll position."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "function releaseClearScrollGuard" in js
    assert 'document.body.dataset.clearScrollGuard = "true"' in js
    assert 'goHome(searchPanel.classList.contains("pulled"))' in js
    assert js.count("releaseClearScrollGuard();") >= 3


def test_parsed_area_reserves_space_for_furigana():
    """Wrapped parsed lines leave room for the floating readings above them."""
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    start = css.index("#parsed {")
    end = css.index("}", start)
    rule = css[start:end]
    assert "line-height: 3.2;" in rule


def test_timing_tooltip_uses_lag_and_db_timings():
    """The timing tooltip shows lag, DB times and first paint."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "Lag: " in js
    assert "First results DB: " in js
    assert "All results DB: " in js
    assert "First paint: " in js


def test_debug_log_can_be_downloaded_as_txt():
    """The menu item offers a .txt download built from the snapshot text."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "function downloadDebugLog(" in js
    assert "new Blob([text], { type: \"text/plain;charset=utf-8\" })" in js
    assert "link.download = `yabai-debug-${stamp}.txt`" in js
    assert 'document.getElementById("download-log-button")' in js


def test_cedict_reading_groups_label_the_spelling():
    """A merged reading group names the spelling its senses came from."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert 'function mergeSharedGroups(blocks, headword = "")' in js
    assert "const label = spelling && spelling !== headword ? spelling : \"\";" in js
    assert "spellings: label ? [label] : []" in js
    assert "mergeSharedGroups(blocks, displayWord(entry))" in js
    assert "spellings.append(toneColoredText(word, group.pinyin));" in js
    assert "searchLink(word, toneColoredText(word, group.pinyin))" not in js
    assert "searchLink(word, toneColoredText(word, form.pinyin))" not in js
    assert "def-reading-spellings" in js
    assert ".def-reading .def-reading-spellings {" in css


def test_traditional_mode_folds_identical_definition_rows():
    """Traditional rows with the same visible definitions share one row."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "function mergeIdenticalRows(rows)" in js
    assert 'return state.script === "traditional" ? mergeIdenticalRows(rows) : rows;' in js
    # Hidden dictionaries must not prevent a merge of identical visible ones.
    assert "const visible = visibleDicts(row);" in js
    assert ".filter((block) => visible.has(block.anchor))" in js
    # A row with no definitions is never folded.
    assert "if (!hasSenses)" in js
    # The folded spelling joins Other forms instead of getting its own note.
    assert "existing.forms.push({" in js
    assert "Same definitions as:" not in js


def test_long_pastes_skip_the_dictionary_search():
    """A pasted paragraph goes straight to /api/annotate, not the search tiers."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "const ANNOTATE_DIRECT_LEN = 40;" in js
    assert "Skipped dictionary search: long CJK text" in js
    assert "function fetchAnnotate(" in js


def test_furigana_layout_is_batched():
    """Cell widths are measured in one batch, not one forced layout per cell."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "const pendingFurigana = new Set();" in js
    assert "function flushFuriganaLayout(" in js
    assert "queueMicrotask(flushFuriganaLayout)" in js


def test_taiwan_mode_hides_duplicate_colloquial_note():
    """A colloquial reading that repeats the Taiwan reading is not shown twice."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "duplicateTaiwan" in js
    assert 'register === "colloquial"' in js
    assert "numericToMarked(note.reading) === reading" in js


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


def test_about_page_links_fineweb2_frequency(client):
    """The About page names the temporary FineWeb2 frequency source."""
    html = client.get("/about").text
    assert "https://github.com/Furretar/FineWeb2-Mandarin-Frequency-List" in html
    assert 'id="fineweb2-frequency"' in html


def test_frequency_chip_has_rank_tooltip():
    """The frequency chip explains the useful range of the rank."""
    text = (
        "Frequency under 20,000 is common. "
        "This number is not reliable past 100,000."
    )
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    html = (ROOT / "yabai_search/templates/word.html").read_text(encoding="utf-8")
    assert text in js
    assert text in html
    assert "chip.title = FREQ_TOOLTIP" in js
