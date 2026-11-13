"""Tests for the exact-word view at /word/<word>."""

from __future__ import annotations

from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "dictionary-databases" / "cedict.sqlite").exists(), reason="CEDICT database not built"
)


def _exact(client, word: str) -> dict:
    response = client.get("/api/search", params={"q": word, "exact": 1, "limit": 25})
    assert response.status_code == 200, response.text
    return response.json()


def test_word_page_opens_the_normal_search_view(client):
    """The details URL renders the search page with the exact word embedded."""
    response = client.get("/word/档案")
    assert response.status_code == 200
    html = response.text
    assert 'id="search-form"' in html
    assert 'class="menu-toggle"' in html
    assert 'class="site-subtitle"' in html
    assert 'class="site-byline"' in html
    assert "window.__wordQuery" in html
    assert "window.__exactSearch = true" in html


def test_exact_api_has_frequency_hsk_and_revised_moe(client):
    """The exact API returns the same decorated row a search would show."""
    data = _exact(client, "档案")
    entry = next(
        entry
        for entry in data["results"]
        if entry["simplified"] == "档案" or entry["traditional"] == "檔案"
    )
    assert entry["freq"]["rank"] > 0
    assert entry["hsk"]
    labels = {definition["label"] for definition in entry["definitions"]}
    assert "重編國語辭典修訂本" in labels


def test_exact_api_works_for_source_only_words(client):
    """A word that only a secondary dictionary has still gets an exact view."""
    response = client.get("/word/劣性")
    assert response.status_code == 200
    data = _exact(client, "劣性")
    assert any(entry["simplified"] == "劣性" for entry in data["results"])
    labels = {
        definition["label"]
        for entry in data["results"]
        for definition in entry["definitions"]
    }
    assert "重編國語辭典修訂本" in labels


def test_word_page_accepts_traditional_forms(client):
    """The route and exact API both accept the traditional spelling."""
    response = client.get("/word/檔案")
    assert response.status_code == 200
    assert "window.__wordQuery" in response.text
    assert _exact(client, "檔案")["results"]


def test_word_page_returns_404_for_unknown_words(client):
    """An unknown non-Chinese word has no exact view."""
    assert client.get("/word/qqzzxxnotaword").status_code == 404


WIKTIONARY = DATA / "dictionary-databases" / "wiktionary.sqlite"


@pytest.mark.skipif(not WIKTIONARY.exists(), reason="Wiktionary database not built")
def test_word_page_opens_reading_only_source_word(client):
    """A source reading without a gloss still gets a details page."""
    word = "嘴馋逼浪，早晚上当"
    response = client.get("/word/" + word)
    assert response.status_code == 200
    assert "window.__exactSearch = true" in response.text


def test_exact_api_links_male_voice_audio(client):
    """Readings expose the CC0 male MP3 used by the speaker button."""
    data = _exact(client, "律师")
    audios = [entry.get("audio") for entry in data["results"]]
    assert any(
        audio and audio.endswith("/male/lv4shi1.mp3") for audio in audios
    )


def test_exact_api_has_dictionary_anchors_for_toggles(client):
    """Definitions keep their anchors so the result chips can toggle them."""
    data = _exact(client, "档案")
    anchors = {
        definition["anchor"]
        for entry in data["results"]
        for definition in entry["definitions"]
    }
    assert "cedict" in anchors
    assert len(anchors) > 1


def test_conversion_literally_is_displayed(client):
    """The (lit.) normalization rule is applied to CEDICT definitions."""
    data = _exact(client, "一人得道，鸡犬升天")
    text = " ".join(
        sense
        for entry in data["results"]
        for definition in entry["definitions"]
        if definition["anchor"] == "cedict"
        for sense in definition.get("defs", [])
    )
    assert "(literal)" in text
    assert "(lit.)" not in text


GLYPH = DATA / "dictionary-databases" / "moe-taiwan" / "moe_glyphs" / "8ff0._104_0.gif"


def test_exact_cross_reference_shows_the_target_definition(client):
    """A CEDICT "see X" entry opens X's definition instead of an empty page.

    没sei了 [méi sei le] is stored only as "see 沒誰了|没谁了[mei2 sei2 le5]".
    The exact view used to keep the cross-reference row, so it showed the
    CC-CEDICT chip with no definitions.
    """
    data = _exact(client, "没sei了")
    assert data["results"], "the cross-reference should resolve"
    entry = data["results"][0]
    assert entry["simplified"] == "没谁了"
    senses = [
        sense
        for definition in entry["definitions"]
        for sense in definition.get("defs") or []
    ]
    assert any("nobody can beat that" in sense for sense in senses)


@pytest.mark.skipif(
    not (DATA / "dictionary-databases" / "wiktionary.sqlite").exists(), reason="Wiktionary database not built"
)
def test_every_dialect_reading_is_returned(client):
    """A word with no Mandarin reading keeps every dialect it lists.

    松毛須 has a Xiang and an Eastern Min reading on Wiktionary. The entry used
    to keep only the first one; the rest are shown on the lines below.
    """
    data = _exact(client, "松毛须")
    entry = next(
        (row for row in data["results"] if "松毛" in row["simplified"]),
        None,
    )
    assert entry is not None
    labels = {reading["label"] for reading in entry.get("readings") or []}
    assert {"Xiang", "Eastern Min"} <= labels
    # The browser uses this flag to skip the Zhuyin conversion.
    assert entry["pinyin_no_tone"] is True
    assert entry["pinyin_label"] == "Xiang"


@pytest.mark.skipif(
    not (DATA / "dictionary-databases" / "wiktionary.sqlite").exists(), reason="Wiktionary database not built"
)
def test_split_phrase_reading_covers_both_halves(client):
    """A 歇后语 keeps the reading for the half after the dash, too.

    Wiktionary stores one Mandarin reading per half. Showing only the first
    left 全不知滋味 without a reading.
    """
    data = _exact(client, "猪八戒吃人参果——全不知滋味")
    entry = next(
        (row for row in data["results"] if "猪八戒" in row["simplified"]),
        None,
    )
    assert entry is not None
    assert entry["pinyin"].split() == [
        "Zhū", "Bā", "jiè", "chī", "rén", "shēn", "guǒ",
        "quán", "bù", "zhī", "zī", "wèi",
    ]


@pytest.mark.skipif(not GLYPH.exists(), reason="MOE glyph images not extracted")
def test_moe_glyph_route_serves_a_picture(client):
    """The glyph route serves an extracted MOE glyph image."""
    response = client.get("/moe-glyphs/8ff0._104_0.gif")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/gif"
    assert response.content


@pytest.mark.skipif(not GLYPH.exists(), reason="MOE glyph images not extracted")
def test_moe_glyph_route_rejects_traversal_and_missing_files(client):
    """Only plain picture names are served; traversal and missing files 404."""
    assert client.get("/moe-glyphs/../cedict.sqlite").status_code == 404
    assert client.get("/moe-glyphs/does-not-exist.gif").status_code == 404
