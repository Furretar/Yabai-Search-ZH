#!/usr/bin/env python3
"""Minify the page's scripts and stylesheet into yabai_search/static/min/.

    app.js 259 KB -> 111 KB (68 -> 31 KB gzipped), custom_dicts.js 164 -> 59 KB, style.css 93 -> 48 KB

The page links a minified file only while it matches its source (min/manifest.json holds each
source's sha256), so an edit in the dev copy is served from the source at once and a forgotten
rebuild can never ship old code. tests/test_ui_contracts.py fails when the manifest is out of date,
which stops a release. Top-level names are kept (the files are plain scripts that share globals).

Needs esbuild: on PATH, or through `nix run nixpkgs#esbuild`.

Usage (from the project root):
    python scripts/build_static.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "yabai_search" / "static"
OUT = STATIC / "min"
FILES = ["app.js", "custom_dicts.js", "glyphs.js", "inline_furigana.js", "hosts.js", "style.css"]


def esbuild() -> list[str]:
    if shutil.which("esbuild"):
        return ["esbuild"]
    if shutil.which("nix"):
        return ["nix", "run", "nixpkgs#esbuild", "--"]
    sys.exit("esbuild not found (install it, or run where nix is available)")


def main() -> None:
    command = esbuild()
    OUT.mkdir(exist_ok=True)
    manifest: dict[str, str] = {}
    for name in FILES:
        source = STATIC / name
        data = source.read_bytes()
        loader = "css" if name.endswith(".css") else "js"
        result = subprocess.run(
            [*command, "--minify", f"--loader={loader}", "--charset=utf8", "--log-level=warning"],
            input=data, capture_output=True, check=False,
        )
        if result.returncode:
            sys.exit(f"esbuild failed on {name}:\n{result.stderr.decode()}")
        (OUT / name).write_bytes(result.stdout)
        manifest[name] = hashlib.sha256(data).hexdigest()
        print(f"  {name:22s} {len(data):>8,} -> {len(result.stdout):>8,} bytes")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
