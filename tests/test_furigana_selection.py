"""Browser test for furigana line selection.

The other UI contract tests only read the source. This one drives a real
headless Chromium, because double-click selection and copy behaviour depend on
the browser's Range and Selection APIs.

The app runs in a separate process. Starting it in a thread inside pytest left
uvicorn's event loop and the app's caches alive for the rest of the session,
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
        // Find the specific headword, not just the first result, so the test
        // does not depend on the result ordering.
        const hanzi = doc && [...doc.querySelectorAll(".entry .hanzi")].find(
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

# The wrapper adds the same-origin harness page to the real app, then mounts the
# app at /. It lives in a temporary file only while the browser test runs.
WRAPPER = """\
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from yabai_search.main import app as real_app

HARNESS = {harness}

app = FastAPI()


@app.get("/__furigana_test__")
def harness() -> HTMLResponse:
    return HTMLResponse(HARNESS)


app.mount("/", real_app)
"""


def _free_port() -> int:
    """Ask the OS for a free localhost port, then release it for uvicorn."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def search_server():
    """Serve the real app plus the harness page from a separate process."""
    if not CHROMIUM:
        pytest.skip("Chromium is needed for the furigana selection test")

    workdir = tempfile.mkdtemp(prefix="yabai-furigana-")
    wrapper = Path(workdir) / "harness_app.py"
    wrapper.write_text(WRAPPER.format(harness=repr(HARNESS)), encoding="utf-8")
    log_path = Path(workdir) / "server.log"
    log = log_path.open("w", encoding="utf-8")
    port = _free_port()
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(BASE), env.get("PYTHONPATH")) if part
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "harness_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
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


def test_double_click_selects_one_furigana_line(search_server, tmp_path):
    """Each line selects and copies on its own, without the other line."""
    result = subprocess.run(
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
            f"{search_server}/__furigana_test__",
        ],
        capture_output=True,
        text=True,
        timeout=14,
    )

    dom = result.stdout
    marker = '<pre id="out">'
    start = dom.find(marker)
    assert start != -1, result.stderr
    end = dom.find("</pre>", start)
    payload = dom[start + len(marker) : end]

    assert payload not in ("pending", "timeout"), payload
    assert not payload.startswith("error:"), payload
    report = json.loads(payload)

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
