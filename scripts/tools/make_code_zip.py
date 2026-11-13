#!/usr/bin/env python3
"""Pack the source tree into one zip, for an archive or to hand the code over.

The archive holds what someone needs to read and run the code, nothing that is
generated or machine-local. Folders whose name starts with "." (".git",
".venv", ".pytest_cache"), the generated "data" folder and the "archives"
folder (older zips) are skipped, and so is "__pycache__" (bytecode Python
recreates on the next run). The exceptions are data/search.log, the last few
searches with their SQL timings, and data/dummy/, the small stand-in databases
built by scripts/tools/build_test_dbs.py: both are small and belong in an archive.

The zip is written into archives/ next to the code, which is gitignored; pass
--out to choose another folder. Only the newest three archives are kept; older
ones are deleted. When the source is a git checkout, a short GIT_HISTORY.txt
(branch, HEAD, recent commits and working-tree state) is added at the archive
root.

    python scripts/tools/make_code_zip.py [--out DIR]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = PROJECT_ROOT / "archives"

# Keep the newest few archives; older ones are regenerable.
KEEP_ARCHIVES = 3

# Folder names that are never source: generated dictionary data, older zips and
# bytecode. Without "archives" here, every new zip would contain the previous
# ones and grow without limit.
SKIP_DIR_NAMES = {"data", "archives", "__pycache__"}

# Files inside a skipped folder that an archive still keeps. The search log is
# a few KB of the last searches and their SQL timings, which is exactly what
# explains a slow search; the databases beside it are gigabytes.
KEPT_FILES = (Path("data") / "search.log",)

# Folders inside a skipped folder that are kept whole. data/dummy holds the
# small stand-in databases (scripts/tools/build_test_dbs.py), so an archive can run
# the optimisation tests without the real data.
KEPT_DIRS = (Path("data") / "dummy-test-databases",)


def iter_source_files(root: Path):
    """Yield every file to archive, relative to *root*, in sorted order.

    os.walk is used instead of Path.rglob so the skipped folders are pruned
    before their contents are read, which keeps a 1.4 GB data/ folder cheap.
    """
    for dir_path, dir_names, file_names in os.walk(root):
        # Prune in place: os.walk then never descends into these folders.
        relative = Path(dir_path).relative_to(root)
        if relative == Path("deploy") and "run" in dir_names:
            dir_names.remove("run")
        dir_names[:] = sorted(
            name
            for name in dir_names
            if not name.startswith(".") and name not in SKIP_DIR_NAMES
        )
        for name in sorted(file_names):
            yield (Path(dir_path) / name).relative_to(root)


def _git_history(root: Path) -> str | None:
    """Short git log text for the archive, or None outside a git checkout."""

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )

    try:
        branch = run("rev-parse", "--abbrev-ref", "HEAD")
        if branch.returncode != 0:
            return None
        head = run("rev-parse", "--short", "HEAD")
        log = run("log", "--oneline", "--decorate", "-n", "30")
        status = run("status", "--short")
    except (OSError, subprocess.SubprocessError):
        return None

    lines = [
        "# Yabai Search git history",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}",
        f"Branch: {branch.stdout.strip()}",
        f"HEAD: {head.stdout.strip()}",
        "",
        "Recent commits:",
        log.stdout.rstrip() or "(no commits)",
        "",
        "Working tree:",
        status.stdout.rstrip() or "(clean)",
        "",
    ]
    return "\n".join(lines)


def build_zip(root: Path, output: Path) -> list[str]:
    """Write the archive at *output* and return the stored names, sorted."""
    root = root.resolve()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temporary name first so an interrupted run leaves no half zip
    # behind, then rename it into place in one step.
    partial = output.with_name(output.name + ".part")

    stored: list[str] = []
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        # Source files plus the few kept files that live in skipped folders,
        # de-duplicated and sorted so the archive order is stable.
        names = set(iter_source_files(root))
        names.update(path for path in KEPT_FILES if (root / path).is_file())
        for kept_dir in KEPT_DIRS:
            folder = root / kept_dir
            if folder.is_dir():
                names.update(
                    path.relative_to(root)
                    for path in folder.rglob("*")
                    if path.is_file()
                )
        for relative in sorted(names):
            # Never archive the archive itself, in case --out points into root.
            if (root / relative).resolve() == output:
                continue
            archive.write(root / relative, arcname=relative.as_posix())
            stored.append(relative.as_posix())
        history = _git_history(root)
        if history is not None:
            archive.writestr("GIT_HISTORY.txt", history)
            stored.append("GIT_HISTORY.txt")
    partial.replace(output)
    return sorted(stored)


def prune_archives(output_dir: Path, keep: int = KEEP_ARCHIVES) -> list[Path]:
    """Delete older yabai-search archives, keeping the newest `keep`."""
    if keep <= 0:
        return []
    archives = sorted(
        (path for path in output_dir.glob("yabai-search-*.zip") if path.is_file()),
        key=lambda path: (path.name, path.stat().st_mtime),
        reverse=True,
    )
    removed: list[Path] = []
    for path in archives[keep:]:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            pass
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Zip the Yabai Search source, skipping dot folders and data/."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="folder that receives the zip (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.out.expanduser() / f"yabai-search-{stamp}.zip"
    stored = build_zip(PROJECT_ROOT, output)
    removed = prune_archives(args.out.expanduser())

    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"{output}: {len(stored)} files, {size_mb:.2f} MB")
    if removed:
        print(f"removed {len(removed)} older archive(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
