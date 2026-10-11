"""Serving sizes for generic USDA foods from their household measures."""

from __future__ import annotations

import pytest
from conftest import fixture_json

from chomp import serving_fill, store
from chomp.sources import fdc as fdcmod

SEARCH = "/fdc/v1/foods/search"

# A search hit's foodMeasures, the way SR Legacy foods list them
EGG_MEASURES = [
    {"disseminationText": "1 cup", "gramWeight": 243, "rank": 1},
    {"disseminationText": "1 extra large", "gramWeight": 56, "rank": 2},
    {"disseminationText": "1 large", "gramWeight": 50, "rank": 3},
    {"disseminationText": "1 small", "gramWeight": 38, "rank": 4},
]


def egg_hit(**extra):
    food = dict(fixture_json("fdc/search_egg_white_generic.json")["foods"][0])
    return food | extra


def test_counted_measure_is_the_serving():
    assert fdcmod.household_serving({"foodMeasures": EGG_MEASURES}) == (50, "1 large (50 g)")
    medium = [*EGG_MEASURES, {"disseminationText": "1 medium", "gramWeight": 44, "rank": 5}]
    assert fdcmod.household_serving({"foodMeasures": medium})[0] == 44
    # nothing counted: the first single measure in USDA's order, never a bare weight
    measures = [
        {"disseminationText": "1 oz", "gramWeight": 28.35, "rank": 2},
        {"disseminationText": "1 cup, chopped", "gramWeight": 150, "rank": 3},
    ]
    assert fdcmod.household_serving({"foodMeasures": measures})[0] == 28.35
    bare = [
        {"disseminationText": "100 g", "gramWeight": 100},
        {"disseminationText": "1 lb", "gramWeight": 453.6},
        {"disseminationText": "Quantity not specified", "gramWeight": 120},
    ]
    assert fdcmod.household_serving({"foodMeasures": bare}) is None
    assert fdcmod.household_serving({}) is None


def test_detail_food_portions():
    # /food/{id} details: SR Legacy gives amount + modifier, FNDDS a portionDescription
    sr = [
        {"amount": 1, "modifier": "cup", "measureUnit": {"name": "undetermined"},
         "gramWeight": 220, "sequenceNumber": 1},
        {"amount": 1, "modifier": "large", "measureUnit": {"name": "undetermined"},
         "gramWeight": 61, "sequenceNumber": 2},
    ]  # fmt: skip
    assert fdcmod.household_serving({"foodPortions": sr}) == (61, "1 large (61 g)")
    fndds = [
        {"portionDescription": "Quantity not specified", "gramWeight": 92, "modifier": "90000"},
        {"portionDescription": "1 egg", "gramWeight": 46, "modifier": "10205"},
    ]
    assert fdcmod.household_serving({"foodPortions": fndds})[0] == 46
    foundation = [{"amount": 1, "measureUnit": {"name": "cup"}, "gramWeight": 240}]
    assert fdcmod.household_serving({"foodPortions": foundation}) == (240, "1 cup (240 g)")


def test_generic_record_gets_household_serving_branded_keeps_label():
    rec = fdcmod.to_record(egg_hit(foodMeasures=EGG_MEASURES))
    assert rec.serving_g == 50 and rec.serving_desc == "1 large (50 g)"
    branded = fixture_json("fdc/search_egg_white_branded.json")["foods"][0]
    label = fdcmod.to_record(branded)
    assert fdcmod.to_record(branded | {"foodMeasures": EGG_MEASURES}).serving_g == label.serving_g


def test_cached_food_without_serving_gets_one(conn):
    row, _ = store.cache_record(conn, fdcmod.to_record(egg_hit()))
    assert row["serving_g"] is None
    row, created = store.cache_record(conn, fdcmod.to_record(egg_hit(foodMeasures=EGG_MEASURES)))
    assert not created and row["serving_g"] == 50 and row["serving_desc"] == "1 large (50 g)"
    # one it already has is never replaced
    other = [{"disseminationText": "1 medium", "gramWeight": 44}]
    row, _ = store.cache_record(conn, fdcmod.to_record(egg_hit(foodMeasures=other)))
    assert row["serving_g"] == 50


@pytest.mark.anyio
async def test_backfill_fills_used_foods(settings, conn, fdc, upstream):
    used, _ = store.cache_record(conn, fdcmod.to_record(egg_hit()))
    unused, _ = store.cache_record(
        conn, fdcmod.to_record(egg_hit(fdcId=999, description="Egg, unused"))
    )
    conn.execute(
        "INSERT INTO portions (user_id, food_id, name, grams) VALUES (1, ?, 'bowl', 200)",
        (used["id"],),
    )
    detail = egg_hit(foodPortions=[{"amount": 1, "modifier": "large", "gramWeight": 33}])
    upstream.add(f"/fdc/v1/food/{used['source_id']}", detail)
    assert await serving_fill.backfill(settings.db_path, fdc, pause_s=0) == 1
    assert store.get_food(conn, used["id"])["serving_g"] == 33
    assert store.get_food(conn, unused["id"])["serving_g"] is None
    assert [r.url.path for r in upstream.requests] == [f"/fdc/v1/food/{used['source_id']}"]
    # nothing left to fill: no more requests
    assert await serving_fill.backfill(settings.db_path, fdc, pause_s=0) == 0
    assert len(upstream.requests) == 1
