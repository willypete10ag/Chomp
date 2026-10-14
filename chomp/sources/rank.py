"""Ranking, trimming and grouping of upstream search hits (#97), shared by USDA FoodData
Central and Open Food Facts, so the search_food tool, Add food and New food agree.

Only the order and which hits are shown change here: nutrition values are never touched.
"""

from __future__ import annotations

import re

from chomp import logic, text
from chomp.sources import FoodRecord

TYPE_RANK = {"Foundation": 0, "SR Legacy": 1, "Survey (FNDDS)": 2, "Branded": 3}

# Words that make a food a processed, prepared or different thing than the plain food the
# query names ("chicken breast" -> not "Chicken breast, coated" or "nuggets"). A word the
# query asks for is never held against a food ("breaded chicken" finds breaded chicken).
NOT_PLAIN = {
    text.stem(w)
    for w in text.words(
        # coated, breaded or fried products
        "breaded battered coated crumbed fried nuggets patties dippers fritters tenders"
        " popcorn"
        # dried, concentrated or mixed forms
        " dried dry dehydrated powder powdered malted condensed evaporated mix mixture"
        " concentrate"
        " instant imitation substitute replacer"
        # cured or packaged meats
        " cured smoked deli luncheon loaf roll sausage bacon"
        # dishes and combinations
        " with and sandwich sandwiches wrap burrito pizza salad soup casserole sauce gravy"
        " meal dinner entree"
        # flavored
        " flavored chocolate vanilla strawberry sweetened"
        # another animal's milk, or a baby food
        " sheep goat buffalo camel human infant toddler baby formula"
    )
}

# How a food was prepared, for a tag on New food results so raw and cooked don't blur
BREADED = {"breaded", "battered", "coated", "crumbed"}
COOKED = {
    "cooked", "roasted", "grilled", "baked", "boiled", "broiled", "braised", "stewed",
    "fried", "scrambled", "poached", "steamed", "rotisserie", "sauteed", "simmered",
    "microwaved", "smoked",
}  # fmt: skip


def coverage(query: str, rec: FoodRecord) -> float:
    """Share of query words found in the food's name or brand, singular and plural alike."""
    return text.match_score(query, f"{rec.name} {rec.brand or ''}")


# "with added vitamin D" is fortification and "made with butter" how it was cooked, not
# another food alongside: these don't count against a food
FORTIFIED_OR_MADE_WITH = re.compile(r"\b(?:with added|made with)\b[^,(]*", re.IGNORECASE)


def not_plain(query: str, name: str) -> int:
    """How many words make this something other than the plain food asked for."""
    asked = {text.stem(w) for w in text.words(query)}
    have = {text.stem(w) for w in text.words(FORTIFIED_OR_MADE_WITH.sub(" ", name))}
    return len((have & NOT_PLAIN) - asked)


def brand_named(query: str, rec: FoodRecord) -> bool:
    """The query names this food's brand ("chobani greek yogurt")."""
    if not rec.brand:
        return False
    asked = {text.stem(w) for w in text.words(query)}
    return any(text.stem(w) in asked for w in text.words(rec.brand))


def preparation(name: str) -> str:
    """'Breaded', 'Raw' or 'Cooked' when the name says so, else ''."""
    have = set(text.words(name))
    if have & BREADED:
        return "Breaded"
    if "raw" in have or "uncooked" in have:
        return "Raw"
    if have & COOKED:
        return "Cooked"
    return ""


def rank(query: str, records: list[FoodRecord], limit: int) -> list[FoodRecord]:
    """The best `limit` hits for `query`, best first, with weak matches dropped and
    equivalent listings grouped.

    Order: how many query words the food's name and brand contain; how much of the name's
    lead (before the first comma) is what was asked for, so "milk" finds fluid milk before
    "Crackers, milk"; then the plain food before processed or prepared ones the query
    didn't ask for (breaded, dried, with bacon); then generic foods before Branded ones,
    unless the query names the brand; then Foundation, SR Legacy, Survey, Branded; then the
    source's own order.

    A hit missing query words when others have them all is dropped rather than filling the
    list, and a hit matching none is never shown. Listings of the same product (same
    barcode, or the same name, brand and nutrition) are shown once; distinct preparations,
    fat levels and products stay apart however alike their names are.
    """
    if not records:
        return []
    order = {id(r): i for i, r in enumerate(records)}
    scored = [(coverage(query, r), r) for r in records]
    best = max(s for s, _ in scored)
    if best == 0:
        return []
    kept = [r for s, r in scored if s >= best]
    kept.sort(
        key=lambda r: (
            -text.head_score(query, r.name),
            not_plain(query, r.name),
            int(r.data_type == "Branded" and not brand_named(query, r)),
            TYPE_RANK.get(r.data_type or "", 9 if r.source != "off" else 3),
            order[id(r)],
        )
    )
    return group(kept)[:limit]


def _identity(rec: FoodRecord) -> tuple:
    core = logic.round_nutrients(rec.nutrients)
    return (
        " ".join(text.words(rec.name)),
        " ".join(text.words(rec.brand or "")),
        tuple(core.get(k) for k in logic.CORE),
    )


def group(records: list[FoodRecord]) -> list[FoodRecord]:
    """Each product once, in order: a later listing with the same barcode, or with the same
    name, brand and core nutrition per 100 g (one product in several package sizes), is
    counted on the first (`same_as`) instead of shown."""
    out: list[FoodRecord] = []
    by_gtin: dict[str, FoodRecord] = {}
    by_identity: dict[tuple, FoodRecord] = {}
    for rec in records:
        first = (by_gtin.get(rec.gtin) if rec.gtin else None) or by_identity.get(_identity(rec))
        if first is not None:
            first.same_as += 1
            continue
        if rec.gtin:
            by_gtin[rec.gtin] = rec
        by_identity[_identity(rec)] = rec
        out.append(rec)
    return out
