"""Browser tests for frontend interactions.

The other UI contract tests only read the source. These drive a real headless
Chromium because furigana selection depends on the browser's Range and
Selection APIs, and the traditional-row merge depends on the rendered result
rows.

The app runs in a separate gunicorn process. Starting it in a thread inside
pytest left the app's caches and server loop alive for the rest of the session,
which made the later in-process TestClient hang.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]

# Prefer Chromium; Chrome works too. The test is skipped when neither exists.
CHROMIUM = next(
    (
        path
        for name in ("chromium", "chromium-browser", "google-chrome")
        if (path := shutil.which(name))
    ),
    None,
)

# A same-origin page that loads the app in an iframe, waits for the long
# headword 发展中国家, then double-clicks each line and records what the
# selection and copy handler produce. Serving it from the app's own origin lets
# the parent script reach into the iframe; a cross-origin frame would be blocked.
HARNESS = """<!doctype html>
<meta charset="utf-8">
<title>furigana selection harness</title>
<iframe id="app" src="/?q=%E5%8F%91%E5%B1%95%E4%B8%AD%E5%9B%BD%E5%AE%B6"
        style="width:800px;height:600px"></iframe>
<pre id="out">pending</pre>
<script>
const out = document.getElementById("out");
const frame = document.getElementById("app");

// Double-click `node` inside the app, then report the selection and the text
// the copy handler would put on the clipboard.
function fire(win, node) {
    node.dispatchEvent(new win.MouseEvent("dblclick", {
        bubbles: true,
        cancelable: true,
        view: win,
    }));
    const selection = win.getSelection();
    const range = selection.rangeCount ? selection.getRangeAt(0) : null;
    return {
        selected: selection.toString(),
        copied: range ? win.furiganaCopyText(range) : null,
    };
}

let tries = 0;
function tick() {
    tries += 1;
    try {
        const doc = frame.contentDocument;
        const win = frame.contentWindow;
        // Wait until the no-flash hold is released. While the initial search is
        // pending the result area is visibility:hidden, and selecting text in a
        // hidden subtree gives an empty Selection.toString().
        const visible = doc && !doc.documentElement.classList.contains("initial-search-pending");
        // Find the specific headword, not just the first result, so the test
        // does not depend on the result ordering.
        const hanzi = visible && [...doc.querySelectorAll(".entry .hanzi")].find(
            (node) => node.querySelector(".char")?.textContent === "发展中国家"
        );
        if (hanzi) {
            const reading = hanzi.querySelector(".reading");
            const char = hanzi.querySelector(".char");
            out.textContent = JSON.stringify({
                order: [...hanzi.querySelectorAll(".reading, .char")].map(
                    (node) => node.className
                ),
                charClick: fire(win, char.querySelector(".char-cell")),
                readingClick: fire(win, reading.querySelector(".reading-cell")),
                pairs: hanzi.querySelectorAll(".furi-pair").length,
            });
            return;
        }
    } catch (error) {
        out.textContent = "error: " + error;
        return;
    }
    if (tries > 150) {
        out.textContent = "timeout";
        return;
    }
    setTimeout(tick, 100);
}
tick();
</script>
"""

# Same-origin harness for the traditional-mode row merge: wait for the search,
# then report each row's headword and only the notes the test cares about.
MERGE_HARNESS = """<!doctype html>
<meta charset="utf-8">
<title>sense group harness</title>
<iframe id="app" style="width:900px;height:800px"></iframe>
<pre id="out">pending</pre>
<script>
const out = document.getElementById("out");
const frame = document.getElementById("app");
const mode = new URLSearchParams(location.search).get("script") || "traditional";
frame.src = `/?q=%E5%8F%AA&script=${mode}&dicts=cedict`;
let tries = 0;
function tick() {
    tries += 1;
    try {
        const doc = frame.contentDocument;
        const loaded = doc && !doc.documentElement.classList.contains("initial-search-pending");
        if (loaded) {
            const rows = [...doc.querySelectorAll(".entry")].map((row) => ({
                head: row.querySelector(".hanzi")?.textContent || "",
                readings: [...row.querySelectorAll(".def-reading")]
                    .map((reading) => reading.textContent),
                spellingClasses: [...row.querySelectorAll(
                    ".def-reading-spellings [class^='tone']"
                )].map((char) => char.className),
                spellingLinks: row.querySelectorAll(".def-reading-spellings a").length,
                noteLinks: row.querySelectorAll(".def-source-cls a").length,
                lines: [...row.querySelectorAll(".def-source-line")]
                    .map((line) => line.textContent),
                notes: [...row.querySelectorAll(".def-source-cls")]
                    .map((note) => note.textContent)
                    .filter((text) => text.startsWith("Other forms")
                        || text.startsWith("Traditional forms")),
            }));
            if (rows.length) {
                const footer = doc.querySelector(".footer-separator");
                out.textContent = JSON.stringify({
                    rows,
                    footerTier: footer?.dataset.tier || "",
                });
                return;
            }
        }
    } catch (error) {
        out.textContent = "error: " + error;
        return;
    }
    if (tries > 150) {
        out.textContent = "timeout";
        return;
    }
    setTimeout(tick, 100);
}
tick();
</script>
"""

# Same-origin harness for the definition copy button: intercept the clipboard
# write and report the text, so the test can check that zhuyin is used.
COPY_HARNESS = """<!doctype html>
<meta charset="utf-8">
<title>definition copy harness</title>
<iframe id="app"
        src="/word/%E7%9B%B4?script=traditional&tone=1&phonetic=zhuyin&taiwan=0&custom=0&compact=1&lang=EN"
        style="width:900px;height:800px"></iframe>
<pre id="out">pending</pre>
<script>
const out = document.getElementById("out");
const frame = document.getElementById("app");
let tries = 0;
function tick() {
    tries += 1;
    try {
        const doc = frame.contentDocument;
        const win = frame.contentWindow;
        const loaded = doc && !doc.documentElement.classList.contains("initial-search-pending");
        if (loaded) {
            const row = [...doc.querySelectorAll(".entry")].find(
                (entry) => (entry.querySelector(".hanzi")?.textContent || "").includes("直")
            );
            if (row) {
                const line = [...row.querySelectorAll(".def-source-line")].find(
                    (node) => node.textContent.includes("Flame Emperors")
                );
                if (line) {
                    const count = doc.querySelector(".results-count");
                    win.navigator.clipboard.writeText = async (text) => {
                        out.textContent = JSON.stringify({
                            text,
                            count: count?.textContent || "",
                            timing: Boolean(count?.querySelector(".results-timing")),
                        });
                    };
                    line.querySelector(".def-copy").click();
                    return;
                }
            }
        }
    } catch (error) {
        out.textContent = "error: " + error;
        return;
    }
    if (tries > 150) {
        out.textContent = "timeout";
        return;
    }
    setTimeout(tick, 100);
}
tick();
</script>
"""

# The wrapper adds the same-origin harness pages ahead of the Django app. It is
# a WSGI application so gunicorn can serve it exactly like yabai_site.wsgi. It
# lives in a temporary file only while the browser tests run.
WRAPPER = """\
from yabai_site.wsgi import application as django_app

HARNESS = {harness}
MERGE_HARNESS = {merge_harness}
COPY_HARNESS = {copy_harness}

PAGES = {{
    "/__furigana_test__": HARNESS,
    "/__merge_test__": MERGE_HARNESS,
    "/__copy_test__": COPY_HARNESS,
}}


def html_page(environ, start_response, page):
    body = page.encode("utf-8")
    start_response(
        "200 OK",
        [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


def application(environ, start_response):
    page = PAGES.get(environ.get("PATH_INFO", ""))
    if page is not None:
        return html_page(environ, start_response, page)

    # The app sends X-Frame-Options: DENY in production. The harness embeds it
    # in a same-origin iframe, so drop that one header for the test server only.
    def allow_frame(status, headers, exc_info=None):
        headers = [
            (name, value)
            for name, value in headers
            if name.lower() != "x-frame-options"
        ]
        return start_response(status, headers, exc_info)

    return django_app(environ, allow_frame)
"""


def _free_port() -> int:
    """Ask the OS for a free localhost port, then release it for gunicorn."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def search_server():
    """Serve the real app plus the harness page from a separate process."""
    if not CHROMIUM:
        pytest.skip("Chromium is needed for the browser tests")

    workdir = tempfile.mkdtemp(prefix="yabai-browser-")
    wrapper = Path(workdir) / "harness_app.py"
    wrapper.write_text(
        WRAPPER.format(
            harness=repr(HARNESS),
            merge_harness=repr(MERGE_HARNESS),
            copy_harness=repr(COPY_HARNESS),
        ),
        encoding="utf-8",
    )
    log_path = Path(workdir) / "server.log"
    log = log_path.open("w", encoding="utf-8")
    port = _free_port()
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(BASE), env.get("PYTHONPATH")) if part
    )
    # The harness only needs the app to answer; building every search cache on
    # import would make start-up slower than the readiness deadline. The first
    # search builds what it needs, as it does for any cold production worker.
    env["YABAI_WARM_ON_START"] = "0"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gunicorn",
            "harness_app:application",
            "--bind",
            f"127.0.0.1:{port}",
            "--workers",
            "1",
            "--threads",
            "4",
            "--timeout",
            "30",
            "--log-level",
            "warning",
        ],
        cwd=workdir,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 12
        while True:
            if process.poll() is not None:
                log.close()
                output = log_path.read_text(encoding="utf-8")
                pytest.fail(f"The test server exited early:\n{output}")
            try:
                with urllib.request.urlopen(
                    f"{base}/__furigana_test__", timeout=0.5
                ) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                pass
            if time.monotonic() > deadline:
                pytest.fail("The test server did not start")
            time.sleep(0.1)
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
        shutil.rmtree(workdir, ignore_errors=True)


def _dump_report(url: str, tmp_path: Path) -> dict:
    """Run one harness in headless Chromium and return its JSON report.

    Some Chromium builds print the DOM and then never exit (seen on this NixOS host:
    even a one-line page hangs until killed), so waiting for the process to end timed
    every test out. Instead, read the output until the page is complete, then stop it.
    """
    process = subprocess.Popen(
        [
            CHROMIUM,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--disable-dev-shm-usage",
            f"--user-data-dir={tmp_path / 'chromium'}",
            "--virtual-time-budget=10000",
            "--dump-dom",
            url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    # Hard limit even if Chromium prints nothing: killing it closes stdout, ending the loop.
    timer = threading.Timer(14, process.kill)
    timer.start()
    chunks: list[str] = []
    try:
        assert process.stdout is not None
        # --dump-dom writes the whole document at once, ending with </html>.
        for line in process.stdout:
            chunks.append(line)
            if "</html>" in line:
                break
    finally:
        timer.cancel()
        process.kill()
        process.wait(timeout=5)

    dom = "".join(chunks)
    marker = '<pre id="out">'
    start = dom.find(marker)
    assert start != -1, "Chromium returned no report page"
    end = dom.find("</pre>", start)
    payload = dom[start + len(marker) : end]

    assert payload not in ("pending", "timeout"), payload
    assert not payload.startswith("error:"), payload
    return json.loads(payload)


def test_double_click_selects_one_furigana_line(search_server, tmp_path):
    """Each line selects and copies on its own, without the other line."""
    report = _dump_report(f"{search_server}/__furigana_test__", tmp_path)

    # Readings first, then characters: a range over one kind stays contiguous.
    assert report["order"] == ["reading", "char"]
    assert report["pairs"] == 0

    # Double-clicking a character selects only the character line.
    assert report["charClick"] == {
        "selected": "发展中国家",
        "copied": "发展中国家",
    }

    # Double-clicking a reading selects only the reading line. The DOM has no
    # spaces between cells, so selection.toString() has none; the copy handler
    # puts the spaces back.
    assert report["readingClick"]["selected"] == "fāzhǎnzhōngguójiā"
    assert report["readingClick"]["copied"] == "fā zhǎn zhōng guó jiā"


def test_traditional_zhi_spellings_keep_their_own_senses(search_server, tmp_path):
    """Traditional 隻/秖 each show only the senses from their own CEDICT row."""
    report = _dump_report(f"{search_server}/__merge_test__?script=traditional", tmp_path)
    rows = report["rows"]
    assert report["footerTier"] == "2"

    by_trad = {row["head"][-1]: row for row in rows if row["head"][-1:] in "隻秖只"}
    assert "classifier for birds" in by_trad["隻"]["lines"][0]
    assert by_trad["秖"]["lines"] == ["1.grain that has begun to ripen"]
    # CEDICT's one definition "only; merely; just" is one sense (";" separates close glosses, "/" senses).
    assert by_trad["只"]["lines"] == ["1.only; merely; just"]

    # The definitions differ now, so the identical-row fold must not run.
    assert "Other forms: 只, 秖" not in by_trad["隻"]["notes"]
    # The form words in the notes are now links (clickable to search them).
    assert sum(row["noteLinks"] for row in by_trad.values()) > 0


def test_simplified_zhi_labels_each_spelling_group(search_server, tmp_path):
    """Simplified 只 labels the 隻 and 秖 sense groups above their definitions."""
    report = _dump_report(f"{search_server}/__merge_test__?script=simplified", tmp_path)
    assert report["footerTier"] == "2"
    row = next(row for row in report["rows"] if "Traditional forms" in " ".join(row["notes"]))

    assert row["readings"] == ["zhǐ", "zhī (隻)", "zhī (秖)"]
    # Both labels are zhī, so the characters keep the tone-1 colour (red).
    assert row["spellingClasses"] == ["tone1", "tone1"]
    # Header spellings and notes are labels, not search links.
    assert row["spellingLinks"] == 0
    # The form words in the note are now links (clickable to search them).
    assert row["noteLinks"] > 0
    assert "Other forms: 祇, 衹" in row["notes"]
    assert "Traditional forms: 隻, 秖" in row["notes"]
    assert "1.classifier for birds" in " ".join(row["lines"])
    assert "1.grain that has begun to ripen" in row["lines"]


def test_definition_copy_converts_pinyin_to_zhuyin(search_server, tmp_path):
    """The copy button writes zhuyin for the reading and inline annotations."""
    report = _dump_report(f"{search_server}/__copy_test__", tmp_path)
    text = report["text"]
    assert text.startswith("ㄓˊ\n直\nㄓˊ (c. 2000 BC)")
    assert "炎帝[ㄧㄢˊ ㄉㄧˋ]" in text
    assert "神農|神农[ㄕㄣˊ ㄋㄨㄥˊ]" in text
    assert "[Yan2 di4]" not in text
    assert "[Shen2 nong2]" not in text


def test_details_page_uses_the_results_slot_for_a_details_label(search_server, tmp_path):
    """The exact-word page shows Details where the search page shows N RESULTS."""
    report = _dump_report(f"{search_server}/__copy_test__", tmp_path)
    assert report["count"] == "Details"
    assert report["timing"] is False
