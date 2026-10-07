"""Word matching shared by saved-food search and FDC result ranking."""

from __future__ import annotations

import re
import unicodedata

_WORD = re.compile(r"[^\W_]+")


def words(text: str) -> list[str]:
    """Lowercase words with accents removed, like the FTS index ("Café" -> "cafe")."""
    folded = unicodedata.normalize("NFKD", text.lower())
    return _WORD.findall("".join(c for c in folded if not unicodedata.combining(c)))


def stem(word: str) -> str:
    """Fold common English plurals so "eggs" matches "egg" and "berries" matches "berry",
    without letting an arbitrary prefix match ("egg" must not match "eggplant")."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith(("ches", "shes", "sses", "xes", "oes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def match_score(query: str, text: str) -> float:
    """Share of query words found in text, singular and plural alike."""
    wanted = [stem(w) for w in words(query)]
    if not wanted:
        return 0.0
    have = {stem(w) for w in words(text)}
    return sum(w in have for w in wanted) / len(wanted)


def head_score(query: str, name: str) -> float:
    """Share of the name's lead words (before the first comma) that the query asked for.

    FDC generic names lead with what the food is ("Milk, reduced fat, fluid"), so this ranks
    plain milk above "Crackers, milk" or "Milk dessert, frozen" when the user says "milk".
    """
    lead = [stem(w) for w in words(name.split(",", 1)[0])]
    if not lead:
        return 0.0
    wanted = {stem(w) for w in words(query)}
    return sum(w in wanted for w in lead) / len(lead)


def most_specific(query: str, names: list[str]) -> str | None:
    """The one name the query clearly means: the only candidate, the one with exactly the
    query's words, or the longest one wholly inside the query ("my usual breakfast" means
    "usual breakfast", not "breakfast"). None when it stays ambiguous."""
    if len(names) == 1:
        return names[0]
    wanted = {stem(w) for w in words(query)}
    exact = [n for n in names if {stem(w) for w in words(n)} == wanted]
    if len(exact) == 1:
        return exact[0]
    inside = [n for n in names if match_score(n, query) == 1]
    if not inside:
        return None
    longest = max(len(words(n)) for n in inside)
    top = [n for n in inside if len(words(n)) == longest]
    return top[0] if len(top) == 1 else None
