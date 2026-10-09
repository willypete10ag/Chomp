"""M4 quality-of-life tools: portions (#75), meal templates, totals over a range, targets."""

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
async def test_portion_is_learned_and_logged_by_count(server, foods, upstream):
    _eggs, whites = foods
    upstream.add("/fdc/v1/foods/search", {"foods": []})
    async with Client(server) as client:
        # "those 3 scoops were 90 g": one scoop is 30 g
        out = await call(
            client, "save_portion", food_id=whites, name=" a  Scoop ", grams=90, count=3
        )
        assert out["portion"] == {"name": "Scoop", "grams": 30, "estimated": False}
        assert out["replaced"] is False
        out = await call(client, "save_portion", food_id=whites, name="scoop", grams=32)
        assert out["replaced"] is True and out["portions"][0]["grams"] == 32
        found = await call(client, "search_food", query="scoop")
        top = found["results"][0]
        assert (top["match"], top["food_id"], top["portion"]) == ("portion", whites, "scoop")
        assert top["portions"] == [{"name": "scoop", "grams": 32, "estimated": False}]
        logged = await call(
            client,
            "log_entries",
            items=[{"food_id": whites, "portion": "1 scoop", "count": 2.5}],
            eaten_at="now",
        )
    entry = logged["meal"]["entries"][0]
    assert entry["grams"] == 80 and entry["estimated"] is False


@pytest.mark.anyio
async def test_estimated_amounts_are_marked_until_weighed(server, conn, foods):
    eggs, _whites = foods
    async with Client(server) as client:
        logged = await call(
            client,
            "log_entries",
            items=[{"food_id": eggs, "grams": 100, "estimated": True}],
            eaten_at="now",
        )
        entry = logged["meal"]["entries"][0]
        assert entry["estimated"] is True
        await call(client, "save_portion", food_id=eggs, name="egg", grams=50, estimated=True)
        again = await call(
            client,
            "log_entries",
            items=[{"food_id": eggs, "portion": "egg", "count": 3}],
            eaten_at="now",
        )
        logged_again = again["meal"]["entries"][0]
        assert (logged_again["grams"], logged_again["estimated"]) == (150, True)
        # The user weighs it: the entry and the portion become exact
        fixed = await call(client, "update_entry", entry_id=entry["entry_id"], grams=110)
        assert fixed["entry"]["estimated"] is False
        out = await call(client, "save_portion", food_id=eggs, name="egg", grams=55)
        assert out["portion"]["estimated"] is False
        gone = await call(client, "forget_portion", food_id=eggs, name="Egg")
        assert gone["portion"]["name"] == "egg" and gone["portions"] == []


@pytest.mark.anyio
async def test_log_items_need_grams_or_a_known_portion(server, foods):
    eggs, _ = foods
    async with Client(server) as client:
        both = await call_error(
            client,
            "log_entries",
            items=[{"food_id": eggs, "grams": 50, "portion": "egg"}],
            eaten_at="now",
        )
        assert "grams or a portion" in both
        unknown = await call_error(
            client, "log_entries", items=[{"food_id": eggs, "portion": "egg"}], eaten_at="now"
        )
        assert "no portion named 'egg'" in unknown
        assert "no food with id 5" in await call_error(
            client, "save_portion", food_id=5, name="x", grams=1
        )


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


@pytest.mark.anyio
async def test_search_picks_the_most_specific_template_or_asks(server, foods, upstream):
    eggs, whites = foods
    upstream.add("/fdc/v1/foods/search", {"foods": []})
    async with Client(server) as client:
        for name, food in (("breakfast", eggs), ("usual breakfast", whites)):
            await call(
                client, "save_meal_template", name=name, items=[{"food_id": food, "grams": 50}]
            )
        mine = await call(client, "search_food", query="my usual breakfast")
        assert sorted(mine["templates"]) == ["breakfast", "usual breakfast"]
        assert "'usual breakfast': log it" in mine["notes"][0]
        await call(
            client,
            "save_meal_template",
            name="big breakfast",
            items=[{"food_id": eggs, "grams": 9}],
        )
        vague = await call(client, "search_food", query="big usual breakfast")
    assert "ask the user which one" in vague["notes"][0]


def test_most_specific():
    from chomp import text

    names = ["breakfast", "usual breakfast"]
    assert text.most_specific("my usual breakfast", names) == "usual breakfast"
    assert text.most_specific("breakfast", names) == "breakfast"
    assert text.most_specific("usual", ["usual breakfast", "usual lunch"]) is None
