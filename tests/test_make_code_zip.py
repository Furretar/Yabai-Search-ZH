"""Tests for scripts/tools/make_code_zip.py, the source-archive helper.

The tree is built in a temporary folder so the test never touches the real
repository or its 1.4 GB data folder.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from scripts.tools import make_code_zip
from scripts.tools.make_code_zip import build_zip, prune_archives


def _make_tree(root: Path) -> None:
    """Create a small project with both source files and folders to skip."""
    (root / "yabai_search" / "static").mkdir(parents=True)
    (root / "yabai_search" / "main.py").write_text("print('hi')\n")
    (root / "yabai_search" / "static" / "app.js").write_text("let a = 1;\n")
    (root / "README.md").write_text("# readme\n")

    # Each of these must never appear in the archive.
    for folder in (
        ".git",
        ".venv",
        ".pytest_cache",
        "data",
        "archives",
        "yabai_search/__pycache__",
    ):
        path = root / folder
        path.mkdir(parents=True)
        (path / "junk.bin").write_bytes(b"junk")

    # The one file inside data/ that an archive keeps.
    (root / "data" / "search.log").write_text("['q'] 1 results\n", encoding="utf-8")
    # The dummy databases are kept whole, so the archive can run the
    # optimisation tests without the real data.
    (root / "data" / "dummy-test-databases").mkdir()
    (root / "data" / "dummy-test-databases" / "cedict.sqlite").write_bytes(b"dummy db")


def _stored_names(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as archive:
        return sorted(archive.namelist())


def test_archive_holds_source_and_skips_generated_folders(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    zip_path = tmp_path.parent / f"{tmp_path.name}-code.zip"

    stored = build_zip(tmp_path, zip_path)

    expected = [
        "README.md",
        "data/dummy-test-databases/cedict.sqlite",
        "data/search.log",
        "yabai_search/main.py",
        "yabai_search/static/app.js",
    ]
    assert sorted(stored) == expected
    # The zip must contain the same entries the helper reported.
    assert _stored_names(zip_path) == expected
    # Everything else under data/ stays out, and so does bytecode.
    assert "data/junk.bin" not in stored
    assert not any(name.endswith(".pyc") for name in stored)


def test_archive_keeps_relative_paths_and_never_contains_itself(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    zip_path = tmp_path / "archive.zip"

    stored = build_zip(tmp_path, zip_path)

    assert "archive.zip" not in stored
    assert "archive.zip" not in _stored_names(zip_path)
    # Nested files keep their path so extracting restores the tree shape.
    assert "yabai_search/static/app.js" in stored


def test_rebuild_overwrites_previous_archive(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    zip_path = tmp_path.parent / f"{tmp_path.name}-again.zip"
    build_zip(tmp_path, zip_path)

    # A stale entry from the first build must not survive the second one.
    (tmp_path / "removed.py").write_text("pass\n")
    build_zip(tmp_path, zip_path)
    (tmp_path / "removed.py").unlink()
    build_zip(tmp_path, zip_path)

    assert "removed.py" not in _stored_names(zip_path)


def test_archive_includes_short_git_history(tmp_path: Path, monkeypatch) -> None:
    """A git checkout's archive carries a small GIT_HISTORY.txt at the root."""
    _make_tree(tmp_path)
    zip_path = tmp_path.parent / f"{tmp_path.name}-history.zip"
    monkeypatch.setattr(
        make_code_zip,
        "_git_history",
        lambda root: "Branch: master\nHEAD: abc1234\n",
    )

    stored = build_zip(tmp_path, zip_path)

    assert "GIT_HISTORY.txt" in stored
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.read("GIT_HISTORY.txt").decode() == (
            "Branch: master\nHEAD: abc1234\n"
        )


def test_only_the_newest_three_archives_are_kept(tmp_path: Path) -> None:
    """The output folder keeps the newest three yabai-search zips."""
    output = tmp_path / "archives"
    output.mkdir()
    names = [f"yabai-search-2026010{i}-000000.zip" for i in range(1, 6)]
    for name in names:
        (output / name).write_bytes(b"archive")
    unrelated = output / "other.zip"
    unrelated.write_bytes(b"keep me")

    removed = prune_archives(output, keep=3)

    assert {path.name for path in output.glob("yabai-search-*.zip")} == set(
        names[-3:]
    )
    assert unrelated.exists()
    assert {path.name for path in removed} == set(names[:2])


def test_older_archives_are_not_packed_into_new_ones(tmp_path: Path) -> None:
    """archives/ is skipped, so a zip cannot contain earlier zips and grow."""
    _make_tree(tmp_path)
    older = tmp_path / "archives" / "yabai-search-old.zip"
    older.write_bytes(b"old archive")

    newer = tmp_path / "archives" / "yabai-search-new.zip"
    stored = build_zip(tmp_path, newer)

    assert not any(name.startswith("archives/") for name in stored)
    assert "archives/yabai-search-old.zip" not in _stored_names(newer)
