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
