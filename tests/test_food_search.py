"""#88: search USDA and Open Food Facts by name from New food; USDA Survey (FNDDS) foods in
FDC search; Add food searches USDA on its own when few of your foods match."""

from __future__ import annotations

import pytest
from conftest import fixture_json
from starlette.testclient import TestClient

from chomp import store
from chomp.server import build_server
from chomp.sources.off import OFFClient

SEARCH = "/fdc/v1/foods/search"
OFF_SEARCH = "/cgi/search.pl"
GENERIC = {"dataType": "Foundation,SR Legacy,Survey (FNDDS)"}
BRANDED = {"dataType": "Branded"}


@pytest.fixture
def off(upstream):
    return OFFClient("Chomp-tests/0", http=upstream.client(), backoff_s=0)


@pytest.fixture
def app(settings, conn, fdc, off):
    server = build_server(settings, fdc=fdc, off=off)
    with TestClient(server.streamable_http_app(), follow_redirects=False) as client:
        yield client


@pytest.fixture
def egg_whites(upstream):
    upstream.add(SEARCH, fixture_json("fdc/search_egg_white_generic.json"), match=GENERIC)
    upstream.add(SEARCH, fixture_json("fdc/search_egg_white_branded.json"), match=BRANDED)


def off_searches(upstream) -> list:
    return [r for r in upstream.requests if r.url.path == OFF_SEARCH]


@pytest.mark.anyio
async def test_off_search_ranks_and_drops_unusable_products(upstream, off):
    upstream.add(OFF_SEARCH, fixture_json("off/search_hummus.json"))
    recs = await off.search("classic hummus", 5)
    # no nutrition and no code are dropped; the full word match leads
    assert [r.name for r in recs] == ["Classic hummus", "Hummus chips"]
    top = recs[0]
    assert (top.source, top.source_id, top.gtin) == ("off", "5000112637922", "05000112637922")
    assert top.brand == "Sabra" and top.serving_g == 30
    assert top.nutrients["kcal"] == 267 and top.nutrients["sodium_mg"] == pytest.approx(433)
    assert recs[1].gtin is None  # a code that isn't a valid barcode still caches
    await off.search("classic hummus", 5)
    assert len(off_searches(upstream)) == 1  # cached
    params = off_searches(upstream)[0].url.params
    assert params["search_terms"] == "classic hummus" and params["json"] == "1"


@pytest.mark.anyio
async def test_fdc_search_includes_survey_foods(upstream, fdc, egg_whites):
    await fdc.search_ranked("egg white", limit=5)
    types = {r.url.params["dataType"] for r in upstream.requests}
    assert types == {GENERIC["dataType"], BRANDED["dataType"]}


def test_new_food_page_has_search_card_first(app):
    text = app.get("/app/foods/new").text
    assert "Search for a food" in text
    assert text.index("Search for a food") < text.index("Scan a barcode")
    assert "USDA FoodData Central and Open Food Facts" in text


def test_search_shows_both_sources(app, upstream, egg_whites):
    upstream.add(OFF_SEARCH, {"products": []})
    text = app.get("/app/foods/new", params={"q": "egg white"}).text
    assert "USDA FoodData Central" in text and "Egg, white, raw, fresh" in text
    assert "No matches in Open Food Facts." in text
    assert 'action="/app/foods/found"' in text
    # results stay unchosen until tapped: they aren't in Foods yet
    assert "Egg, white, raw, fresh" not in app.get("/app/foods").text


def test_tapping_a_usda_result_confirms_it_and_opens_it(app, conn, upstream, egg_whites):
    upstream.add(OFF_SEARCH, {"products": []})
    app.get("/app/foods/new", params={"q": "egg white"})
    row = conn.execute("SELECT id FROM foods WHERE source_id = '172183'").fetchone()
    back = "/app/foods/new?q=egg+white"
    r = app.post("/app/foods/found", data={"food_id": row["id"], "back": back})
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/app/foods/{row['id']}?")
    food = store.get_food(conn, row["id"])
    assert food["verified"] == 1
    page = app.get(r.headers["location"]).text
    assert "Added Egg, white, raw, fresh to your foods." in page
    assert "Egg, white, raw, fresh" in app.get("/app/foods").text


def test_tapping_an_off_result_keeps_it_unconfirmed(app, conn, upstream):
    upstream.add(OFF_SEARCH, fixture_json("off/search_hummus.json"))
    upstream.add(SEARCH, {"foods": []})
    text = app.get("/app/foods/new", params={"q": "hummus"}).text
    assert "Classic hummus" in text and "Crowd-sourced" in text
    assert "No matches in USDA FoodData Central." in text
    # shown but not tapped: OFF hits are a lookup cache, like USDA's
    assert "Classic hummus" not in app.get("/app/foods", params={"q": "hummus"}).text
    row = conn.execute("SELECT id FROM foods WHERE source_id = '5000112637922'").fetchone()
    r = app.post("/app/foods/found", data={"food_id": row["id"]})
    assert r.status_code == 303
    food = store.get_food(conn, row["id"])
    assert food["verified"] == 0
    foods = app.get("/app/foods").text
    assert "Classic hummus" in foods and "Unconfirmed" in foods
    assert "Hummus chips" not in foods


def test_one_source_down_still_shows_the_other(app, upstream, egg_whites):
    upstream.add(OFF_SEARCH, {"error": "busy"}, status=503)
    text = app.get("/app/foods/new", params={"q": "egg white"}).text
    assert "Egg, white, raw, fresh" in text
    assert "Open Food Facts isn&#x27;t reachable right now" in text


def test_found_refuses_a_missing_food(app):
    assert app.post("/app/foods/found", data={"food_id": "999999"}).status_code == 404


def test_add_food_searches_usda_on_its_own_when_few_match(app, upstream, egg_whites):
    text = app.get("/app/add", params={"q": "egg white"}).text
    assert "Egg, white, raw, fresh" in text and "Search USDA for" not in text


def test_add_food_keeps_the_button_when_enough_match(app, conn, upstream, egg_whites):
    for i in range(3):
        store.save_label_food(
            conn,
            name=f"Egg white bites {i}",
            brand=None,
            gtin=None,
            per_100g={
                "kcal": 50,
                "protein_g": 11,
                "carbs_g": 1,
                "fat_g": 0,
                "fiber_g": 0,
                "sugar_g": 0,
                "sodium_mg": 160,
            },
            serving_g=100,
            serving_desc=None,
        )
    text = app.get("/app/add", params={"q": "egg white"}).text
    assert "Search USDA for" in text
    assert not [r for r in upstream.requests if r.url.path == SEARCH]
