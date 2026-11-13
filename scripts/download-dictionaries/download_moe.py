#!/usr/bin/env python3
"""Download the Taiwan MOE Mandarin dictionary data (教育部國語辭典).

Each dictionary has a "資料下載" page listing a versioned text-database .zip.
This scrapes the current link from that page and downloads it, so it keeps
working across version bumps.

Usage (from the project root):
    uv run python scripts/download-dictionaries/download_moe.py            # 國語辭典簡編本 (default)
    uv run python scripts/download-dictionaries/download_moe.py revised    # 重編國語辭典修訂本
    uv run python scripts/download-dictionaries/download_moe.py mini       # 國語小字典
    uv run python scripts/download-dictionaries/download_moe.py idioms     # 成語典
    uv run python scripts/download-dictionaries/download_moe.py revised --pics
        # also fetch the 字圖 zip (glyph images for characters Unicode can't
        # represent, referenced in definitions as &8ff0._104_0.gif;)

Note: the data is released under CC BY-ND 3.0 TW (Attribution, NoDerivatives).
"""

from __future__ import annotations

import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://language.moe.gov.tw/001/Upload/Files/site_content/M0001/respub/"
PAGES = {
    "revised": "dict_reviseddict_download.html",
    "concised": "dict_concised_download.html",
    "mini": "dict_mini_download.html",
    "idioms": "dict_idiomsdict_download.html",
}
ROOT = Path(__file__).resolve().parents[2]
# Raw downloads only; scripts/download-dictionaries/import_moe.py builds data/dictionary-databases/moe-taiwan/moe.sqlite from them.
OUT_DIR = ROOT / "data" / "dl-base-dictionaries"

# Links to downloadable zips on a dictionary's page, e.g. "download/..._2014_20260929.zip".
ZIP_RE = re.compile(r'href="(download/[^"]+\.zip)"')
# The text database only; skip the image and audio archives.
NON_TEXT_RE = re.compile(r"_(pic|music|img)")
# The glyph-image archive ("字圖" - pictures for characters Unicode can't show).
PIC_RE = re.compile(r"_pic")


def open_url(url: str):
    """Open a URL, retrying with an unverified TLS context if verification fails.

    The MOE server's certificate lacks a Subject Key Identifier, which strict
    OpenSSL rejects (curl happens to accept it). We only relax verification for
    that fallback.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "yabai-search/0.1"})
    try:
        return urllib.request.urlopen(request, timeout=30)
    except (urllib.error.URLError, ssl.SSLError):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(request, timeout=30, context=context)


def fetch(url: str) -> bytes:
    with open_url(url) as response:
        return response.read()


def latest_zip(dict_key: str, pic: bool = False) -> str | None:
    """Return the (relative) URL of the newest matching zip on the page.

    The default is the text database; `pic=True` picks the 字圖 (glyph image)
    archive instead.
    """
    page = fetch(BASE + PAGES[dict_key]).decode("utf-8", "replace")
    # De-duplicate while keeping order; the current version is listed first.
    zips = list(dict.fromkeys(ZIP_RE.findall(page)))
    for z in zips:
        if pic and PIC_RE.search(z):
            return z
        if not pic and not NON_TEXT_RE.search(z):
            return z
    return None


def download(dict_key: str, pic: bool = False) -> int:
    relative = latest_zip(dict_key, pic=pic)
    if not relative:
        kind = "picture" if pic else "text"
        print(f"no {kind}-data zip found for '{dict_key}'")
        return 1

    url = BASE + relative
    dest = OUT_DIR / Path(relative).name
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"downloading {url}")
    with open_url(url) as response, dest.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    print(f"  saved {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return 0


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg != "--pics"]
    pic = "--pics" in sys.argv[1:]
    dict_key = args[0] if args else "concised"
    if dict_key not in PAGES:
        print(f"unknown dictionary: {dict_key!r}. Choose from: {', '.join(PAGES)}")
        return 2
    return download(dict_key, pic=pic)


if __name__ == "__main__":
    sys.exit(main())
