"""ASCII punctuation next to Chinese text, normalized once at the entry points.

The parser and the search both normalize the input before matching, so the
same sentence typed with "," or "，" finds the same entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yabai_search import db

DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "cedict.sqlite").exists(), reason="CEDICT database not built"
)


def test_normalize_cjk_punct_only_after_han():
    assert db.normalize_cjk_punct("君子一言, 駟馬難追") == "君子一言，駟馬難追"
    assert db.normalize_cjk_punct("你好! 世界") == "你好！世界"
    assert db.normalize_cjk_punct("甲;乙:丙") == "甲；乙：丙"
    # Latin numbers, URLs and Latin text keep their ASCII punctuation.
    assert db.normalize_cjk_punct("3,000元") == "3,000元"
    assert db.normalize_cjk_punct("https://example.com/a,b") == "https://example.com/a,b"
    # "?" is a search wildcard, so it is deliberately left alone.
    assert db.normalize_cjk_punct("为什么? 好") == "为什么? 好"


def test_annotate_treats_ascii_and_fullwidth_punctuation_alike(client):
    ascii_response = client.post("/api/annotate", json={"text": "君子一言, 駟馬難追"})
    full_response = client.post("/api/annotate", json={"text": "君子一言，驷马难追"})
    assert ascii_response.status_code == 200
    assert full_response.status_code == 200

    ascii_tokens = ascii_response.json()["tokens"]
    full_tokens = full_response.json()["tokens"]
    # Both spellings parse as the single idiom, with a reading.
    assert len(ascii_tokens) == 1
    assert len(full_tokens) == 1
    assert ascii_tokens[0]["text"] == "君子一言，駟馬難追"
    assert full_tokens[0]["text"] == "君子一言，驷马难追"
    assert ascii_tokens[0]["entries"]
    assert full_tokens[0]["entries"]


def test_search_treats_ascii_and_fullwidth_punctuation_alike(search):
    ascii_result = search("君子一言, 駟馬難追")
    full_result = search("君子一言，驷马难追")
    ascii_words = [entry["simplified"] for entry in ascii_result["results"]]
    full_words = [entry["simplified"] for entry in full_result["results"]]
    assert ascii_words == full_words == ["君子一言，驷马难追"]
