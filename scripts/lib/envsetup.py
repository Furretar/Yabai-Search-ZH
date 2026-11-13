"""Make sure a top-level script runs with the project's dependencies (standard library only).

The native `opencc` wheel needs libstdc++, which on NixOS only exists inside `nix develop`. When the
current interpreter cannot import what the project needs, the script re-runs itself once under
`nix develop -c uv run python ...` (YABAI_IN_NIX marks the second run so it can never loop).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def can_import_project() -> tuple[bool, str]:
    sys.path.insert(0, str(ROOT))
    try:
        import opencc  # noqa: F401
        import django  # noqa: F401

        return True, ""
    except Exception as error:  # ImportError, or the loader error for a missing libstdc++
        return False, f"{type(error).__name__}: {str(error).splitlines()[0] if str(error) else ''}"


def ensure_project_environment(script: str, argv: list[str], log=None) -> None:
    """Return when the project can be imported; otherwise re-exec under nix develop (does not return)."""
    ok, why = can_import_project()
    if ok:
        return
    say = log.warn if log else (lambda m: print(m, flush=True))
    if os.environ.get("YABAI_IN_NIX") == "1" or not shutil.which("nix"):
        say(f"the project's dependencies cannot be imported here ({why}); continuing anyway")
        return
    say(f"dependencies not importable ({why}); re-running inside `nix develop` ...")
    os.environ["YABAI_IN_NIX"] = "1"
    os.chdir(ROOT)
    os.execvp("nix", ["nix", "develop", "-c", "uv", "run", "python", script, *argv])
