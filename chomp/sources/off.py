"""Open Food Facts client (barcode lookups, name search for #88, and product photos for #74).

OFF data is crowd-sourced, so every OFF food is cached unverified until the user confirms
it with `confirm_food`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from chomp import logic
from chomp.sources import FoodRecord, SourceError, rank
from chomp.sources import gtin as gtins
from chomp.sources.http import TTLCache, get_json

BASE_URL = "https://world.openfoodfacts.org/api/v2/product"
# Full-text search lives on the older endpoint; v2 has no text query. OFF allows about 10
# searches a minute, so results are cached and a refused search isn't retried.
SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
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
        "image_front_url",
        "image_url",
    )
)
IMAGE_MAX = 10 * 1024 * 1024  # a product photo bigger than this isn't worth keeping


def _num(nutriments: Mapping[str, Any], key: str) -> float | None:
    v = nutriments.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Label details (#63) -> OFF nutriment names; sugar alcohols are "polyols" there
DETAIL_KEYS = {
    "sat_fat_g": "saturated-fat",
    "trans_fat_g": "trans-fat",
    "poly_fat_g": "polyunsaturated-fat",
    "mono_fat_g": "monounsaturated-fat",
    "cholesterol_mg": "cholesterol",
    "added_sugar_g": "added-sugars",
    "sugar_alcohol_g": "polyols",
}
# Our vitamin and mineral names -> OFF nutriment names (#53)
MICRO_KEYS = {
    "vitamin_d_mcg": "vitamin-d",
    "calcium_mg": "calcium",
    "iron_mg": "iron",
    "potassium_mg": "potassium",
    "vitamin_a_mcg": "vitamin-a",
    "vitamin_c_mg": "vitamin-c",
    "vitamin_e_mg": "vitamin-e",
    "vitamin_k_mcg": "vitamin-k",
    "thiamin_mg": "vitamin-b1",
    "riboflavin_mg": "vitamin-b2",
    "niacin_mg": "vitamin-pp",
    "vitamin_b6_mg": "vitamin-b6",
    "folate_mcg": "vitamin-b9",
    "vitamin_b12_mcg": "vitamin-b12",
    "phosphorus_mg": "phosphorus",
    "magnesium_mg": "magnesium",
    "zinc_mg": "zinc",
}


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
    for key, theirs in (DETAIL_KEYS | MICRO_KEYS).items():
        grams = _num(nutriments, f"{theirs}_100g")  # OFF stores these in grams too
        out[key] = None if grams is None else logic.convert_amount(key, grams, "g")
    if any(out[k] is None for k in logic.CORE):
        return None
    if any(v is not None and v < 0 for v in out.values()):
        return None
    return out


def to_record(product: Mapping[str, Any], gtin14: str | None) -> FoodRecord | None:
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
        source_id=str(product.get("code") or gtin14),  # a search hit always has a code
        name=name.strip(),
        brand=brand,
        gtin=gtin14,
        nutrients=nutrients,
        serving_g=serving_g,
        serving_desc=(product.get("serving_size") or "").strip() or None,
        flags=["unverified: Open Food Facts data is crowd-sourced; confirm against the label"],
        image_url=image_url(product),
    )


def image_url(product: Mapping[str, Any]) -> str | None:
    """The product's front photo (else its main photo), only when it is served from Open
    Food Facts' own image host over HTTPS: Chomp downloads it, so it never follows a URL
    to anywhere else."""
    for key in ("image_front_url", "image_url"):
        url = product.get(key)
        if isinstance(url, str) and is_off_image(url):
            return url
    return None


def is_off_image(url: str) -> bool:
    try:
        u = httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        return False
    host = u.host or ""
    return (
        u.scheme == "https"
        and u.port in (None, 443)
        and (host == "openfoodfacts.org" or host.endswith(".openfoodfacts.org"))
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

    async def search(self, query: str, limit: int) -> list[FoodRecord]:
        """Products whose name matches `query`, ranked and grouped like USDA's hits
        (`rank.rank`), up to `limit`. OFF matches on more than the name, so products whose
        name and brand have none of the query's words are dropped, as are products without
        usable per-100 g nutrition. Raises SourceError."""
        key = ("search", query.lower(), limit)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = await get_json(
            self._http,
            "Open Food Facts",
            SEARCH_URL,
            params={
                "search_terms": query,
                "search_simple": 1,
                "action": "process",
                "json": 1,
                "page_size": max(limit * 3, 24),
                "fields": FIELDS,
            },
            headers=self._headers,
            attempts=1,
            backoff_s=self._backoff_s,
        )
        records = []
        for product in (data or {}).get("products") or []:
            if not product.get("code"):
                continue
            rec = to_record(product, gtins.lenient(str(product["code"])))
            if rec is not None:
                records.append(rec)
        records = rank.rank(query, records, limit)
        self._cache.put(key, records)
        return records

    async def image_url(self, gtin14: str) -> str | None:
        """The product photo for a barcode, even when OFF's nutrition for it is unusable
        (the food may have come from FoodData Central)."""
        for code in gtins.variants(gtin14):
            product = await self._product(code)
            if product:
                return image_url(product)
        return None

    async def fetch_image(self, url: str) -> bytes:
        """Download a product photo found by image_url. Raises SourceError."""
        if not is_off_image(url):
            raise SourceError("Open Food Facts photo URL is not on its image host")
        try:
            async with self._http.stream("GET", url, headers=self._headers) as r:
                if r.status_code != 200:
                    raise SourceError(f"Open Food Facts photo: HTTP {r.status_code}")
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > IMAGE_MAX:
                        raise SourceError("Open Food Facts photo is too large")
        except httpx.HTTPError as e:
            raise SourceError(f"Open Food Facts photo: {type(e).__name__}") from e
        return bytes(data)
