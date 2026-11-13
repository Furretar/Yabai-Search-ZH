#!/usr/bin/env python3
"""Rebuild the segmenter's files: the jieba user dict (every headword) and its loaded-state cache.

    data/dl-base-dictionaries/cedict_userdict.txt          one "word 20000" line per headword
    data/generated-indexes-optimizations/segmenter.pickle   jieba's word table with that list merged in

The app loads the cache at start (one file read). Without a matching cache it merges the user dict
itself, which takes minutes on a tablet. Serving machines never rebuild either file.

Usage (from the project root):
    uv run python scripts/generate-indexes-optimizations/build_segmenter.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from yabai_search import segment  # noqa: PLC0415  (needs the project's dependencies)

    started = time.time()
    count = segment.build_userdict()
    path = segment.build_cache()
    print(f"segmenter: {count:,} headwords, {path} ({path.stat().st_size / 1e6:.1f} MB) in {time.time() - started:.0f} s")


if __name__ == "__main__":
    main()
