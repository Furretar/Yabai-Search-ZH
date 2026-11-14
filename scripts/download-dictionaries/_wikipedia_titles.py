"""Chinese Wikipedia titles with their English and Japanese titles (used by the Wiktionary importers).

    import_zhwiktionary.py   every title as a headword (no definition)
    import_wiktionary.py     the titles that have an English title; that title is the gloss
    import_jpwiktionary.py   the titles that have a Japanese title; that title is the sense

Nothing else from Wikipedia is kept: no text, no categories, no redirect targets. The official SQL dumps
(https://dumps.wikimedia.org/zhwiki/latest/) are read once into data/dl-base-dictionaries/zhwikipedia_titles.sqlite:

    zhwiki-latest-page.sql.gz       page id, namespace and title (the titles; about 290 MB)
    zhwiki-latest-langlinks.sql.gz  the page's titles in other languages (about 290 MB)

They are downloaded when missing. Delete zhwikipedia_titles.sqlite, or run an importer with --force, to read newer
dumps. A title is kept only when every character is CJK (or a middle dot): a title with Latin letters, digits or
spaces ("超时空要塞7 Trash") would outrank real words for Latin and numeric searches. Text is CC BY-SA 4.0.
"""
from __future__ import annotations

import gzip
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://dumps.wikimedia.org/zhwiki/latest/"
DL = ROOT / "data" / "dl-base-dictionaries"
PAGE_DUMP = DL / "zhwiki-latest-page.sql.gz"
LANGLINKS_DUMP = DL / "zhwiki-latest-langlinks.sql.gz"
TITLES_DB = DL / "zhwikipedia_titles.sqlite"

# Only CJK characters and the dots that separate a name ("约翰·列侬", "ジョン・レノン").
TITLE_RE = re.compile(r"[㐀-鿿豈-﫿\U00020000-\U0003134f·・]+")
# (page_id, page_namespace, 'page_title', ...  and  (ll_from, 'll_lang', 'll_title')
PAGE_ROW = re.compile(rb"\((\d+),0,'((?:[^'\\]|\\.)*)',[01],")
LANG_ROW = re.compile(rb"\((\d+),'(en|ja)','((?:[^'\\]|\\.)*)'\)")
ESCAPE = re.compile(rb"\\(.)")


def _text(raw: bytes) -> str:
    """A title as stored in the dump: SQL escapes removed, underscores are spaces."""
    return ESCAPE.sub(rb"\1", raw).decode("utf-8", "replace").replace("_", " ").strip()


def _download(name: str, destination: Path) -> None:
    print(f"downloading {BASE}{name} -> {destination}", file=sys.stderr)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(BASE + name, timeout=120) as response, temp.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    temp.replace(destination)


def _lines(path: Path):
    with gzip.open(path, "rb") as handle:
        for line in handle:
            if line.startswith((b"(", b"INSERT")):
                yield line


def build(force: bool = False) -> Path:
    """Make sure the titles database exists (downloading and reading the dumps when it does not)."""
    if TITLES_DB.exists() and not force:
        return TITLES_DB
    for name, path in (("zhwiki-latest-page.sql.gz", PAGE_DUMP), ("zhwiki-latest-langlinks.sql.gz", LANGLINKS_DUMP)):
        if force or not path.exists():
            _download(name, path)
    started = time.time()
    pages: dict[int, str] = {}
    for line in _lines(PAGE_DUMP):
        for match in PAGE_ROW.finditer(line):
            title = _text(match.group(2))
            if TITLE_RE.fullmatch(title):
                pages[int(match.group(1))] = title
    english: dict[int, str] = {}
    japanese: dict[int, str] = {}
    for line in _lines(LANGLINKS_DUMP):
        for match in LANG_ROW.finditer(line):
            page = int(match.group(1))
            if page in pages:
                (english if match.group(2) == b"en" else japanese)[page] = _text(match.group(3))
    temp = TITLES_DB.with_suffix(".tmp")
    temp.unlink(missing_ok=True)
    connection = sqlite3.connect(temp)
    connection.executescript(
        "CREATE TABLE titles (title TEXT PRIMARY KEY, en TEXT, ja TEXT) WITHOUT ROWID;"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
    )
    connection.executemany(
        "INSERT OR IGNORE INTO titles VALUES (?, ?, ?)",
        ((title, english.get(page) or None, japanese.get(page) or None) for page, title in pages.items()),
    )
    connection.execute("INSERT INTO meta VALUES ('built', ?)", (time.strftime("%Y-%m-%d"),))
    connection.commit()
    connection.close()
    temp.replace(TITLES_DB)
    print(
        f"  Wikipedia: {len(pages):,} titles, {len(english):,} with English, {len(japanese):,} with Japanese "
        f"({time.time() - started:.0f}s)",
        file=sys.stderr,
    )
    return TITLES_DB


def rows(column: str | None = None):
    """(title, translation) for the titles that have a translation in `column` ('en' or 'ja'); every title when None."""
    connection = sqlite3.connect(f"file:{build()}?mode=ro", uri=True)
    try:
        if column is None:
            yield from connection.execute("SELECT title, NULL FROM titles ORDER BY title")
        else:
            assert column in ("en", "ja")
            yield from connection.execute(f"SELECT title, {column} FROM titles WHERE {column} IS NOT NULL ORDER BY title")
    finally:
        connection.close()
