"""Pure English-definition relevance tiers.

The search SQL finds candidate rows; this module decides how strongly one of
those rows matches an English query. It never touches a database and has no
dependencies outside the standard library, so the same code can be tested with
fake entries and used from the search path without a rebuild.

Tier 0 is special: it is a headword match where the query appears as Latin text
inside the headword (``word姐`` for ``word``). The remaining tiers describe the
row's glosses:

0. headword contains the query as Latin text
1. a gloss alternative equals the query after normalization
2. a whole-word match outside parentheses
3. a whole-word match only inside parentheses
4. a substring match
5. a match only in a reference sense (``variant of ...``)
"""

from __future__ import annotations

import re

RELEVANCE_VERSION = 2
from dataclasses import dataclass
from functools import lru_cache

# The prefixes that mark a sense as a pointer to another word (the SQL filters in db.py use them too).
SQL_XREF_PREFIXES = (
    "variant of ",
    "old variant of ",
    "archaic variant of ",
    "erhua variant of ",
    "also written ",
)

# Additional markers that make a gloss a reference rather than a definition.
REFERENCE_PREFIXES = SQL_XREF_PREFIXES + (
    "see ",
    "abbr. for ",
    "cl:",
)

_LEADING_ARTICLES = ("to ", "a ", "an ", "the ")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_LATIN_RE = re.compile(r"[a-z]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_OPEN = "([（【"
_CLOSE = ")]）】"


@dataclass(frozen=True, order=True)
class Features:
    """One row's best relevance features; lower is better."""

    tier: int
    sense_bucket: int
    length_bucket: int


def _strip_parentheticals(text: str) -> str:
    """Remove balanced or unbalanced parenthetical groups.

    An unclosed opening bracket removes the rest of the string; a stray closing
    bracket is ignored. Nested groups are handled with a depth counter.
    """
    out: list[str] = []
    depth = 0
    for char in text:
        if char in _OPEN:
            depth += 1
            continue
        if char in _CLOSE:
            if depth:
                depth -= 1
            continue
        if depth == 0:
            out.append(char)
    return "".join(out)


def _parenthetical_text(text: str) -> str:
    """Return only the text inside parenthetical groups."""
    out: list[str] = []
    depth = 0
    for char in text:
        if char in _OPEN:
            depth += 1
            continue
        if char in _CLOSE:
            if depth:
                depth -= 1
            continue
        if depth:
            out.append(char)
    return "".join(out)


def _collapse(text: str) -> str:
    return " ".join(text.split())


@lru_cache(maxsize=100_000)
def _normalize(text: str, strip_parentheses: bool = True) -> str:
    """Lowercase, strip parentheses, drop one leading article, collapse."""
    base = text.lower()
    base = _strip_parentheticals(base) if strip_parentheses else _parenthetical_text(base)
    base = _collapse(base)
    if not base:
        base = _collapse(text.lower())
    result = base
    for article in _LEADING_ARTICLES:
        if result.startswith(article) and len(result) > len(article):
            result = _collapse(result[len(article) :])
            break
    # Never return an empty string: a query such as "to" must still match.
    return result or base


def normalize_gloss(text: str) -> str:
    """Normalize one gloss alternative for comparison."""
    return _normalize(text)


def split_alternatives(english: str) -> list[tuple[int, str]]:
    """Split a CEDICT ``english`` field into (sense index, alternative)."""
    alternatives: list[tuple[int, str]] = []
    for sense_index, sense in enumerate((english or "").split("/")):
        for alternative in sense.split(";"):
            value = alternative.strip()
            if value:
                alternatives.append((sense_index, value))
    return alternatives


def is_reference_gloss(text: str) -> bool:
    """True when an alternative is a cross-reference, not a definition."""
    lowered = _collapse((text or "").lower())
    return any(lowered.startswith(prefix) for prefix in REFERENCE_PREFIXES)


@lru_cache(maxsize=4096)
def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(text))


def _whole_word(text: str, query: str) -> bool:
    """True when the query's tokens appear contiguously as whole words."""
    wanted = _tokens(query)
    if not wanted:
        return False
    have = _tokens(text)
    if len(wanted) > len(have):
        return False
    first = wanted[0]
    for start in range(len(have) - len(wanted) + 1):
        if have[start] == first and have[start : start + len(wanted)] == wanted:
            return True
    return False


def _substring(text: str, query: str) -> bool:
    if not query:
        return False
    # A one-letter query would match almost everything; only whole words count.
    if len(query) == 1 and len(_tokens(query)) == 1:
        return False
    return query in text


def _length_bucket(text: str) -> int:
    count = len(_tokens(text))
    if count <= 3:
        return 0
    if count <= 10:
        return 1
    return 2


def _sense_bucket(sense_index: int, reference: bool) -> int:
    if reference:
        return 2
    if sense_index <= 0:
        return 0
    if sense_index <= 2:
        return 1
    return 2


def match_features(english: str, query: str) -> Features | None:
    """Return the best gloss features for one row, or None when it does not match."""
    normalized_query = normalize_gloss(query)
    if not normalized_query:
        return None
    best: Features | None = None
    for sense_index, alternative in split_alternatives(english):
        reference = is_reference_gloss(alternative)
        outside = normalize_gloss(alternative)
        inside = normalize_gloss(_parenthetical_text(alternative))
        if reference:
            if not (
                outside == normalized_query
                or _whole_word(outside, normalized_query)
                or _substring(outside, normalized_query)
            ):
                continue
            tier = 6
        elif outside == normalized_query:
            tier = 2
        elif _whole_word(outside, normalized_query):
            tier = 3
        elif _whole_word(inside, normalized_query):
            tier = 4
        elif _substring(outside, normalized_query) or _substring(
            inside, normalized_query
        ):
            tier = 5
        else:
            continue
        features = Features(
            tier,
            _sense_bucket(sense_index, reference),
            _length_bucket(outside or inside),
        )
        if best is None or features < best:
            best = features
    return best


def is_latin_query(query: str) -> bool:
    """True for a Latin query with no Han characters (the T0 candidate case)."""
    normalized_query = normalize_gloss(query)
    return bool(_LATIN_RE.search(normalized_query)) and not _CJK_RE.search(
        normalized_query
    )


def headword_features(headword: str, query: str) -> Features | None:
    """Tier 0 when the query appears as Latin text inside a Chinese headword."""
    normalized_query = normalize_gloss(query)
    if not normalized_query or not _LATIN_RE.search(normalized_query):
        return None
    if _CJK_RE.search(normalized_query):
        return None
    if not _CJK_RE.search(headword or ""):
        return None
    if not _LATIN_RE.search(headword or ""):
        return None
    pattern = (
        r"(?<![a-z0-9])"
        + re.escape(normalized_query)
        + r"(?![a-z0-9])"
    )
    if re.search(pattern, headword.lower()):
        return Features(1, 0, 0)
    return None


def match_entry(entry: dict, query: str) -> Features | None:
    """Best features for an entry: headword tier first, then its glosses."""
    for headword in (entry.get("simplified"), entry.get("traditional")):
        features = headword_features(headword or "", query)
        if features is not None:
            return features
    return match_features(entry.get("english") or "", query)


def relevance_key(features: Features, entry: dict) -> tuple:
    """Deterministic sort key for matched English-definition rows."""
    freq = entry.get("freq")
    return (
        features.tier,
        features.sense_bucket,
        features.length_bucket,
        freq is None,
        freq["rank"] if freq else 0,
        len(entry.get("simplified") or ""),
        entry.get("simplified") or "",
        entry.get("pinyin") or "",
    )


def exact_whole_gloss(english: str, query: str) -> bool:
    """True when the whole gloss is exactly the query, with no qualifiers.

    Unlike `normalize_gloss`, this does not strip leading articles or
    parenthetical notes, so "to water" does not count as an exact match for
    "water". Used to give a direct translation (法国 for "France") a small
    edge over a polysemous word (法) that only lists the query as one sense.
    """
    return " ".join((english or "").split()).lower() == " ".join(
        (query or "").split()
    ).lower()


def rank_entries(entries: list[dict], query: str, boost_exact: bool | None = None) -> list[dict]:
    """Sort matched rows; rows with no match keep their relative order at the end.

    A capitalized query (a proper noun such as "France") gives a small edge to
    entries whose entire gloss is exactly the query, so the canonical
    translation (法国) sorts above a polysemous word (法) that merely lists it
    as one sense. Lowercase queries keep the frequency order, which the judged
    set expects for common words.
    """
    matched: list[tuple[Features, dict]] = []
    unmatched: list[dict] = []
    for entry in entries:
        features = match_entry(entry, query)
        if features is None:
            unmatched.append(entry)
        else:
            matched.append((features, entry))
    if boost_exact is None:
        boost_exact = bool(query.strip()) and query.strip()[0].isupper() and query.strip()[0].isalpha()
    has_exact = boost_exact and any(
        features.tier == 2
        and exact_whole_gloss(entry.get("english") or "", query)
        for features, entry in matched
    )

    def sort_key(item: tuple[Features, dict]) -> tuple:
        features, entry = item
        freq = entry.get("freq")
        whole = (
            0
            if (
                has_exact
                and features.tier == 2
                and exact_whole_gloss(entry.get("english") or "", query)
            )
            else 1
        )
        return (
            features.tier,
            features.sense_bucket,
            features.length_bucket,
            whole,
            freq is None,
            freq["rank"] if freq else 0,
            len(entry.get("simplified") or ""),
            entry.get("simplified") or "",
            entry.get("pinyin") or "",
        )

    matched.sort(key=sort_key)
    return [entry for _features, entry in matched] + unmatched
