"""Server-side port of the result merge in static/app.js.

The browser merges rows that share a headword (了/瞭, 和/咊) after the server
pages them. This module performs the same merge so the server can eventually
send one row per headword.

It deliberately stops short of ``mergeIdenticalRows`` from app.js: that folds
two traditional spellings that say the same thing, and it depends on which
dictionaries the user currently has visible (a browser-only setting). The
browser keeps doing that part.

The module is not wired into the request path yet. Phase 3c will call it behind
a flag once its output is compared with the browser's.
"""

from __future__ import annotations

from typing import Any


def _entry_reading(entry: dict, taiwan: bool) -> str:
    """The reading shown for an entry (Taiwan reading when Taiwan is on)."""
    if taiwan and entry.get("pinyin_taiwan"):
        return entry["pinyin_taiwan"]
    return entry.get("pinyin") or ""


def _clone_definition(definition: dict, entry: dict, taiwan: bool) -> dict:
    """Copy one dictionary block and tag it with its row's headword/reading.

    Merged rows use ``traditional`` and ``reading`` to keep each spelling's
    definitions in its own group.
    """
    out = dict(definition)
    out["traditional"] = entry.get("traditional") or ""
    out["reading"] = _entry_reading(entry, taiwan)
    out["groups"] = [
        {
            "pinyin": group.get("pinyin") or "",
            "senses": list(group.get("senses") or []),
        }
        for group in definition.get("groups") or []
    ]
    out["defs"] = list(definition.get("defs") or [])
    out["classifiers"] = [dict(cls) for cls in definition.get("classifiers") or []]
    return out


def _clone_entry(entry: dict, taiwan: bool) -> dict:
    """A deep-enough copy, so merging never mutates the API data."""
    out = dict(entry)
    out["sources"] = [dict(source) for source in entry.get("sources") or []]
    out["forms"] = [dict(form) for form in entry.get("forms") or []]
    out["mergedForms"] = [dict(form) for form in entry.get("mergedForms") or []]
    out["definitions"] = [
        _clone_definition(definition, entry, taiwan)
        for definition in entry.get("definitions") or []
    ]
    return out


def _merge_entry(target: dict, addition: dict, taiwan: bool) -> None:
    """Add one entry's definitions, chips and forms to another (app.js)."""
    traditional = addition.get("traditional") or ""
    if (
        not addition.get("main")
        and traditional
        and traditional != target.get("traditional")
        and not any(
            (form.get("traditional") if isinstance(form, dict) else form)
            == traditional
            for form in target.get("mergedForms") or []
        )
        and not any(
            form.get("traditional") == traditional
            for form in target.get("forms") or []
        )
    ):
        target["mergedForms"].append(
            {"traditional": traditional, "pinyin": addition.get("pinyin") or ""}
        )

    for source in addition.get("definitions") or []:
        block = next(
            (
                existing
                for existing in target.get("definitions") or []
                if existing.get("anchor") == source.get("anchor")
                and existing.get("traditional") == traditional
            ),
            None,
        )
        if block is None:
            target["definitions"].append(_clone_definition(source, addition, taiwan))
            continue
        for group in source.get("groups") or []:
            existing = next(
                (
                    item
                    for item in block.get("groups") or []
                    if item.get("pinyin") == group.get("pinyin")
                ),
                None,
            )
            if existing is None:
                block.setdefault("groups", []).append(
                    {
                        "pinyin": group.get("pinyin") or "",
                        "senses": list(group.get("senses") or []),
                    }
                )
                continue
            for sense in group.get("senses") or []:
                if sense not in existing["senses"]:
                    existing["senses"].append(sense)
        for text in source.get("defs") or []:
            if text not in block.setdefault("defs", []):
                block["defs"].append(text)
        for classifier in source.get("classifiers") or []:
            if classifier not in block.setdefault("classifiers", []):
                block["classifiers"].append(dict(classifier))

    for chip in addition.get("sources") or []:
        if not any(
            existing.get("anchor") == chip.get("anchor")
            for existing in target.get("sources") or []
        ):
            target["sources"].append(dict(chip))
    for form in addition.get("forms") or []:
        if form.get("traditional") == target.get("traditional"):
            continue
        if not any(
            existing.get("traditional") == form.get("traditional")
            for existing in target.get("forms") or []
        ):
            target["forms"].append(dict(form))
    if not target.get("freq") and addition.get("freq"):
        target["freq"] = addition["freq"]


def group_key(entry: dict, script: str) -> Any:
    """The key app.js's mergeEntries() uses for one display script."""
    if script == "traditional":
        return (entry.get("simplified") or "", entry.get("traditional") or "")
    return entry.get("simplified") or entry.get("traditional") or ""


def page_layouts(
    rows: list[dict], taiwan: bool = False
) -> tuple[list[dict], list[dict], bool]:
    """Both display layouts for one page, plus whether they are identical.

    Rows are grouped by simplified form upstream. If no group on the page has
    more than one traditional spelling, the simplified and traditional layouts
    are the same page, so the browser does not need a second copy.
    """
    simplified = merge_entries(rows, "simplified", taiwan)
    traditional = merge_entries(rows, "traditional", taiwan)
    variants: dict[str, set[str]] = {}
    for row in rows:
        key = row.get("simplified") or row.get("traditional") or ""
        variants.setdefault(key, set()).add(row.get("traditional") or "")
    alt_same = all(len(values) <= 1 for values in variants.values())
    return simplified, traditional, alt_same


def merge_entries(
    entries: list[dict], script: str = "simplified", taiwan: bool = False
) -> list[dict]:
    """Collapse rows that display as the same headword, like mergeEntries().

    The merged row is built on the entry with the best ``reading_score`` (set by
    the server: real senses plus preferred readings), so 只 shows zhǐ "only"
    rather than the classifier zhī. Ties keep the server's order.
    """
    groups: dict[Any, list[dict]] = {}
    for entry in entries:
        groups.setdefault(group_key(entry, script), []).append(entry)
    rows: list[dict] = []
    for group in groups.values():
        base = group[0]
        for entry in group:
            if (entry.get("reading_score") or 0) > (base.get("reading_score") or 0):
                base = entry
        merged = _clone_entry(base, taiwan)
        # The group is in the server's ranked order, so its first row is the
        # best-ranked one. Keep that category/tier even when the row chosen as
        # the base (by reading_score) is a different member.
        if group[0].get("category") is not None:
            merged["category"] = group[0]["category"]
        if group[0].get("tier") is not None:
            merged["tier"] = group[0]["tier"]
        # Every dictionary whose definition matched a definition search, for
        # any spelling in the group.
        anchors = sorted(
            {a for entry in group for a in entry.get("match_anchors") or []}
        )
        if anchors:
            merged["match_anchors"] = anchors
        for entry in group:
            if entry is base:
                continue
            if _is_variant_of(entry, merged) and _entry_reading(entry, taiwan) == _entry_reading(merged, taiwan):
                # A variant spelling (剛纔 "variant of 剛才") says what the row says: only its spelling is listed.
                _add_traditional_form(merged, entry)
            else:
                _merge_entry(merged, entry, taiwan)
        rows.append(merged)
    return fold_variant_rows(rows, taiwan) if script == "traditional" else rows


def _is_variant_of(row: dict, target: dict) -> bool:
    """True when one of the row's definitions calls it a variant of the target's spelling ("variant of 剛才|...")."""
    head = target.get("traditional") or ""
    if not head or head == row.get("traditional"):
        return False
    marker = f"variant of {head}"
    for block in row.get("definitions") or []:
        texts = list(block.get("defs") or [])
        for group in block.get("groups") or []:
            texts.extend(group.get("senses") or [])
        for text in texts:
            text = str(text)
            at = text.find(marker)
            # The spelling must end there ("剛才|", "剛才[", "剛才)"), not run on into a longer word.
            after = text[at + len(marker) : at + len(marker) + 1]
            if at >= 0 and not ("\u3400" <= after <= "\u9fff"):
                return True
    return False


def _add_traditional_form(target: dict, row: dict) -> None:
    """List the row's spelling under the target's "Traditional forms" (its definitions are not added)."""
    traditional = row.get("traditional") or ""
    forms = target.setdefault("mergedForms", [])
    if traditional and not any(
        (form.get("traditional") if isinstance(form, dict) else form) == traditional for form in forms
    ):
        forms.append({"traditional": traditional, "pinyin": row.get("pinyin") or ""})


def fold_variant_rows(rows: list[dict], taiwan: bool = False) -> list[dict]:
    """Traditional layout: fold a spelling that a definition calls a variant of another row's spelling.

    剛纔 ("just now (variant of 剛才|刚才[gang1 cai2])") goes into the 剛才 row when both simplify alike (刚才) and
    have the same reading. Only the spelling is kept, as a link under "Traditional forms": a variant says what the
    main row says, so its definitions are not repeated. app.js foldVariantRows() does the same for pages it merges
    itself.
    """
    out = list(rows)
    for row in rows:
        if not any(item is row for item in out):
            continue
        target = next(
            (
                other
                for other in out
                if other is not row
                and (other.get("simplified") or "") == (row.get("simplified") or "")
                and _entry_reading(other, taiwan) == _entry_reading(row, taiwan)
                and _is_variant_of(row, other)
                and not _is_variant_of(other, row)
            ),
            None,
        )
        if target is None:
            continue
        _add_traditional_form(target, row)
        out = [item for item in out if item is not row]
    return out
