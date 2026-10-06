"""Open Food Facts client (barcode lookups only).

OFF data is crowd-sourced, so every OFF food is cached unverified until the user confirms
it with `confirm_food`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from chomp import logic
from chomp.sources import FoodRecord
from chomp.sources import gtin as gtins
from chomp.sources.http import TTLCache, get_json

BASE_URL = "https://world.openfoodfacts.org/api/v2/product"
FIELDS = ",".join(
    (
        "code",
        "product_name",
        "product_name_en",
        "generic_name",
        "brands",
        "nutriments",
        "serving_size",
        "serving_quantity",
        "serving_quantity_unit",
    )
)


def _num(nutriments: Mapping[str, Any], key: str) -> float | None:
    v = nutriments.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_nutrients(nutriments: Mapping[str, Any]) -> logic.Nutrients | None:
    """Per-100 g nutrients from OFF `nutriments`, or None if a core value is missing."""
    kcal = _num(nutriments, "energy-kcal_100g")
    if kcal is None and (kj := _num(nutriments, "energy-kj_100g")) is not None:
        kcal = logic.kj_to_kcal(kj)
    sodium_g = _num(nutriments, "sodium_100g")  # OFF stores sodium in grams
    out: logic.Nutrients = {
        "kcal": kcal,
        "protein_g": _num(nutriments, "proteins_100g"),
        "carbs_g": _num(nutriments, "carbohydrates_100g"),
        "fat_g": _num(nutriments, "fat_100g"),
        "fiber_g": _num(nutriments, "fiber_100g"),
        "sugar_g": _num(nutriments, "sugars_100g"),
        "sodium_mg": None if sodium_g is None else logic.g_to_mg(sodium_g),
    }
    if any(out[k] is None for k in logic.CORE):
        return None
    if any(v is not None and v < 0 for v in out.values()):
        return None
    return out


def to_record(product: Mapping[str, Any], gtin14: str) -> FoodRecord | None:
    nutrients = parse_nutrients(product.get("nutriments") or {})
    name = (
        product.get("product_name_en") or product.get("product_name") or product.get("generic_name")
    )
    if nutrients is None or not name:
        return None
    brand = (product.get("brands") or "").split(",")[0].strip() or None
    serving_g = None
    unit = (product.get("serving_quantity_unit") or "g").lower()
    if unit == "g" and (q := _num(product, "serving_quantity")) and q > 0:
        serving_g = q
    return FoodRecord(
        source="off",
        source_id=str(product.get("code") or gtin14),
        name=name.strip(),
        brand=brand,
        gtin=gtin14,
        nutrients=nutrients,
        serving_g=serving_g,
        serving_desc=(product.get("serving_size") or "").strip() or None,
        flags=["unverified: Open Food Facts data is crowd-sourced; confirm against the label"],
    )


class OFFClient:
    def __init__(
        self,
        user_agent: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
        cache_ttl_s: float = 24 * 3600,
        backoff_s: float = 0.5,
    ) -> None:
        self._http = http or httpx.AsyncClient(timeout=timeout_s)
        self._headers = {"User-Agent": user_agent}
        self._cache = TTLCache(cache_ttl_s)
        self._backoff_s = backoff_s

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _product(self, code: str) -> dict | None:
        key = (code,)
        cached = self._cache.get(key)
        if cached is not None:
            return cached or None
        data = await get_json(
            self._http,
            "Open Food Facts",
            f"{BASE_URL}/{code}",
            params={"fields": FIELDS},
            headers=self._headers,
            backoff_s=self._backoff_s,
            not_found_ok=True,
        )
        product = data.get("product") if data and data.get("status") == 1 else None
        self._cache.put(key, product or {})
        return product

    async def lookup_gtin(self, gtin14: str) -> FoodRecord | None:
        for code in gtins.variants(gtin14):
            product = await self._product(code)
            if product:
                return to_record(product, gtin14)
        return None
