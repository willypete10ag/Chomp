"""M2: barcode chain (cache -> FDC Branded -> Open Food Facts -> ask for label), label saves,
and confirm_food. Upstreams are replayed from fixtures."""

from __future__ import annotations

import pytest
from conftest import fixture_json
from mcp import Client

from chomp import store
from chomp.photos import Photos
from chomp.server import build_server
from chomp.sources import off as offmod
from chomp.sources.off import OFFClient

SEARCH = "/fdc/v1/foods/search"
EGG_WHITES_UPC = "041220576463"
EGG_WHITES_GTIN = "00041220576463"
NUTELLA = "3017624010701"
UNKNOWN = "00000000000017"


@pytest.fixture
def off(upstream):
    return OFFClient("Chomp-tests/0", http=upstream.client(), backoff_s=0)


@pytest.fixture
def server(settings, conn, fdc, off):
    # No product photos here: these tests count the lookup chain's requests (see test_photos)
    return build_server(settings, fdc=fdc, off=off, photos=Photos(settings.photos_path))


@pytest.fixture
def chain(upstream):
    """FDC knows the egg whites; OFF knows Nutella; nobody knows UNKNOWN."""
    upstream.add(
        SEARCH, fixture_json("fdc/search_upc_egg_whites.json"), match={"query": EGG_WHITES_UPC}
    )
    upstream.add(SEARCH, {"foods": []})
    upstream.add("/fdc/v1/food/2091234", fixture_json("fdc/food_2091234_egg_whites.json"))
    upstream.add(
        f"/api/v2/product/{NUTELLA}", fixture_json("off/product_3017624010701_nutella.json")
    )
    return upstream


async def call(client, tool, **args):
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    return result.structured_content


async def call_error(client, tool, **args) -> str:
    result = await client.call_tool(tool, args)
    assert result.is_error
    return result.content[0].text


def paths(upstream):
    return [r.url.path for r in upstream.requests]


@pytest.mark.anyio
async def test_known_upc_found_in_fdc_and_cached(server, conn, chain):
    async with Client(server) as client:
        out = await call(client, "lookup_barcode", barcode=EGG_WHITES_UPC)
        assert out["found"] and out["found_in"] == "fdc"
        food = out["food"]
        assert food["name"] == "100% Liquid Egg Whites"
        assert food["per_100g"]["protein_g"] == 10.9
        assert food["serving_g"] == 46
        assert out["warnings"] == []
        # newest publication of the GTIN wins, and its detail record is fetched
        assert "/fdc/v1/food/2091234" in paths(chain)
        assert not any("openfoodfacts" in str(r.url) for r in chain.requests)

        n = len(chain.requests)
        again = await call(client, "lookup_barcode", barcode="0" + EGG_WHITES_UPC)
    assert again["found_in"] == "saved"
    assert again["food"]["food_id"] == food["food_id"]
    assert len(chain.requests) == n  # served from the DB
    row = conn.execute("SELECT gtin, source, verified, saved FROM foods").fetchone()
    assert tuple(row) == (EGG_WHITES_GTIN, "fdc", 0, 1)
    # A scanned product counts as saved in name search, even unlogged
    assert [r["id"] for r in store.search_foods(conn, "liquid egg whites", 5)] == [food["food_id"]]


@pytest.mark.anyio
async def test_off_only_upc_saved_unverified(server, conn, chain):
    async with Client(server) as client:
        out = await call(client, "lookup_barcode", barcode=NUTELLA)
    assert out["found_in"] == "off"
    food = out["food"]
    assert food["source"] == "off" and food["verified"] is False
    assert food["brand"] == "Nutella"
    assert food["per_100g"]["kcal"] == 539
    assert food["per_100g"]["sodium_mg"] == 43  # 0.0428 g
    assert food["serving_g"] == 15
    assert "unverified" in out["warnings"][0]
    # FDC was asked first
    assert paths(chain)[0] == SEARCH


@pytest.mark.anyio
async def test_unknown_upc_asks_for_label(server, conn, chain):
    async with Client(server) as client:
        out = await call(client, "lookup_barcode", barcode="000000000017")
    assert out == {
        "barcode": UNKNOWN,
        "found": False,
        "found_in": None,
        "food": None,
        "warnings": [],
        "next_step": out["next_step"],
    }
    assert "save_food_from_label" in out["next_step"]
    assert conn.execute("SELECT count(*) FROM foods").fetchone()[0] == 0


@pytest.mark.anyio
async def test_invalid_barcode_is_a_clear_error(server):
    async with Client(server) as client:
        msg = await call_error(client, "lookup_barcode", barcode="041220576464")
    assert "check digit" in msg and "re-read" in msg


@pytest.mark.anyio
async def test_label_mismatch_prefers_label_and_flags(server, upstream):
    upstream.add(
        SEARCH,
        {"foods": [{"fdcId": 2200000, "gtinUpc": "722252100900", "dataType": "Branded"}]},
    )
    upstream.add("/fdc/v1/food/2200000", fixture_json("fdc/food_2200000_bar_label_mismatch.json"))
    async with Client(server) as client:
        out = await call(client, "lookup_barcode", barcode="722252100900")
    per = out["food"]["per_100g"]
    assert per["kcal"] == 393  # 110 kcal per 28 g serving, not FDC's 300
    assert per["protein_g"] == 25.0
    assert "label_preferred" in out["warnings"][0]


@pytest.mark.anyio
async def test_sources_down_says_so(server, upstream):
    upstream.add(SEARCH, {}, status=503)
    upstream.add(f"/api/v2/product/{NUTELLA}", {}, status=503)
    upstream.add("/api/v2/product/03017624010701", {}, status=503)
    async with Client(server) as client:
        out = await call(client, "lookup_barcode", barcode=NUTELLA)
    assert not out["found"]
    assert len(out["warnings"]) == 2
    assert "could not be reached" in out["next_step"]


@pytest.mark.anyio
async def test_fdc_down_still_tries_off(server, upstream):
    upstream.add(SEARCH, {}, status=503)
    upstream.add(
        f"/api/v2/product/{NUTELLA}", fixture_json("off/product_3017624010701_nutella.json")
    )
    async with Client(server) as client:
        out = await call(client, "lookup_barcode", barcode=NUTELLA)
    assert out["found_in"] == "off"
    assert any("FoodData Central" in w for w in out["warnings"])


@pytest.mark.anyio
async def test_save_food_from_label_converts_and_claims_barcode(server, conn, chain):
    async with Client(server) as client:
        first = await call(client, "lookup_barcode", barcode=NUTELLA)
        saved = await call(
            client,
            "save_food_from_label",
            name="Nutella",
            brand="Ferrero",
            upc=NUTELLA,
            serving_g=37,
            serving_desc="2 tbsp (37 g)",
            kcal=200,
            protein_g=2,
            carbs_g=22,
            fat_g=12,
            sugar_g=21,
            sodium_mg=15,
        )
        food = saved["food"]
        assert food["source"] == "label" and food["verified"] is True
        assert food["per_100g"]["kcal"] == 541  # 200 / 37 * 100
        assert food["per_100g"]["sodium_mg"] == 41
        assert food["serving_desc"] == "2 tbsp (37 g)"
        assert saved["barcode_moved_from_food_id"] == first["food"]["food_id"]

        scan = await call(client, "lookup_barcode", barcode=NUTELLA)
    assert scan["found_in"] == "saved"
    assert scan["food"]["food_id"] == food["food_id"]


@pytest.mark.anyio
async def test_save_food_from_label_validates(server):
    async with Client(server) as client:
        bad = await client.call_tool(
            "save_food_from_label",
            {"name": "x", "serving_g": 0, "kcal": 1, "protein_g": 1, "carbs_g": 1, "fat_g": 1},
        )
        assert bad.is_error
        bad = await client.call_tool(
            "save_food_from_label",
            {"name": "x", "serving_g": 10, "kcal": -1, "protein_g": 1, "carbs_g": 1, "fat_g": 1},
        )
        assert bad.is_error
        msg = await call_error(
            client,
            "save_food_from_label",
            name="x",
            serving_g=10,
            kcal=1,
            protein_g=1,
            carbs_g=1,
            fat_g=1,
            upc="123",
        )
    assert "8, 12, 13 or 14 digits" in msg


@pytest.mark.anyio
async def test_confirm_food_marks_verified_and_corrects(server, conn, chain):
    async with Client(server) as client:
        found = await call(client, "lookup_barcode", barcode=NUTELLA)
        food_id = found["food"]["food_id"]
        logged = await call(
            client, "log_entries", items=[{"food_id": food_id, "grams": 15}], eaten_at="now"
        )
        out = await call(client, "confirm_food", food_id=food_id)
        assert out["food"]["verified"] is True and out["changed"] == []

        out = await call(
            client, "confirm_food", food_id=food_id, kcal=200, protein_g=2, per_serving_g=37
        )
        assert out["changed"] == ["kcal", "protein_g"]
        assert out["food"]["per_100g"]["kcal"] == 541
        assert out["food"]["per_100g"]["fat_g"] == 30.9  # untouched

        day = await call(client, "get_day")
    # past entries keep their snapshot
    assert day["totals"]["kcal"] == logged["meal"]["totals"]["kcal"] == 81


@pytest.mark.anyio
async def test_confirm_food_errors(server):
    async with Client(server) as client:
        assert "no food with id 42" in await call_error(client, "confirm_food", food_id=42)


@pytest.mark.anyio
async def test_save_food_without_a_label(server):
    async with Client(server) as client:
        saved = await call(
            client,
            "save_food",
            name=" Chicken burrito bowl ",
            brand="Chipotle",
            serving_g=500,
            serving_desc="1 bowl",
            kcal=650,
            protein_g=50,
            carbs_g=60,
            fat_g=22,
            sodium_mg=1800,
        )
        assert saved["source"] == "manual" and saved["verified"] is False
        assert saved["name"] == "Chicken burrito bowl" and saved["serving_g"] == 500
        assert saved["per_100g"]["kcal"] == 130 and saved["per_100g"]["sodium_mg"] == 360

        approved = await call(client, "confirm_food", food_id=saved["food_id"])
        assert approved["food"]["verified"] is True and approved["changed"] == []
        undone = await call(client, "confirm_food", food_id=saved["food_id"], confirmed=False)
        assert undone["food"]["verified"] is False

        home = await call(
            client,
            "save_food",
            name="Mom's chili",
            serving_g=250,
            kcal=300,
            protein_g=20,
            carbs_g=25,
            fat_g=12,
            confirmed=True,
        )
        assert home["verified"] is True and home["brand"] is None
        found = await call(client, "search_food", query="chili")
    assert found["results"][0]["food_id"] == home["food_id"]


@pytest.mark.anyio
async def test_save_food_needs_a_name(server):
    async with Client(server) as client:
        blank = await call_error(
            client,
            "save_food",
            name="  ",
            serving_g=300,
            kcal=900,
            protein_g=30,
            carbs_g=120,
            fat_g=30,
        )
    assert "needs a name" in blank


def test_off_parse_kj_only_and_missing_core():
    n = offmod.parse_nutrients(
        {
            "energy-kj_100g": 418.4,
            "proteins_100g": "1",
            "carbohydrates_100g": 2,
            "fat_100g": 3,
        }
    )
    assert n["kcal"] == pytest.approx(100) and n["protein_g"] == 1 and n["sodium_mg"] is None
    assert offmod.parse_nutrients({"energy-kcal_100g": 1, "proteins_100g": 1}) is None


def test_off_serving_in_ml_is_the_serving():
    # OFF's _100g values are per 100 ml for drinks, so a ml serving is the serving's size
    nutriments = {"energy-kcal_100g": 54, "proteins_100g": 8.5, "carbohydrates_100g": 3.4,
                  "fat_100g": 1.3}  # fmt: skip
    product = {"code": "1", "product_name": "Shake", "nutriments": nutriments,
               "serving_size": "1 bottle (354 ml)", "serving_quantity": 354}  # fmt: skip
    rec = offmod.to_record(product | {"serving_quantity_unit": "ml"}, None)
    assert rec.serving_g == 354 and rec.serving_desc == "1 bottle (354 ml)"
    assert offmod.to_record(product | {"serving_quantity_unit": "oz"}, None).serving_g is None


@pytest.mark.anyio
async def test_fdc_tries_upc_a_first_then_other_forms(upstream, fdc):
    upstream.add(SEARCH, {"foods": []})
    assert await fdc.lookup_gtin(EGG_WHITES_GTIN) is None
    assert [r.url.params["query"] for r in upstream.requests] == [
        "041220576463",
        "0041220576463",
        "00041220576463",
    ]


@pytest.mark.anyio
async def test_off_sends_user_agent_and_tries_variants(upstream, off):
    assert await off.lookup_gtin("00041220576463") is None
    codes = [r.url.path.rsplit("/", 1)[1] for r in upstream.requests]
    assert codes == ["0041220576463", "041220576463", "00041220576463"]
    assert upstream.requests[0].headers["User-Agent"] == "Chomp-tests/0"
