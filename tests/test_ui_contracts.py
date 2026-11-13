"""Contracts for home-page markup and frontend features.

The Python suite cannot drive the browser, so these tests guard the specific
markup and source hooks that the recent UI changes rely on.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

JS_FILES = ("app.js", "custom_dicts.js", "glyphs.js")


def test_about_page_renders_readme_live(client, monkeypatch, tmp_path):
    """The About page is the README as HTML and re-reads it on every request."""
    from yabai_search import services as app_main

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
    assert "Debug Log" in html
    assert 'class="menu-item"' in html
    assert 'id="copy-log-button"' not in html
    assert "Copy debug log" not in html
    assert "glyphs.js" in html
    # The submit button is an icon button with an accessible label.
    assert 'class="search-button" aria-label="Search"' in html
    assert 'viewBox="0 0 24 24"' in html
    # The clear button starts hidden and is shown by app.js when text exists.
    assert 'title="Clear" hidden' in html


def test_home_page_shows_which_machine_is_serving(client, monkeypatch):
    """The hamburger menu names the host from YABAI_SERVED_BY."""
    monkeypatch.setenv("YABAI_SERVED_BY", "tablet")
    html = client.get("/").text
    assert 'class="menu-host"' in html
    assert "<strong>tablet</strong>" in html


def _origin_gate(monkeypatch, secret="test-secret"):
    """The gate as a plain callable: it answers 200 "ok" when a request gets through."""
    from django.http import HttpResponse

    from yabai_site.middleware import OriginSecretMiddleware

    monkeypatch.setenv("YABAI_ORIGIN_SECRET", secret)
    return OriginSecretMiddleware(lambda request: HttpResponse("ok"))


def test_origin_guard_redirects_public_browsers_to_the_public_site(monkeypatch):
    """A public browser on an origin hostname lands on the main site."""
    from django.test import RequestFactory

    gate = _origin_gate(monkeypatch)
    factory = RequestFactory()
    response = gate(
        factory.get("/about", HTTP_ACCEPT="text/html", REMOTE_ADDR="203.0.113.9")
    )
    assert response.status_code == 302
    assert response["Location"] == "https://yabaisearch.org/about"

    api = gate(
        factory.get(
            "/api/search", HTTP_ACCEPT="application/json", REMOTE_ADDR="203.0.113.9"
        )
    )
    assert api.status_code == 403


def test_origin_guard_allows_the_hosts_own_read_only_page(monkeypatch):
    """A visitor can open <node>.yabaisearch.org and its relative API calls."""
    from django.test import RequestFactory

    monkeypatch.setenv("YABAI_REPL_NODE", "tabs2")
    gate = _origin_gate(monkeypatch)
    factory = RequestFactory()

    page = gate(
        factory.get(
            "/?q=%E7%9B%B4",
            HTTP_HOST="tabs2.yabaisearch.org",
            HTTP_ACCEPT="text/html",
            REMOTE_ADDR="203.0.113.9",
        )
    )
    assert page.status_code == 200

    api = gate(
        factory.get(
            "/api/search?q=test",
            HTTP_HOST="tabs2.yabaisearch.org",
            HTTP_ACCEPT="application/json",
            REMOTE_ADDR="203.0.113.9",
        )
    )
    assert api.status_code == 200


def test_origin_guard_keeps_other_hosts_and_writes_gated(monkeypatch):
    """Only the machine's own name is opened, and only for GET/HEAD."""
    from django.test import RequestFactory

    monkeypatch.setenv("YABAI_REPL_NODE", "tabs2")
    gate = _origin_gate(monkeypatch)
    factory = RequestFactory()

    other = gate(
        factory.get(
            "/?q=test",
            HTTP_HOST="tabs.yabaisearch.org",
            HTTP_ACCEPT="text/html",
            REMOTE_ADDR="203.0.113.9",
        )
    )
    assert other.status_code == 302
    assert other["Location"] == "https://yabaisearch.org/?q=test"

    write = gate(
        factory.post(
            "/accounts/login/",
            HTTP_HOST="tabs2.yabaisearch.org",
            HTTP_ACCEPT="text/html",
            REMOTE_ADDR="203.0.113.9",
        )
    )
    assert write.status_code == 403


def test_origin_guard_allows_private_clients(monkeypatch):
    """The LAN and Tailscale may open the origin directly for debugging."""
    from django.test import RequestFactory

    gate = _origin_gate(monkeypatch)
    response = gate(RequestFactory().get("/__ping", REMOTE_ADDR="100.100.100.100"))
    assert response.status_code == 200


def test_origin_guard_accepts_the_worker_secret(monkeypatch):
    from django.test import RequestFactory

    gate = _origin_gate(monkeypatch)
    request = RequestFactory().get(
        "/api/search", REMOTE_ADDR="203.0.113.9", HTTP_X_ORIGIN_SECRET="test-secret"
    )
    assert gate(request).status_code == 200


def test_origin_guard_cannot_be_bypassed_with_a_forged_forwarded_for(monkeypatch):
    """cloudflared is the TCP peer (loopback); the visitor's address comes from
    the right-hand end of X-Forwarded-For, so a forged left-hand value is ignored."""
    from django.test import RequestFactory

    gate = _origin_gate(monkeypatch)
    request = RequestFactory().get(
        "/api/search",
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_FORWARDED_FOR="10.0.0.5, 203.0.113.9",
    )
    assert gate(request).status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("node", ["laptop", "pc", "tabs", "tabs2"])
def test_public_host_domains_serve_the_page_and_api(node, monkeypatch):
    """https://<node>.yabaisearch.org/?q=直 loads that host's read-only page."""
    from django.test import Client

    monkeypatch.setenv("YABAI_ORIGIN_SECRET", "test-secret")
    monkeypatch.setenv("YABAI_REPL_NODE", node)
    host = f"{node}.yabaisearch.org"
    client = Client()
    common = {"HTTP_HOST": host, "REMOTE_ADDR": "203.0.113.9"}

    page = client.get("/", {"q": "直"}, HTTP_ACCEPT="text/html", **common)
    assert page.status_code == 200
    assert "Yabai Search" in page.content.decode("utf-8")

    api = client.get(
        "/api/search",
        {"q": "直"},
        HTTP_ACCEPT="application/json",
        **common,
    )
    assert api.status_code == 200
    assert api.json()["query"] == "直"


@pytest.mark.parametrize("node", ["laptop", "pc", "tabs", "tabs2"])
def test_public_host_domains_do_not_open_another_host(node, monkeypatch):
    """A host's public name still only works on that host."""
    from django.test import Client

    monkeypatch.setenv("YABAI_ORIGIN_SECRET", "test-secret")
    monkeypatch.setenv("YABAI_REPL_NODE", node)
    other = "tabs2" if node != "tabs2" else "laptop"
    response = Client().get(
        "/",
        HTTP_HOST=f"{other}.yabaisearch.org",
        HTTP_ACCEPT="text/html",
        REMOTE_ADDR="203.0.113.9",
    )
    assert response.status_code == 302
    assert response["Location"] == "https://yabaisearch.org/"


def test_origin_guard_is_off_without_a_secret(monkeypatch):
    from django.http import HttpResponse
    from django.test import RequestFactory

    from yabai_site.middleware import OriginSecretMiddleware

    monkeypatch.delenv("YABAI_ORIGIN_SECRET", raising=False)
    gate = OriginSecretMiddleware(lambda request: HttpResponse("ok"))
    assert gate(RequestFactory().get("/", REMOTE_ADDR="203.0.113.9")).status_code == 200


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


def test_dictionaries_declared_as_merged_share_one_block():
    """國語辭典簡編本 and the revised dictionary share one block, concise first.

    The pair is declared once (the `merged` trait in db.py); the script merges whatever group the page declares.
    """
    from yabai_search import db

    declared = {item["anchor"]: item for item in db.dictionaries()}
    assert declared["moe"]["merged"] == ["moe", "moe_revised"]  # the lead first
    assert declared["moe_revised"].get("hidden")  # one visible row for the pair
    html = (ROOT / "yabai_search/templates/index.html").read_text(encoding="utf-8")
    assert "data-merged=" in html and "data-shown-as=" in html
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "function mergeDictionaryBlocks(blocks)" in js
    assert "members.every((member) => byAnchor.has(member))" in js
    # Exact duplicates after trimming are dropped, and selecting one member keeps the whole group so the
    # merge still happens.
    assert "seen.has(trimmed)" in js
    assert "const withMergedGroups" in js and "function expandMergedSelection" in js


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


def test_details_page_shows_details_in_the_results_slot():
    """The exact-word page labels the result count slot as Details."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert '? "Details"' in js
    assert "if (!exactMode) updateTiming();" in js
    assert "if (!count || exactMode || lastSearchMs === null)" in js
    assert ".exact-mode .results-count {" in css
    assert "display: block;" in css


def test_footer_separator_uses_the_last_result_tier():
    """The line below the results is the same hoverable tier separator."""
    html = (ROOT / "yabai_search/templates/index.html").read_text(encoding="utf-8")
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert 'class="footer-separator entry-separator"' in html
    assert "function updateFooterSeparator(tier)" in js
    assert "updateFooterSeparator(lastTier);" in js
    assert "updateFooterSeparator(fuzzyTier);" in js
    assert ".footer-separator + footer {" in css
    assert "border-top: 0;" in css


def test_custom_dicts_script_loads_only_after_an_import():
    """First-time visitors skip the 163 KB script; the toggle loads it on demand."""
    html = (ROOT / "yabai_search/templates/index.html").read_text(encoding="utf-8")
    assert 'need = on || url || has === "true";' in html
    # glyphs.js no longer blocks parsing; it still runs before app.js (defer
    # preserves document order). The path comes from the view, so the page can
    # link the minified copy.
    assert 'src="{{ static.glyphs_js }}?v={{ asset_version }}" defer' in html


def test_the_minified_assets_match_their_sources():
    """The shipped minified copies must be built from the current sources.

    scripts/generate-indexes-optimizations/build_static.py writes yabai_search/static/min/ and the manifest there holds the
    sha256 of the source each copy came from. A source edited without a rebuild would otherwise
    ship old code, so this fails until build_static.py runs again.
    """
    import hashlib
    import json

    static = ROOT / "yabai_search" / "static"
    manifest_path = static / "min" / "manifest.json"
    assert manifest_path.is_file(), (
        "yabai_search/static/min/manifest.json is missing: run python scripts/generate-indexes-optimizations/build_static.py"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, digest in manifest.items():
        source = static / name
        assert source.is_file(), f"{name} is missing"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest, (
            f"{name} changed since the minified copy was built: run python scripts/generate-indexes-optimizations/build_static.py"
        )
        assert (static / "min" / name).is_file(), f"min/{name} is missing"


def test_the_page_links_the_minified_assets_when_they_are_current():
    """A fresh minified copy is linked; an out-of-date one falls back to the source."""
    from yabai_search import services

    services._min_state.clear()
    assert services.static_path("app.js") == "/static/min/app.js"
    # Pretend the source changed after the build: the source itself is served again.
    original = services._MIN_DIR / "manifest.json"
    backup = original.read_text(encoding="utf-8")
    try:
        original.write_text("{}", encoding="utf-8")
        services._min_state.clear()
        assert services.static_path("app.js") == "/static/app.js"
    finally:
        original.write_text(backup, encoding="utf-8")
        services._min_state.clear()


def test_frontend_wildcard_tier_parity_with_backend():
    """The browser copies _glob_match / _wildcard_tier from db.py."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "function globMatch(pattern, word)" in js
    assert "function wildcardTier(words, query)" in js
    assert 'if (isWildcardQuery(text)) return wildcardTier(words, text);' in js
    assert "globMatch(glob, lower)" in js


def test_headword_reading_is_trimmed_to_spoken_characters():
    """Alternate source readings do not spill past the headword."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "function readingSyllablesForWord(pinyin, word)" in js
    assert "readingSyllablesForWord(pinyin, text).filter(" in js
    assert "readingSyllablesForWord(pinyin, word)" in js
    assert "syllables = syllables.slice(0, spoken);" in js


def test_pager_has_first_and_last_buttons():
    """Paged results can jump to the first and last page in one click."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert 'first.textContent = "First";' in js
    assert 'last.textContent = "Last";' in js
    assert 'search(query, 0, scrollMode)' in js
    assert 'search(query, (pages - 1) * size, scrollMode)' in js
    assert "prefetchOnIntent(first, 0);" in js
    assert "prefetchOnIntent(last, (pages - 1) * size);" in js


def test_definition_copy_converts_pinyin_to_zhuyin():
    """Copied definitions use zhuyin when the phonetic toggle is zhuyin."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "function pinyinToZhuyinForCopy(" in js
    assert "pinyinToZhuyinForCopy(definition, pinyin)" in js
    # Conversion is per token now, so a Latin token in a reading stays Latin.
    assert "phoneticTokens(syllables" in js


def test_search_url_carries_selected_dictionaries():
    """Changing the dictionary selection re-runs the server-side ranking."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "function selectedAnchorsParam()" in js
    assert "&dicts=${encodeURIComponent(dictsParam)}" in js
    assert "function rerunSearch()" in js
    assert "rerunSearch(); // the effective selected dictionaries just changed" in js
    assert "rerunSearch(); // the server picks the preview dictionary and the categories" in js


def test_selected_dictionaries_keep_the_drag_order():
    """The dicts parameter follows the visible row order, not an alphabetical sort."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    start = js.index("function selectedAnchorsParam()")
    end = js.index("\n}", start)
    body = js[start:end]
    assert 'document.querySelectorAll(".dict-toggle")' in body
    assert ".sort()" not in body
    # Both the mouse drop and the touch/pen drag must refetch with the new order.
    assert js.count("// The server chooses its preview dictionary from this order too.") == 2


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
    # The form words are now links: clicking "Other forms"/"old variant" searches them.
    assert "searchLink(word, toneColoredText(word, form.pinyin))" in js
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


def test_each_dictionary_links_to_its_own_site_from_its_traits():
    """A dictionary's look-up page comes from its lookup_url trait (db.py), not from code in app.js.

    中文维基词典 opens the word's page on zh.wiktionary.org, using the headword that source stored.
    """
    from yabai_search import db

    urls = {item["anchor"]: item.get("lookup_url") for item in db.dictionaries()}
    assert urls["zhwiktionary"] == "https://zh.wiktionary.org/wiki/"
    assert urls["jpwiktionary"] == "https://ja.wiktionary.org/wiki/"
    assert urls["wiktionary"] == "https://en.wiktionary.org/wiki/"
    assert urls["cedict"].startswith("https://cc-cedict.org/")
    html = (ROOT / "yabai_search/templates/index.html").read_text(encoding="utf-8")
    assert 'data-lookup-url="{{ d.lookup_url }}"' in html  # the toggle carries it to the page
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "lookupUrls.get(source.anchor)" in js
    assert "lookupUrl + encodeURIComponent(source.headword || word)" in js
    # No dictionary is named in the link code any more.
    assert "ZH_WIKTIONARY_PAGE" not in js and "CC_CEDICT_SEARCH" not in js


def test_the_page_names_no_dictionary_but_reads_their_declared_capabilities():
    """What a dictionary can do (variant forms, pronunciation notes, merged spellings) is declared in db.py.

    The script must not decide by a dictionary's name: it reads the capability from the dictionary's toggle.
    """
    from yabai_search import db

    declared = {item["anchor"]: item for item in db.dictionaries()}
    assert declared["cedict"].get("supplies_forms") and declared["cedict"].get("supplies_notes")
    assert declared["cedict"].get("shared_groups")
    assert not declared["wiktionary"].get("supplies_forms")
    html = (ROOT / "yabai_search/templates/index.html").read_text(encoding="utf-8")
    for attribute in ("data-supplies-forms", "data-supplies-notes", "data-shared-groups"):
        assert attribute in html
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    for name in ('"cedict"', '"wiktionary"', '"moe"', '"zhwiktionary"', '"jpwiktionary"', '"baidu"', '"unihan"'):
        assert name not in js, f"app.js decides by the dictionary name {name}"
    assert "formsAnchors.has(source.anchor)" in js and "notesAnchors.has(anchor)" in js


def test_dungan_reading_is_one_muted_line():
    """Dungan's Latin + tone numerals are not split into syllables."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert 'node.className = raw ? "furigana raw-reading" : "furigana";' in js
    assert 'entry.pinyin_label === "Dungan"' in js
    assert ".furigana.raw-reading .reading" in css


def test_extra_dialect_readings_are_notes_below_the_definitions():
    """One dialect reading sits above the headword; the rest become notes."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert "entry.readings.slice(1)" in js
    assert 'detailRow(`${extra.label || "Reading"}:`' in js
    # The note's reading sits above the headword like furigana, muted.
    assert "extra.pinyin" in js
    assert '(extra.label || "") === "Dungan"' in js
    assert ".def-source-cls .dialect-reading .reading" in css
    # The old per-line block under the headword is gone.
    assert "word-readings" not in js
    assert ".word-readings" not in css


def test_script_toggle_uses_the_server_layouts():
    """The page asks for a script and reuses the other layout from the response."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    assert "&script=${state.script}&merge=1" in js
    assert "function serverLayouts(data)" in js
    assert "currentLayouts = serverLayouts(full);" in js
    assert "const layout = currentLayouts && currentLayouts[state.script];" in js


def test_zhuyin_conversion_skips_dialect_readings():
    """Only Mandarin pinyin becomes bopomofo; dialect readings stay as written."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    # Conversion is per token: valid pinyin becomes bopomofo, Latin stays Latin.
    assert "function phoneticTokens(" in js
    assert "isPinyinSyllable(raw)" in js
    assert 'kind === "latin"' in js
    assert '["m", "n", "ng", "hm", "hng"].includes(base)' in js
    assert "/^[A-ZÜ]$/.test(raw)" in js
    # Dialect readings still pass allowTones=false.
    assert "allowTones: !entry.pinyin_no_tone" in js
    assert "!entry.pinyin_no_tone" in js
    # The old all-or-nothing guard is gone.
    assert "syllables.every((s) => isPinyinSyllable(s))" not in js


def test_ids_headword_keeps_readable_prefix():
    """An image only covers the IDS part; a normal prefix stays as text."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert "text.search(/[\\u2ff0-\\u2fff]/)" in js
    assert 'prefixNode.className = "ids-prefix"' in js
    assert ".ids-prefix" in css


def test_latin_reading_tokens_are_muted():
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert ".furigana .reading .latin" in css
    assert "color: var(--muted);" in css


def test_frontend_javascript_parses():
    """A syntax error in app.js breaks the whole page, so guard against it."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    for name in JS_FILES:
        path = ROOT / "yabai_search" / "static" / name
        result = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_pager_puts_buttons_below_page_text():
    """The page buttons sit under the "Page X of Y (N results)" line."""
    js = (ROOT / "yabai_search/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert "[label, first, prev, customJump, next, last]" in js
    assert "[label, first, prev, next, last]" in js
    assert "flex-wrap: wrap;" in css
    assert ".pagination .page-info" in css
    assert "flex-basis: 100%;" in css


def test_empty_avatar_links_to_login_and_icons_glow(client):
    """The blank avatar is a login link; both menu icons glow only on hover."""
    html = client.get("/").text
    assert 'class="menu-avatar" href="/accounts/login/" aria-label="Log in"' in html
    css = (ROOT / "yabai_search/static/style.css").read_text(encoding="utf-8")
    assert ".menu-avatar:hover" in css
    assert "box-shadow: 0 0 .55rem var(--accent)" in css
    assert ".menu-item:hover svg" in css
    assert "drop-shadow(0 0 4px var(--accent))" in css
