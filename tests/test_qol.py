"""M4 quality-of-life tools: aliases, meal templates, totals over a range, targets."""

from __future__ import annotations

import pytest
from mcp import Client
from test_tools import EGGS, WHITES, add_food, call, call_error

from chomp.server import build_server


@pytest.fixture
def server(settings, conn, fdc):
    return build_server(settings, fdc=fdc)


@pytest.fixture
def foods(conn):
    return add_food(conn, "Egg, whole", *EGGS), add_food(conn, "Egg white", *WHITES)


@pytest.mark.anyio
async def test_alias_is_found_first_with_default_grams(server, foods, upstream):
    _eggs, whites = foods
    upstream.add("/fdc/v1/foods/search", {"foods": []})
    async with Client(server) as client:
        out = await call(
            client, "save_alias", alias=" My Egg Whites ", food_id=whites, default_grams=300
        )
        assert out["alias"] == "My Egg Whites" and out["replaced"] is False
        out = await call(
            client, "save_alias", alias="my egg whites", food_id=whites, default_grams=250
        )
        assert out["replaced"] is True
        found = await call(client, "search_food", query="my egg whites")
    top = found["results"][0]
    assert top["match"] == "alias" and top["food_id"] == whites and top["default_grams"] == 250


@pytest.mark.anyio
async def test_alias_needs_existing_food(server):
    async with Client(server) as client:
        assert "no food with id 5" in await call_error(client, "save_alias", alias="x", food_id=5)


@pytest.mark.anyio
async def test_usual_breakfast_in_one_call(server, conn, foods):
    eggs, whites = foods
    async with Client(server) as client:
        saved = await call(
            client,
            "save_meal_template",
            name="Usual Breakfast",
            items=[
                {"food_id": eggs, "grams": 200},
                {"food_id": whites, "grams": 300},
                {"food_id": eggs, "grams": 50},  # same food twice is allowed
            ],
        )
        assert saved["replaced"] is False
        assert saved["totals"]["kcal"] == round(143 * 2.5 + 52 * 3)

        logged = await call(
            client, "log_meal_template", name="my usual breakfast", eaten_at="2026-10-05T07:45"
        )
    meal = logged["meal"]
    assert meal["label"] == "Usual Breakfast"
    assert [e["grams"] for e in meal["entries"]] == [200, 300, 50]
    assert meal["totals"]["kcal"] == saved["totals"]["kcal"]
    assert logged["day"]["date"] == "2026-10-05"


@pytest.mark.anyio
async def test_template_replace_and_unknown_name(server, foods):
    eggs, whites = foods
    async with Client(server) as client:
        await call(
            client, "save_meal_template", name="lunch", items=[{"food_id": eggs, "grams": 1}]
        )
        again = await call(
            client, "save_meal_template", name="LUNCH", items=[{"food_id": whites, "grams": 100}]
        )
        assert again["replaced"] is True and [i["food_id"] for i in again["items"]] == [whites]
        msg = await call_error(client, "log_meal_template", name="dinner", eaten_at="now")
    assert "no meal template named 'dinner'" in msg and "LUNCH" in msg


@pytest.mark.anyio
async def test_weekly_protein_check_against_target(server, foods):
    _eggs, whites = foods
    async with Client(server) as client:
        targets = await call(client, "set_targets", protein_g=180, kcal=2200)
        assert targets == {"kcal": 2200, "protein_g": 180, "carbs_g": None, "fat_g": None}
        # Oct 1 and Oct 3 logged; Oct 3's late meal is 03:30Z on Oct 4 but local Oct 3
        for when in ("2026-10-01T08:00", "2026-10-03T23:30"):
            await call(
                client,
                "log_entries",
                items=[{"food_id": whites, "grams": 1000}],
                eaten_at=when,
            )
        out = await call(client, "get_totals", start="2026-10-01", end="2026-10-07")
    assert out["days"] == 7 and out["days_logged"] == 2
    assert out["totals"]["protein_g"] == 218
    assert out["average_per_logged_day"]["protein_g"] == 109
    assert out["average_per_day"]["protein_g"] == pytest.approx(31.1)
    assert out["progress"]["protein_g"] == {
        "target": 180,
        "eaten": 109.0,
        "remaining": 71.0,
        "percent": 61.0,
    }
    by_date = {d["date"]: d["totals"]["kcal"] for d in out["per_day"]}
    assert by_date == {
        "2026-10-01": 520,
        "2026-10-02": 0,
        "2026-10-03": 520,
        "2026-10-04": 0,
        "2026-10-05": 0,
        "2026-10-06": 0,
        "2026-10-07": 0,
    }


@pytest.mark.anyio
async def test_totals_empty_range_and_limits(server):
    async with Client(server) as client:
        out = await call(client, "get_totals", start="2026-10-01", end="2026-10-02")
        assert out["days_logged"] == 0 and out["average_per_logged_day"] is None
        assert out["progress"] == {}
        assert "before start" in await call_error(
            client, "get_totals", start="2026-10-02", end="2026-10-01"
        )
        assert "maximum is 366" in await call_error(
            client, "get_totals", start="2024-01-01", end="2026-01-01"
        )


@pytest.mark.anyio
async def test_set_targets_partial_update_and_clear(server):
    async with Client(server) as client:
        await call(client, "set_targets", kcal=2000, protein_g=150)
        out = await call(client, "set_targets", protein_g=180)
        assert out == {"kcal": 2000, "protein_g": 180, "carbs_g": None, "fat_g": None}
        out = await call(client, "set_targets", clear=["kcal"])
        assert out["kcal"] is None and out["protein_g"] == 180
        assert "at least one" in await call_error(client, "set_targets")
        assert "both set and cleared" in await call_error(
            client, "set_targets", kcal=1, clear=["kcal"]
        )
        bad = await client.call_tool("set_targets", {"protein_g": 0})
        assert bad.is_error


@pytest.mark.anyio
async def test_search_points_to_a_matching_template(server, foods, upstream):
    # Live 2026-10-06: "log my protein shake" searched FDC and offered bottled shakes
    _eggs, whites = foods
    upstream.add("/fdc/v1/foods/search", {"foods": []})
    async with Client(server) as client:
        await call(
            client,
            "save_meal_template",
            name="protein shake",
            items=[{"food_id": whites, "grams": 64}],
        )
        found = await call(client, "search_food", query="my protein shake")
        assert found["templates"] == ["protein shake"]
        assert "log_meal_template" in found["notes"][0]
        other = await call(client, "search_food", query="egg")
    assert other["templates"] == []
    assert not any("template" in n for n in other["notes"])
