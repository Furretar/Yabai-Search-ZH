"""Shared tail of every source importer: rebuild the derived indexes once the source has changed.

Several indexes (definition_grams, gram_words, head_grams, ...) are exact negative filters built from
the source databases; one that is older than its sources hides results that exist. So an importer ends
with `sys.exit(_indexes.run(main))` instead of `sys.exit(main())`. Pass --no-indexes (or set
YABAI_NO_INDEXES=1) to skip it, for example when several importers run one after another and one
rebuild at the end is enough:  uv run python scripts/generate-indexes-optimizations/rebuild_all.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REBUILD = Path(__file__).resolve().parents[1] / "generate-indexes-optimizations" / "rebuild_all.py"


def run(main) -> int:
    """Run the importer's main(), then rebuild the indexes unless the import failed or was told not to."""
    skip = "--no-indexes" in sys.argv or os.environ.get("YABAI_NO_INDEXES") == "1"
    sys.argv = [arg for arg in sys.argv if arg != "--no-indexes"]
    code = main()
    if code:
        return code
    if skip:
        print("indexes NOT rebuilt (--no-indexes): run scripts/generate-indexes-optimizations/rebuild_all.py")
        return 0
    print("rebuilding the derived indexes (scripts/generate-indexes-optimizations/rebuild_all.py) ...", flush=True)
    return subprocess.run([sys.executable, str(REBUILD)]).returncode
