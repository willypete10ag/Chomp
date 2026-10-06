"""USDA FoodData Central client.

Search hits and food details are normalized to `FoodRecord`s with per-100 g nutrients.
Raw responses are cached in memory for a day to stay well under the ~1,000 requests/hour
key limit. The key goes in the X-Api-Key header so it never appears in a logged URL.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

import httpx

from chomp import logic
from chomp.sources import FoodRecord, SourceError
from chomp.sources import gtin as gtins
from chomp.sources.http import TTLCache, get_json

log = logging.getLogger("chomp.sources.fdc")

BASE_URL = "https://api.nal.usda.gov/fdc/v1"
GENERIC_TYPES = ("Foundation", "SR Legacy")
BRANDED = "Branded"
TYPE_RANK = {"Foundation": 0, "SR Legacy": 1, "Survey (FNDDS)": 2, "Branded": 3}

# FDC nutrient ids, best first. Foundation foods often lack 1008 (Energy) and 1005
# (carbohydrate by difference) and report the Atwater / by-summation values instead.
NUTRIENT_IDS: dict[str, tuple[int, ...]] = {
    "kcal": (1008, 2047, 2048),
    "protein_g": (1003,),
    "carbs_g": (1005, 1050),
    "fat_g": (1004, 1085),
    "fiber_g": (1079,),
    "sugar_g": (2000, 1063),
    "sodium_mg": (1093,),
}
ENERGY_KJ = 1062

# labelNutrients keys (per serving) -> our nutrient names
LABEL_KEYS = {
    "calories": "kcal",
    "protein": "protein_g",
    "carbohydrates": "carbs_g",
    "fat": "fat_g",
    "fiber": "fiber_g",
    "sugars": "sugar_g",
    "sodium": "sodium_mg",
}
GRAM_UNITS = {"g", "grm", "gram", "grams"}


def _amounts(food_nutrients: Iterable[Mapping[str, Any]]) -> dict[int, tuple[float, str]]:
    """nutrient id -> (amount, unit) for both search-hit and detail formats."""
    out: dict[int, tuple[float, str]] = {}
    for n in food_nutrients or ():
        if "nutrient" in n:  # /food/{id} detail format
            meta = n["nutrient"]
            nid, unit, value = meta.get("id"), meta.get("unitName"), n.get("amount")
        else:  # /foods/search format
            nid, unit, value = n.get("nutrientId"), n.get("unitName"), n.get("value")
        if nid is None or value is None:
            continue
        out.setdefault(int(nid), (float(value), (unit or "").lower()))
    return out


def parse_nutrients(food_nutrients: Iterable[Mapping[str, Any]]) -> logic.Nutrients | None:
    """Per-100 g nutrients from an FDC foodNutrients list, or None if a core value is missing."""
    amounts = _amounts(food_nutrients)
    out: logic.Nutrients = {}
    for key, ids in NUTRIENT_IDS.items():
        value = None
        for nid in ids:
            if nid in amounts:
                v, unit = amounts[nid]
                if key == "kcal" and unit == "kj":
                    v = logic.kj_to_kcal(v)
                value = v
                break
        if key == "kcal" and value is None and ENERGY_KJ in amounts:
            value = logic.kj_to_kcal(amounts[ENERGY_KJ][0])
        out[key] = value
    if any(out[k] is None for k in logic.CORE):
        return None
    return out


def _name(food: Mapping[str, Any]) -> str:
    name = (food.get("description") or "").strip()
    # Branded descriptions are often all caps
    return name.title() if name.isupper() else name


def _serving(food: Mapping[str, Any]) -> tuple[float | None, str | None]:
    size, unit = food.get("servingSize"), (food.get("servingSizeUnit") or "").strip()
    household = (food.get("householdServingFullText") or "").strip()
    serving_g = float(size) if size and unit.lower() in GRAM_UNITS else None
    parts = []
    if household:
        parts.append(household)
    if size:
        amount = f"{float(size):g} {'g' if serving_g else unit.lower()}"
        parts.append(f"({amount})" if household else amount)
    return serving_g, " ".join(parts) or None


def to_record(food: Mapping[str, Any]) -> FoodRecord | None:
    """Normalize one FDC food (search hit or detail). None if core nutrients are missing."""
    nutrients = parse_nutrients(food.get("foodNutrients") or [])
    if nutrients is None or food.get("fdcId") is None:
        return None
    serving_g, serving_desc = _serving(food)
    brand = (food.get("brandName") or food.get("brandOwner") or "").strip() or None
    return FoodRecord(
        source="fdc",
        source_id=str(food["fdcId"]),
        name=_name(food),
        brand=brand,
        gtin=gtins.lenient(food.get("gtinUpc")),
        data_type=food.get("dataType"),
        nutrients=nutrients,
        serving_g=serving_g,
        serving_desc=serving_desc,
    )


def apply_label_check(record: FoodRecord, detail: Mapping[str, Any]) -> FoodRecord:
    """Cross-check per-100 g values against the per-serving label (Branded details only).

    When a core nutrient differs by more than 10%, the label-derived values win and the
    record is flagged so Claude can tell the user.
    """
    label = detail.get("labelNutrients") or {}
    if not label or not record.serving_g:
        return record
    per_serving = {
        ours: float(label[theirs]["value"])
        for theirs, ours in LABEL_KEYS.items()
        if isinstance(label.get(theirs), Mapping) and label[theirs].get("value") is not None
    }
    if any(k not in per_serving for k in logic.CORE):
        return record
    try:
        from_label = logic.per_serving_to_per_100g(per_serving, record.serving_g)
    except ValueError:
        return record
    mismatched = logic.label_mismatch(record.nutrients, from_label)
    if mismatched:
        record.nutrients = {
            k: from_label[k] if from_label.get(k) is not None else record.nutrients.get(k)
            for k in logic.NUTRIENTS
        }
        record.flags.append(
            "label_preferred: FDC per-100 g values differed from the package label by more "
            f"than 10% ({', '.join(mismatched)}), so the label values were used"
        )
    return record


class FDCClient:
    def __init__(
        self,
        api_key: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
        cache_ttl_s: float = 24 * 3600,
        backoff_s: float = 0.5,
    ) -> None:
        self.enabled = bool(api_key)
        self._http = http or httpx.AsyncClient(timeout=timeout_s)
        self._headers = {"X-Api-Key": api_key} if api_key else {}
        self._cache = TTLCache(cache_ttl_s)
        self._backoff_s = backoff_s

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None, **kw) -> Any:
        if not self.enabled:
            raise SourceError("FoodData Central is not configured (FDC_API_KEY is empty)")
        key = (path, tuple(sorted((params or {}).items())))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = await get_json(
            self._http,
            "FoodData Central",
            f"{BASE_URL}{path}",
            params=params,
            headers=self._headers,
            backoff_s=self._backoff_s,
            **kw,
        )
        if data is not None:
            self._cache.put(key, data)
        return data

    async def search(
        self, query: str, data_types: Iterable[str], page_size: int = 10
    ) -> list[FoodRecord]:
        """Search FDC; returns usable hits in FDC's relevance order."""
        data = await self._get(
            "/foods/search",
            {"query": query, "dataType": ",".join(data_types), "pageSize": page_size},
        )
        records = [to_record(f) for f in (data or {}).get("foods", [])]
        return [r for r in records if r is not None]

    async def search_ranked(self, query: str, limit: int) -> list[FoodRecord]:
        """Generic foods (Foundation, then SR Legacy) first, Branded after, up to `limit`."""
        generic = await self.search(query, GENERIC_TYPES, page_size=limit)
        generic.sort(key=lambda r: TYPE_RANK.get(r.data_type or "", 9))  # stable within type
        out = generic[:limit]
        if len(out) < limit:
            out += (await self.search(query, (BRANDED,), page_size=limit))[: limit - len(out)]
        return out

    async def food(self, fdc_id: str) -> dict | None:
        return await self._get(f"/food/{fdc_id}", not_found_ok=True)

    async def lookup_gtin(self, gtin14: str) -> FoodRecord | None:
        """Branded food for a barcode, with the label cross-check applied."""
        hits: list[dict] = []
        for code in gtins.variants(gtin14)[:2]:
            data = await self._get(
                "/foods/search", {"query": code, "dataType": BRANDED, "pageSize": 10}
            )
            foods = (data or {}).get("foods", [])
            hits = [f for f in foods if gtins.same_product(gtin14, f.get("gtinUpc"))]
            if hits:
                break
        if not hits:
            return None
        # Several records can share a GTIN; the newest publication is the current label.
        hits.sort(key=lambda f: (f.get("publishedDate") or "", f.get("fdcId") or 0), reverse=True)
        detail = await self.food(str(hits[0]["fdcId"])) or hits[0]
        record = to_record(detail) or to_record(hits[0])
        if record is None:
            return None
        record.gtin = gtin14
        return apply_label_check(record, detail)
