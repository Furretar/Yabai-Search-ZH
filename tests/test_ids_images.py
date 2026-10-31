"""The IDS headwords that have no Unicode glyph must have a real image."""

from __future__ import annotations

from pathlib import Path

from yabai_search import db
from yabai_search.overrides import IDS_IMAGES

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "yabai_search"


def _image_path(url: str) -> Path:
    assert url.startswith("/static/ids_images/")
    return STATIC / url.removeprefix("/")


def test_every_ids_image_has_a_png_file():
    """A missing or truncated image would leave a dashed-box placeholder."""
    for word, url in IDS_IMAGES.items():
        path = _image_path(url)
        assert path.is_file(), (word, path)
        data = path.read_bytes()
        assert len(data) > 500, (word, path)
        assert data[:8] == b"\x89PNG\r\n\x1a\n", (word, path)


def test_listed_ids_headwords_resolve_to_an_image():
    """The IDS rows from the Wiktionary search results all show their image."""
    listed = {
        "⿰讠旦",
        "⿰贝专",
        "⿰车匡",
        "⿰车卜",
        "⿰阝单",
        "⿰麦方",
        "⿱⺈⿻⿻⿵冂⿰一一⿺乚丨⿻⿱ココ弖",
        "⿱⿻丅⿱冖⿰丶丶令",
        "⿱⿻丅⿱冖⿰丶丶双",
        "⿱⿻丅⿱冖⿰丶丶巴",
        "⿳⿱⿴𦥑⿵冂〒冖⿰金⿷⿼己⿼コ丨⿷匚丨火",
        "⿳宀珎⿺进⿰贝招",
        "⿴⿻弓𠃊⿴囗⿻⿰丿丿丶",
        "⿺⻍⿳⿻山昍⿰月⿱⿲糸言糸⿰长𮪉心",
        "⿺⻍⿳⿻山昍⿰月⿱⿲糸言糸⿰长𮪉心⿺⻍⿳⿻山昍⿰月⿱⿲糸言糸⿰长𮪉心面",
        "⿺辶⿳⿻山昍⿰月⿱亦⿰𲈤长心",
        "⿻屮⿱⿻⿰コ匚一冂",
        "搞⿰车匡",
    }
    for word in listed:
        images = db._ids_images_for(word)
        assert images, word
        for url in images:
            assert _image_path(url).is_file(), (word, url)
