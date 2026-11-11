#!/usr/bin/env python3
"""Download the Unicode Han Database (Unihan) to data/unihan/Unihan.zip.

Unihan is Unicode's per-character data for about 103,000 CJK ideographs: readings,
English glosses, variants, radicals and stroke counts. It holds no words, only single
characters. scripts/import_unihan.py turns the Chinese parts of it into data/unihan.sqlite.

Usage (from the project root):
    uv run python -m scripts.download_unihan           # skip if already downloaded
    uv run python -m scripts.download_unihan --force   # fetch the latest again

Licence: Unicode License V3 (https://www.unicode.org/license.txt). Redistribution is
allowed as long as the copyright and permission notice goes with the data or its docs.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path

# "latest" always points at the newest Unicode version, so this keeps working across releases.
URL = "https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "unihan" / "Unihan.zip"


def download(force: bool = False) -> Path:
    """Fetch Unihan.zip unless it is already there; return its path."""
    if OUT.exists() and not force:
        print(f"{OUT} already exists (use --force to fetch it again)")
        return OUT
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".zip.part")
    request = urllib.request.Request(URL, headers={"User-Agent": "yabai-search/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    # Rename only after the whole file arrived, so a broken download never looks complete.
    tmp.replace(OUT)
    print(f"downloaded {OUT} ({OUT.stat().st_size // 1024} KiB)")
    return OUT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="download even if present")
    args = parser.parse_args(argv)
    download(args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
