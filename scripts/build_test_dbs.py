#!/usr/bin/env python3
"""Build tiny stand-in databases for every dictionary.

The real data/ folder is about 1.4 GB. A run that only cares about how queries
are shaped (optimisation work, smoke tests, a laptop without the full data)
does not need that: point the app at small copies instead, where each database
keeps its schema and the first few rows of every table:

    uv run python scripts/build_test_dbs.py
    YABAI_DATA_DIR=data/dummy uv run python scripts/build_search_index.py
    YABAI_DATA_DIR=data/dummy uv run pytest tests/test_search_optimizations.py

Full-text tables are rebuilt from their content tables, so an indexed search
still returns hits. The sources are opened read-only; nothing in data/ changes.
The output folder lives under data/, which is gitignored.

    uv run python scripts/build_test_dbs.py [--rows N] [--source DIR] [--output DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_SOURCE = PROJECT_ROOT / "data"
DEFAULT_OUTPUT = DEFAULT_SOURCE / "dummy-test-databases"

# These databases are derived from the others. Copying them would leave a stale
# signature, so rebuild them from the dummy sources instead (the search index
# with scripts/build_search_index.py, the sense counts below).
DERIVED_DATABASES = {"search_index.sqlite", "sense_counts.sqlite"}

# fts5 keeps its index in these shadow tables. They belong to the virtual table
# and are recreated by it, so they must never be copied by hand.
FTS_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")


def _is_virtual(sql: str) -> bool:
    return sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")


def _has_external_content(sql: str) -> bool:
    """True for fts5 tables that read their rows from another table."""
    return "content=" in sql.replace(" ", "").lower()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# The derived indexes remember which files they were built from, as a SHA-256 of
# each source. A plain copy would look stale next to the copied sources, so the
# dummy copies are re-stamped with the dummy files' hashes. The index rows are
# only the first N source rows, which is what a query-shape run needs.
SIGNATURE_SOURCES = {
    "source_gloss.sqlite": {"cedict_sha256": "cedict.sqlite"},
    "head_grams.sqlite": {"cedict_sha256": "cedict.sqlite"},
    "gram_words.sqlite": {
        "source:moe": "moe.sqlite",
        "source:moe_revised": "moe_revised.sqlite",
        "source:wiktionary": "wiktionary.sqlite",
        "source:zhwiktionary": "zhwiktionary.sqlite",
        "source:jpwiktionary": "jpwiktionary.sqlite",
        "source:freq": "corpus_freq.sqlite",
    },
}


def refresh_signatures(output_dir: Path, generated_dir: Path) -> None:
    """Point the derived indexes' recorded source hashes at the dummy files."""
    digests: dict[str, str] = {}
    for index_name, keys in SIGNATURE_SOURCES.items():
        index_path = generated_dir / index_name
        if not index_path.is_file():
            continue
        connection = sqlite3.connect(index_path)
        try:
            for key, source_name in keys.items():
                source_path = output_dir / source_name
                if not source_path.is_file():
                    continue
                if source_name not in digests:
                    digests[source_name] = hashlib.sha256(
                        source_path.read_bytes()
                    ).hexdigest()
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, digests[source_name]),
                )
            connection.commit()
        except sqlite3.Error as exc:
            # A derived database without a meta table is simply not stamped; the
            # app then falls back to its live path.
            print(f"{index_name}: could not update signatures ({exc})", file=sys.stderr)
        finally:
            connection.close()


def copy_database(source: Path, target: Path, rows: int) -> dict[str, int]:
    """Copy `source` into `target`, keeping the schema and at most `rows` rows.

    Returns the number of rows copied per table. Indexes, triggers and views are
    recreated after the rows, so they describe finished data and the copy does
    not pay for trigger work on every insert.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)

    destination = sqlite3.connect(target)
    try:
        destination.execute("PRAGMA journal_mode=OFF")
        destination.execute("PRAGMA synchronous=OFF")
        destination.execute("ATTACH DATABASE ? AS src", (str(source),))

        objects = destination.execute(
            "SELECT type, name, sql FROM src.sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ).fetchall()

        virtual = {
            name: sql
            for kind, name, sql in objects
            if kind == "table" and _is_virtual(sql)
        }
        shadow = {
            name + suffix for name in virtual for suffix in FTS_SHADOW_SUFFIXES
        }
        ordinary = [
            (name, sql)
            for kind, name, sql in objects
            if kind == "table" and name not in shadow and name not in virtual
        ]

        for _, sql in ordinary:
            destination.execute(sql)

        counts: dict[str, int] = {}
        for name, _ in ordinary:
            destination.execute(
                f"INSERT INTO main.{_quote(name)} SELECT * FROM src.{_quote(name)} LIMIT ?",
                (rows,),
            )
            counts[name] = destination.execute(
                f"SELECT COUNT(*) FROM main.{_quote(name)}"
            ).fetchone()[0]

        for sql in virtual.values():
            destination.execute(sql)

        # A standalone fts5 table stores its own content, so copy its rows. An
        # external-content table is rebuilt from its content table below.
        for name, sql in virtual.items():
            if _has_external_content(sql):
                continue
            destination.execute(
                f"INSERT INTO main.{_quote(name)} SELECT * FROM src.{_quote(name)} LIMIT ?",
                (rows,),
            )
            counts[name] = destination.execute(
                f"SELECT COUNT(*) FROM main.{_quote(name)}"
            ).fetchone()[0]

        for kind, _, sql in objects:
            if kind in {"index", "trigger", "view"}:
                destination.execute(sql)

        for name, sql in virtual.items():
            if _has_external_content(sql):
                destination.execute(
                    f"INSERT INTO main.{_quote(name)}({_quote(name)}) VALUES ('rebuild')"
                )

        destination.commit()
    finally:
        destination.close()

    return counts


def build_all(
    source_dir: Path, output_dir: Path, rows: int
) -> dict[str, dict[str, int]]:
    """Build a dummy copy of every source database; return counts per database."""
    built: dict[str, dict[str, int]] = {}
    generated_source = source_dir / "generated-indexes-optimizations"
    generated_output = output_dir / "generated-indexes-optimizations"
    generated_output.mkdir(parents=True, exist_ok=True)

    for source in sorted(source_dir.glob("*.sqlite")):
        if source.name in DERIVED_DATABASES:
            continue
        if source.stat().st_size == 0:
            print(f"skip {source.name}: empty file", file=sys.stderr)
            continue
        target = output_dir / source.name
        counts = copy_database(source, target, rows)
        built[source.name] = counts
        size_kb = target.stat().st_size / 1024
        print(f"{source.name:28} {sum(counts.values()):6} rows {size_kb:7.0f} KB")

    if generated_source.is_dir():
        for source in sorted(generated_source.glob("*.sqlite")):
            if source.name in DERIVED_DATABASES:
                continue
            if source.stat().st_size == 0:
                print(f"skip {source.name}: empty file", file=sys.stderr)
                continue
            target = generated_output / source.name
            counts = copy_database(source, target, rows)
            built[source.name] = counts
            size_kb = target.stat().st_size / 1024
            print(f"{source.name:28} {sum(counts.values()):6} rows {size_kb:7.0f} KB")

    refresh_signatures(output_dir, generated_output)
    # Build the sense-count table from the dummy CEDICT, so it agrees with the
    # copied rows instead of carrying the real file's counts.
    cedict = output_dir / "cedict.sqlite"
    if cedict.is_file():
        from yabai_search import db as app_db

        counts = app_db.build_sense_counts(
            generated_output / "sense_counts.sqlite", cedict_path=cedict
        )
        built["sense_counts.sqlite"] = {"senses": counts["rows"]}
        print(
            f"{'sense_counts.sqlite':28} {counts['rows']:6} rows"
            f" { (generated_output / 'sense_counts.sqlite').stat().st_size / 1024:7.0f} KB"
        )
    return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build small stand-in databases for test and optimisation runs."
    )
    parser.add_argument(
        "--rows", type=int, default=100, help="rows per table (default: %(default)s)"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="folder with the real databases (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="folder that receives the dummy databases (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    if not args.source.is_dir():
        print(f"no source folder: {args.source}", file=sys.stderr)
        return 1
    built = build_all(args.source, args.output, args.rows)
    if not built:
        print(f"no databases found in {args.source}", file=sys.stderr)
        return 1

    total = sum(path.stat().st_size for path in args.output.rglob("*.sqlite"))
    print(f"{len(built)} databases, {total / 1024 / 1024:.1f} MB in {args.output}")
    print(
        "next: YABAI_DATA_DIR={out} uv run python scripts/build_search_index.py".format(
            out=args.output
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
