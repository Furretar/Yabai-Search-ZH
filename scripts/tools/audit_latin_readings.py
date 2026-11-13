#!/usr/bin/env python3
"""List CEDICT readings the Latin-token classifier treats as non-pinyin.

    uv run python scripts/tools/audit_latin_readings.py

Read-only. Use the output to tune the allow-list in `yabai_search/pinyin.py`
before relying on the classifier for search filtering.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from yabai_search import db  # noqa: E402
from yabai_search.pinyin import is_standard_pinyin, latin_tokens  # noqa: E402


def main() -> int:
    connection = db.connect()
    try:
        rows = connection.execute(
            "SELECT simplified, traditional, pinyin FROM entries "
            "WHERE pinyin <> ''"
        ).fetchall()
    finally:
        connection.close()

    flagged = [
        (simplified, traditional, pinyin, latin_tokens(pinyin))
        for simplified, traditional, pinyin in rows
        if not is_standard_pinyin(pinyin)
    ]
    print(f"{len(flagged)} of {len(rows)} CEDICT readings are flagged")
    for simplified, traditional, pinyin, tokens in flagged[:200]:
        print(f"{simplified}\t{traditional}\t{pinyin}\t{tokens}")
    if len(flagged) > 200:
        print(f"... and {len(flagged) - 200} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
