"""#97: New food's search relevance (USDA and Open Food Facts) and Foods' search as you type.

The candidate lists follow what the searches in the issue showed: the names are USDA's and
Open Food Facts' own style, and each test pins the bad candidates and the food types that
should lead.
"""

from __future__ import annotations

import pytest
from conftest import fixture_json
from starlette.testclient import TestClient

from chomp import store
from chomp.server import build_server
from chomp.sources import FoodRecord, rank
from chomp.sources.off import OFFClient

SEARCH = "/fdc/v1/foods/search"
OFF_SEARCH = "/cgi/search.pl"
GENERIC = {"dataType": "Foundation,SR Legacy,Survey (FNDDS)"}
BRANDED = {"dataType": "Branded"}


def _hit(fdc_id, name, data_type, brand=None, kcal=100, gtin=None):
    food = {
        "fdcId": fdc_id,
        "description": name,
        "dataType": data_type,
        "foodNutrients": [
            {"nutrientId": 1008, "unitName": "KCAL", "value": kcal},
            {"nutrientId": 1003, "unitName": "G", "value": 10},
            {"nutrientId": 1005, "unitName": "G", "value": 1},
            {"nutrientId": 1004, "unitName": "G", "value": 3},
        ],
    }
    if brand:
        food["brandName"] = brand
    if gtin:
        food["gtinUpc"] = gtin
    return food


def _product(code, name, brand=None, kcal=100):
    return {
        "code": code,
        "product_name": name,
        "brands": brand or "",
        "nutriments": {
            "energy-kcal_100g": kcal,
            "proteins_100g": 10,
            "carbohydrates_100g": 1,
            "fat_100g": 3,
        },
    }


@pytest.fixture
def off(upstream):
    return OFFClient("Chomp-tests/0", http=upstream.client(), backoff_s=0)


def usda(upstream, generic, branded):
    upstream.add(SEARCH, {"foods": generic}, match=GENERIC)
    upstream.add(SEARCH, {"foods": branded}, match=BRANDED)


# --- the six searches from the issue ---


@pytest.mark.anyio
async def test_chicken_breast_leads_with_plain_generic_breast(upstream, fdc):
    usda(
        upstream,
        [
            _hit(1, "Chicken breast, baked, coated, skin / coating eaten", "Survey (FNDDS)"),
            _hit(
                2,
                "Chicken, broilers or fryers, breast, meat and skin, cooked, fried, batter",
                "SR Legacy",
            ),
            _hit(3, "Chicken, broilers or fryers, breast, meat only, cooked, roasted", "SR Legacy"),
            _hit(4, "Chicken, breast, boneless, skinless, raw", "Foundation"),
        ],
        [
            _hit(11, "CHICKEN BREAST", "Branded", brand="GIANT EAGLE"),
            _hit(12, "CHICKEN BREAST", "Branded", brand="TYSON"),
            _hit(13, "CHICKEN BREAST DINO NUGGETS, CHICKEN BREAST", "Branded", brand="PERDUE"),
            _hit(14, "BREADED CHICKEN BREAST PATTIES", "Branded", brand="YUMMY"),
        ],
    )
    recs = await fdc.search_ranked("chicken breast", limit=8)
    ids = [r.source_id for r in recs]
    # plain generic breast first, raw and cooked both offered and tagged
    assert ids[:2] == ["4", "3"]
    assert [rank.preparation(r.name) for r in recs[:2]] == ["Raw", "Cooked"]
    # coated, battered, nuggets and patties after every plain breast
    plain = {"4", "3", "11", "12"}
    assert set(ids[:4]) == plain
    assert rank.preparation(recs[ids.index("1")].name) == "Breaded"


@pytest.mark.anyio
async def test_cooked_chicken_breast_puts_meat_only_first(upstream, fdc):
    # FDC's order put plain roasted meat-only breast fifth
    usda(
        upstream,
        [
            _hit(
                1,
                "Chicken, broilers or fryers, breast, meat and skin, cooked, roasted",
                "SR Legacy",
            ),
            _hit(
                2,
                "Chicken, broilers or fryers, breast, meat and skin, cooked, fried, flour",
                "SR Legacy",
            ),
            _hit(
                3,
                "Chicken, broilers or fryers, breast, meat and skin, cooked, fried, batter",
                "SR Legacy",
            ),
            _hit(4, "Chicken breast, baked, coated, skin / coating eaten", "Survey (FNDDS)"),
            _hit(5, "Chicken, broilers or fryers, breast, meat only, cooked, roasted", "SR Legacy"),
        ],
        [],
    )
    recs = await fdc.search_ranked("cooked chicken breast", limit=8)
    assert [r.source_id for r in recs][:2] == ["5", "1"]
    assert {r.source_id for r in recs[-2:]} == {"2", "3"}  # fried and battered come last


@pytest.mark.anyio
async def test_milk_finds_fluid_milk_before_powders_and_other_dairy(upstream, fdc, off):
    usda(
        upstream,
        [
            _hit(1, "Milk, buttermilk, dried", "SR Legacy"),
            _hit(2, "Milk, sheep, fluid", "SR Legacy"),
            _hit(
                3,
                "Milk, chocolate, fluid, commercial, whole, with added vitamin A and vitamin D",
                "SR Legacy",
            ),
            _hit(4, "Milk, canned, condensed, sweetened", "SR Legacy"),
            _hit(5, "Milk, whole, 3.25% milkfat, with added vitamin D", "Foundation"),
            _hit(
                6,
                "Milk, reduced fat, fluid, 2% milkfat, with added vitamin A and vitamin D",
                "SR Legacy",
            ),
            _hit(7, "Malted drink mix, natural, powder", "SR Legacy"),
        ],
        [],
    )
    recs = await fdc.search_ranked("milk", limit=8)
    ids = [r.source_id for r in recs]
    assert ids[:2] == ["5", "6"]
    assert ids.index("1") > 1 and ids.index("4") > 1 and ids.index("2") > 1
    upstream.add(
        OFF_SEARCH,
        {
            "products": [
                _product("1", "Cream cheese", "Philadelphia"),
                _product("2", "Mature cheddar", "Cathedral City"),
                _product("3", "Milk chocolate", "Cadbury"),
                _product("4", "Whole milk", "Arla"),
                _product("5", "Semi skimmed milk", "Tesco"),
            ]
        },
    )
    names = [r.name for r in await off.search("milk", 8)]
    # cheeses match only on ingredients, not their names: dropped
    assert names == ["Whole milk", "Semi skimmed milk", "Milk chocolate"]


@pytest.mark.anyio
async def test_scrambled_eggs_keeps_the_good_match_and_drops_weak_ones(upstream, fdc, off):
    usda(
        upstream,
        [
            _hit(1, "Egg, whole, cooked, scrambled", "SR Legacy"),
            _hit(2, "Egg, scrambled, frozen mixture", "SR Legacy"),
            _hit(3, "Egg Mix, USDA Commodity", "SR Legacy"),
            _hit(4, "Egg, scrambled, patty", "Survey (FNDDS)"),
            _hit(5, "Egg, scrambled, made with butter", "Survey (FNDDS)"),
            _hit(6, "Egg, scrambled, made with margarine", "Survey (FNDDS)"),
        ],
        [],
    )
    ids = [r.source_id for r in await fdc.search_ranked("scrambled eggs", limit=8)]
    assert ids == ["1", "5", "6", "2", "4"]  # egg mix isn't scrambled eggs at all
    upstream.add(
        OFF_SEARCH,
        {
            "products": [
                _product("1", "Scrambled eggs with sausage, potatoes and cheese", "Jimmy Dean"),
                _product("2", "Scrambled eggs with bacon", "Blue Mountain"),
                _product("3", "Scrambled eggs", "Eggs-Pert"),
                _product("4", "Sausage breakfast bowl", "Jimmy Dean"),
            ]
        },
    )
    names = [r.name for r in await off.search("scrambled eggs", 8)]
    assert names[0] == "Scrambled eggs"
    assert "Sausage breakfast bowl" not in names


@pytest.mark.anyio
async def test_plain_greek_yogurt_groups_listings_and_keeps_fat_levels(upstream, fdc):
    usda(
        upstream,
        [
            _hit(1, "Yogurt, Greek, plain, nonfat", "Foundation", kcal=61),
            _hit(2, "Yogurt, Greek, plain, lowfat", "SR Legacy", kcal=73),
            _hit(3, "Yogurt, Greek, plain, whole milk", "SR Legacy", kcal=97),
        ],
        [
            # one product in three package sizes: same name, brand and values
            _hit(11, "PLAIN GREEK YOGURT", "Branded", brand="FAGE", kcal=54, gtin="689544080008"),
            _hit(12, "PLAIN GREEK YOGURT", "Branded", brand="FAGE", kcal=54, gtin="689544080015"),
            _hit(13, "PLAIN GREEK YOGURT", "Branded", brand="FAGE", kcal=54, gtin="689544080022"),
            # the same name from another brand is another product
            _hit(14, "PLAIN GREEK YOGURT", "Branded", brand="KROGER", kcal=59),
            # a listing repeated under a second id with the same barcode
            _hit(15, "GREEK YOGURT, PLAIN", "Branded", brand="OIKOS", kcal=55, gtin="036632037473"),
            _hit(16, "GREEK YOGURT PLAIN", "Branded", brand="OIKOS", kcal=56, gtin="036632037473"),
        ],
    )
    recs = await fdc.search_ranked("plain greek yogurt", limit=8)
    ids = [r.source_id for r in recs]
    assert ids[:3] == ["1", "2", "3"]  # nonfat, lowfat and whole milk all stay
    assert "12" not in ids and "13" not in ids and "16" not in ids
    fage = recs[ids.index("11")]
    assert fage.same_as == 2
    assert "14" in ids and recs[ids.index("15")].same_as == 1


@pytest.mark.anyio
async def test_chobani_plain_greek_yogurt_respects_the_brand(upstream, fdc, off):
    usda(
        upstream,
        [_hit(1, "Yogurt, Greek, plain, nonfat", "Foundation")],
        [
            _hit(11, "NON-FAT PLAIN GREEK YOGURT", "Branded", brand="CHOBANI", kcal=59),
            _hit(12, "WHOLE MILK PLAIN GREEK YOGURT", "Branded", brand="CHOBANI", kcal=93),
            _hit(13, "PLAIN GREEK YOGURT", "Branded", brand="SHOPRITE"),
            _hit(14, "PLAIN GREEK YOGURT", "Branded", brand="SIMPLY BALANCED"),
            _hit(15, "PLAIN GREEK YOGURT", "Branded", brand="OLD HOME"),
        ],
    )
    ids = [r.source_id for r in await fdc.search_ranked("chobani plain greek yogurt", limit=8)]
    assert ids == ["11", "12"]
    upstream.add(
        OFF_SEARCH,
        {
            "products": [
                _product("1", "Plain Greek Yogurt Non-Fat", "Chobani", kcal=59),
                _product("2", "Plain Greek Yogurt Non-Fat", "Chobani", kcal=59),
                _product("3", "Plain Greek Yogurt Non-Fat", "Chobani", kcal=59),
                _product("4", "Plain Greek Yogurt Whole Milk", "Chobani", kcal=93),
            ]
        },
    )
    recs = await off.search("chobani plain greek yogurt", 8)
    assert [(r.source_id, r.same_as) for r in recs] == [("1", 2), ("4", 0)]


# --- the ranking's safeguards ---


def _rec(name, data_type="SR Legacy", brand=None, source_id="1"):
    nutrients = {"kcal": 100, "protein_g": 1, "carbs_g": 1, "fat_g": 1}
    return FoodRecord("fdc", source_id, name, nutrients, brand=brand, data_type=data_type)


def test_words_the_query_asks_for_are_not_held_against_a_food():
    assert rank.not_plain("breaded chicken", "Chicken, breaded, fried") == 1  # fried only
    assert rank.not_plain("chicken", "Chicken, breaded, fried") == 2
    breaded = _rec("Chicken breast, breaded", source_id="1")
    plain = _rec("Chicken breast, raw", source_id="2")
    assert [r.source_id for r in rank.rank("breaded chicken breast", [plain, breaded], 5)] == ["1"]


def test_prefix_and_plural_safeguards_still_hold():
    egg = _rec("Egg, whole, raw", source_id="1")
    eggplant = _rec("Eggplant, raw", source_id="2")
    assert [r.source_id for r in rank.rank("eggs", [eggplant, egg], 5)] == ["1"]
    assert [r.source_id for r in rank.rank("eggplant", [egg, eggplant], 5)] == ["2"]
    berries = _rec("Blueberries, raw")
    assert rank.rank("blueberry", [berries], 5) == [berries]


def test_nothing_matching_shows_nothing():
    assert rank.rank("tofu", [_rec("Cheese, cheddar")], 5) == []


def test_different_values_are_never_grouped():
    a = _rec("Greek Yogurt", "Branded", brand="X", source_id="1")
    b = _rec("Greek Yogurt", "Branded", brand="X", source_id="2")
    b.nutrients = {**b.nutrients, "kcal": 120}
    assert len(rank.group([a, b])) == 2


# --- USDA generic search failing ---


@pytest.mark.anyio
async def test_failed_generic_search_retries_each_type_on_its_own(upstream, fdc):
    upstream.add(SEARCH, {}, status=500, match=GENERIC)
    upstream.add(
        SEARCH,
        {"foods": [_hit(1, "Chicken, breast, boneless, skinless, raw", "Foundation")]},
        match={"dataType": "Foundation,SR Legacy"},
    )
    upstream.add(SEARCH, {}, status=500, match={"dataType": "Survey (FNDDS)"})
    upstream.add(
        SEARCH, {"foods": [_hit(11, "CHICKEN BREAST", "Branded", brand="TYSON")]}, match=BRANDED
    )
    recs = await fdc.search_ranked("chicken breast", limit=8)
    assert [r.source_id for r in recs] == ["1", "11"]
    sizes = {
        r.url.params["dataType"]: r.url.params["pageSize"]
        for r in upstream.requests
        if r.url.params["dataType"] == "Survey (FNDDS)"
    }
    assert sizes == {"Survey (FNDDS)": "25"}


@pytest.mark.anyio
async def test_generic_search_finding_nothing_asks_each_type_too(upstream, fdc):
    upstream.add(SEARCH, {"foods": []}, match=GENERIC)
    upstream.add(
        SEARCH,
        {"foods": [_hit(1, "Milk, whole, 3.25% milkfat, with added vitamin D", "Foundation")]},
        match={"dataType": "Foundation,SR Legacy"},
    )
    upstream.add(SEARCH, {"foods": []}, match={"dataType": "Survey (FNDDS)"})
    upstream.add(SEARCH, {"foods": []}, match=BRANDED)
    assert [r.source_id for r in await fdc.search_ranked("whole milk", limit=8)] == ["1"]


@pytest.mark.anyio
async def test_rate_limited_generic_search_is_not_retried_per_type(upstream, fdc):
    upstream.add(SEARCH, {}, status=429, match=GENERIC)
    upstream.add(SEARCH, {"foods": []}, match=BRANDED)
    assert await fdc.search_ranked("chicken breast", limit=8) == []
    types = {r.url.params["dataType"] for r in upstream.requests}
    assert types == {GENERIC["dataType"], BRANDED["dataType"]}


# --- the dashboard ---


@pytest.fixture
def app(settings, conn, fdc, off):
    server = build_server(settings, fdc=fdc, off=off)
    with TestClient(server.streamable_http_app(), follow_redirects=False) as client:
        yield client


PER_100G = {
    "kcal": 50,
    "protein_g": 11,
    "carbs_g": 1,
    "fat_g": 0,
    "fiber_g": 0,
    "sugar_g": 0,
    "sodium_mg": 160,
}


def test_a_result_already_in_your_foods_opens_it_and_keeps_its_state(app, conn, upstream):
    # The barcode of the USDA egg whites was saved earlier from Open Food Facts, unconfirmed
    rec = FoodRecord(
        "off", "41220576463", "Liquid egg whites", PER_100G, brand="Hill Country Fare",
        gtin="00041220576463",
    )  # fmt: skip
    row, _ = store.cache_record(conn, rec)
    store.mark_saved(conn, row["id"])
    upstream.add(SEARCH, fixture_json("fdc/search_egg_white_generic.json"), match=GENERIC)
    upstream.add(SEARCH, fixture_json("fdc/search_egg_white_branded.json"), match=BRANDED)
    upstream.add(OFF_SEARCH, {"products": []})
    page = app.get("/app/foods/new", params={"q": "egg white"}).text
    usda_card = page[page.index("USDA FoodData Central") : page.index("Open Food Facts<small>")]
    item = next(li for li in usda_card.split("<li>") if "Liquid egg whites" in li)
    assert "In your foods" in item and "from Open Food Facts" in item and "Unconfirmed" in item
    assert f'href="/app/foods/{row["id"]}?' in item and "/app/foods/found" not in item
    # a plain USDA hit is still added with a tap
    assert 'action="/app/foods/found"' in usda_card
    assert "Raw" in usda_card  # "Egg, white, raw, fresh"
    assert store.get_food(conn, row["id"])["verified"] == 0


def test_foods_search_as_you_type_returns_just_the_results(app, conn):
    store.save_label_food(
        conn, name="Egg white bites", brand=None, gtin=None, per_100g=PER_100G,
        serving_g=100, serving_desc=None,
    )  # fmt: skip
    page = app.get("/app/foods", params={"q": "egg"}).text
    assert "data-live-search" in page and "data-results" in page
    part = app.get("/app/foods", params={"q": "egg", "part": "1"})
    assert part.status_code == 200
    assert "<html" not in part.text and "<h1>" not in part.text
    assert "Egg white bites" in part.text
    # a found food still offers the databases, carrying the words over; nothing upstream yet
    assert 'href="/app/foods/new?q=egg"' in part.text
    assert "Type it in" not in part.text
    empty = app.get("/app/foods", params={"q": "tofu", "part": "1"}).text
    assert "Nothing in your foods matches “tofu”." in empty
    assert 'href="/app/foods/new?q=tofu"' in empty
    assert 'href="/app/foods/new?how=type&amp;name=tofu"' in empty
    everything = app.get("/app/foods", params={"part": "1"}).text
    assert "Egg white bites" in everything and "Search databases" not in everything


def test_foods_search_never_asks_upstream(app, upstream):
    app.get("/app/foods", params={"q": "chicken", "part": "1"})
    assert upstream.requests == []


def test_type_it_in_fills_in_the_name(app):
    page = app.get("/app/foods/new", params={"how": "type", "name": "Grandma's  chili"}).text
    assert 'name="name" value="Grandma&#x27;s chili"' in page


def test_foods_search_as_you_type_finds_a_half_typed_word(app, conn):
    for name, brand in (("Plain Greek Yogurt", "Chobani"), ("Eggs", None), ("Eggplant", None)):
        store.save_label_food(
            conn, name=name, brand=brand, gtin=None, per_100g=PER_100G, serving_g=100,
            serving_desc=None,
        )  # fmt: skip
    typing = app.get("/app/foods", params={"q": "chob", "part": "1", "typing": "1"}).text
    assert "Plain Greek Yogurt" in typing
    plain = typing[typing.index("Plain Greek Yogurt") :]
    assert "Search databases for “chob”" in plain
    # a whole word still comes before a word it only begins
    eggs = app.get("/app/foods", params={"q": "egg", "part": "1", "typing": "1"}).text
    assert eggs.index(">Eggs<") < eggs.index(">Eggplant<")
    # pressing Search (no typing) keeps whole words only
    searched = app.get("/app/foods", params={"q": "egg", "part": "1"}).text
    assert ">Eggs<" in searched and ">Eggplant<" not in searched
    assert "Nothing in your foods" in app.get("/app/foods", params={"q": "chob"}).text
