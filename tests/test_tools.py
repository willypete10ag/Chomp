"""M1 tools end to end through an in-process MCP client, with FDC replayed from fixtures."""

from __future__ import annotations

import pytest
from conftest import fixture_json
from mcp import Client

from chomp.server import build_server

SEARCH = "/fdc/v1/foods/search"


@pytest.fixture
def fdc_eggs(upstream):
    upstream.add(
        SEARCH,
        fixture_json("fdc/search_egg_white_generic.json"),
        match={"dataType": "Foundation,SR Legacy"},
    )
    upstream.add(
        SEARCH,
        fixture_json("fdc/search_egg_white_branded.json"),
        match={"dataType": "Branded"},
    )
    return upstream


@pytest.fixture
def server(settings, conn, fdc):
    return build_server(settings, fdc=fdc)


async def call(client, tool, **args):
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    return result.structured_content


async def call_error(client, tool, **args) -> str:
    result = await client.call_tool(tool, args)
    assert result.is_error
    return result.content[0].text


def add_food(conn, name, kcal, protein, carbs, fat, *, verified=0, brand=None, source="manual"):
    cur = conn.execute(
        "INSERT INTO foods (name, brand, source, verified, kcal, protein_g, carbs_g, fat_g)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, brand, source, verified, kcal, protein, carbs, fat),
    )
    return cur.lastrowid


@pytest.mark.anyio
async def test_tool_list_and_descriptions(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for name in ("search_food", "log_entries", "update_entry", "delete_entry", "get_day"):
        assert name in tools
        assert tools[name].output_schema is not None
    assert "never" in tools["search_food"].description.lower() or "rather than guessing" in (
        tools["search_food"].description
    )
    assert "estimate" in server.instructions.lower()
    # restaurant nutrition from the web goes to save_food, not a refusal or a USDA stand-in (#78)
    assert "save_food" in server.instructions
    assert "web" in tools["save_food"].description
    assert "save_food" in tools["save_food_from_label"].description


@pytest.mark.anyio
async def test_search_caches_fdc_hits_with_ids(server, conn, fdc_eggs):
    async with Client(server) as client:
        out = await call(client, "search_food", query="egg white")
    results = out["results"]
    assert [r["fdc_data_type"] for r in results] == ["Foundation", "SR Legacy", "Branded"]
    assert all(r["match"] == "fdc" and r["food_id"] for r in results)
    assert results[1]["per_100g"]["protein_g"] == 10.9
    rows = conn.execute("SELECT source, source_id FROM foods ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("fdc", "747997"),
        ("fdc", "172183"),
        ("fdc", "2091234"),
    ]


@pytest.mark.anyio
async def test_search_twice_reuses_food_ids(server, conn, fdc_eggs):
    async with Client(server) as client:
        first = await call(client, "search_food", query="egg white")
        second = await call(client, "search_food", query="egg white")
    assert {r["food_id"] for r in first["results"]} == {r["food_id"] for r in second["results"]}
    assert conn.execute("SELECT count(*) FROM foods").fetchone()[0] == 3
    # Cached search hits are not "saved": they keep FDC's ranking on the next search
    assert [r["match"] for r in second["results"]] == ["fdc"] * 3
    assert [r["food_id"] for r in second["results"]] == [r["food_id"] for r in first["results"]]


@pytest.mark.anyio
async def test_logged_fdc_food_ranks_as_saved(server, conn, fdc_eggs):
    async with Client(server) as client:
        first = await call(client, "search_food", query="egg white")
        branded = first["results"][2]["food_id"]
        await call(
            client, "log_entries", items=[{"food_id": branded, "grams": 100}], eaten_at="now"
        )
        second = await call(client, "search_food", query="egg white")
    r = second["results"]
    assert (r[0]["food_id"], r[0]["match"]) == (branded, "saved")
    assert [x["match"] for x in r[1:]] == ["fdc", "fdc"]


@pytest.mark.anyio
async def test_search_ranks_portion_then_verified_saved_then_fdc(server, conn, fdc_eggs):
    plain = add_food(conn, "Egg white omelette", 50, 10, 1, 0)
    verified = add_food(conn, "Egg white bites", 70, 8, 4, 2, verified=1)
    conn.execute(
        "INSERT INTO portions (user_id, food_id, name, grams) VALUES (1, ?, 'egg white', 300)",
        (plain,),
    )
    async with Client(server) as client:
        out = await call(client, "search_food", query="egg white", limit=4)
    r = out["results"]
    assert [x["match"] for x in r] == ["portion", "saved", "fdc", "fdc"]
    assert r[0]["food_id"] == plain and r[0]["portions"][0]["grams"] == 300
    assert r[1]["food_id"] == verified  # the plain one already appeared for its portion


@pytest.mark.anyio
async def test_search_prefix_matching(server, conn):
    eggs = add_food(conn, "Eggs, scrambled", 150, 10, 2, 11)
    async with Client(server) as client:
        out = await call(client, "search_food", query="egg", limit=1)
    assert out["results"][0]["food_id"] == eggs


@pytest.mark.anyio
async def test_search_saved_matches_plurals_but_not_other_words(server, conn, upstream):
    upstream.add(SEARCH, {"foods": []})
    egg = add_food(conn, "Egg, whole, cooked, scrambled", 149, 10, 1.6, 11)
    add_food(conn, "Eggplant, raw", 25, 1, 6, 0.2)
    berry = add_food(conn, "Blueberry muffin", 380, 5, 50, 18)
    async with Client(server) as client:
        eggs = await call(client, "search_food", query="scrambled eggs")
        egg_only = await call(client, "search_food", query="egg")
        berries = await call(client, "search_food", query="blueberries")
    assert [r["food_id"] for r in eggs["results"]] == [egg]
    assert [r["food_id"] for r in egg_only["results"]] == [egg]
    assert [r["food_id"] for r in berries["results"]] == [berry]


@pytest.mark.anyio
async def test_search_saved_limit_skips_prefix_only_hits(server, conn, upstream):
    upstream.add(SEARCH, {"foods": []})
    for i in range(8):
        add_food(conn, f"Eggplant {i}", 25, 1, 6, 0.2)
    egg = add_food(conn, "Egg, whole, cooked, scrambled, with milk and butter", 149, 10, 1.6, 11)
    async with Client(server) as client:
        out = await call(client, "search_food", query="egg", limit=1)
    assert [r["food_id"] for r in out["results"]] == [egg]


@pytest.mark.anyio
async def test_search_survives_fdc_outage(server, conn, upstream):
    upstream.add(SEARCH, {}, status=503)
    add_food(conn, "Egg white", 52, 10.9, 0.7, 0.2)
    async with Client(server) as client:
        out = await call(client, "search_food", query="egg white")
    assert len(out["results"]) == 1
    assert "unavailable" in out["notes"][0]


@pytest.mark.anyio
async def test_search_survives_slightly_negative_fdc_carbs(server, conn, upstream):
    nutrients = [
        {"nutrientId": 1008, "unitName": "KCAL", "value": 116},
        {"nutrientId": 1003, "unitName": "G", "value": 25.5},
        {"nutrientId": 1005, "unitName": "G", "value": -0.104},
        {"nutrientId": 1004, "unitName": "G", "value": 0.8},
    ]
    food = {"fdcId": 9, "description": "Tuna, light, canned", "dataType": "SR Legacy"}
    upstream.add(SEARCH, {"foods": [{**food, "foodNutrients": nutrients}]})
    async with Client(server) as client:
        out = await call(client, "search_food", query="tuna casserole")
    assert out["results"][0]["per_100g"]["carbs_g"] == 0


@pytest.mark.anyio
async def test_search_no_match_says_so(server, upstream):
    upstream.add(SEARCH, {"foods": []})
    async with Client(server) as client:
        out = await call(client, "search_food", query="zzzz")
    assert out["results"] == []
    assert "barcode or label photo" in out["notes"][-1]


@pytest.mark.anyio
async def test_search_without_fdc_key(settings, conn):
    from chomp.sources.fdc import FDCClient

    server = build_server(settings, fdc=FDCClient(""))
    async with Client(server) as client:
        out = await call(client, "search_food", query="anything")
    assert "not configured" in out["notes"][0]


@pytest.mark.anyio
async def test_search_handles_fts_syntax_in_query(server, upstream):
    upstream.add(SEARCH, {"foods": []})
    async with Client(server) as client:
        out = await call(client, "search_food", query='egg "AND" (white*) OR -')
    assert out["results"] == []


EGGS = (143, 12.6, 0.72, 9.51)
WHITES = (52, 10.9, 0.73, 0.17)


@pytest.mark.anyio
async def test_log_correct_and_review_the_done_when_scenario(server, conn):
    """'4 scrambled eggs and 300 g egg whites': log, correct the eggs to 220 g, review."""
    eggs = add_food(conn, "Egg, whole, scrambled", *EGGS)
    whites = add_food(conn, "Egg white, raw", *WHITES)
    async with Client(server) as client:
        logged = await call(
            client,
            "log_entries",
            items=[{"food_id": eggs, "grams": 200}, {"food_id": whites, "grams": 300}],
            meal_label="breakfast",
            eaten_at="2026-10-05T08:30",
        )
        meal = logged["meal"]
        assert meal["eaten_at"] == "2026-10-05T08:30:00-04:00"
        assert [e["nutrients"]["kcal"] for e in meal["entries"]] == [286, 156]
        assert meal["totals"]["protein_g"] == pytest.approx(25.2 + 32.7)
        assert logged["day"]["date"] == "2026-10-05"

        egg_entry = meal["entries"][0]["entry_id"]
        fixed = await call(client, "update_entry", entry_id=egg_entry, grams=220)
        assert fixed["entry"]["grams"] == 220
        assert fixed["entry"]["nutrients"]["kcal"] == 315  # 314.6 rounded
        assert fixed["meal"]["totals"]["kcal"] == 471

        day = await call(client, "get_day", date="2026-10-05")
    assert len(day["meals"]) == 1
    assert day["totals"]["kcal"] == 471
    assert day["totals"]["protein_g"] == pytest.approx(27.7 + 32.7, abs=0.1)
    # no goals set: progress is against the defaults, 2,000 kcal and even thirds
    assert day["progress"]["kcal"]["target"] == 2000
    assert {k: p["target"] for k, p in day["progress"].items()} == {
        "kcal": 2000,
        "protein_g": 167,
        "carbs_g": 166,
        "fat_g": 74,
    }


@pytest.mark.anyio
async def test_snapshot_survives_food_edit(server, conn):
    eggs = add_food(conn, "Egg", *EGGS)
    async with Client(server) as client:
        await call(
            client,
            "log_entries",
            items=[{"food_id": eggs, "grams": 100}],
            eaten_at="2026-10-05T12:00:00Z",
        )
        conn.execute("UPDATE foods SET kcal = 999 WHERE id = ?", (eggs,))
        day = await call(client, "get_day", date="2026-10-05")
    assert day["totals"]["kcal"] == 143


@pytest.mark.anyio
async def test_update_swaps_food(server, conn):
    eggs = add_food(conn, "Egg", *EGGS)
    whites = add_food(conn, "Egg white", *WHITES)
    async with Client(server) as client:
        logged = await call(
            client, "log_entries", items=[{"food_id": eggs, "grams": 100}], eaten_at="now"
        )
        entry_id = logged["meal"]["entries"][0]["entry_id"]
        out = await call(client, "update_entry", entry_id=entry_id, food_id=whites)
    assert out["entry"]["food_id"] == whites
    assert out["entry"]["nutrients"]["kcal"] == 52


@pytest.mark.anyio
async def test_update_needs_a_change(server, conn):
    async with Client(server) as client:
        msg = await call_error(client, "update_entry", entry_id=1)
    assert "grams, food_id, or both" in msg


@pytest.mark.anyio
async def test_delete_entry_then_meal(server, conn):
    eggs = add_food(conn, "Egg", *EGGS)
    whites = add_food(conn, "Egg white", *WHITES)
    async with Client(server) as client:
        logged = await call(
            client,
            "log_entries",
            items=[{"food_id": eggs, "grams": 100}, {"food_id": whites, "grams": 100}],
            eaten_at="now",
        )
        first, second = (e["entry_id"] for e in logged["meal"]["entries"])
        out = await call(client, "delete_entry", entry_id=first)
        assert out["meal_deleted"] is False
        assert out["meal"]["totals"]["kcal"] == 52
        assert out["day"]["totals"]["kcal"] == 52
        out = await call(client, "delete_entry", entry_id=second)
        assert out["meal_deleted"] is True and out["meal"] is None
        assert out["day"]["totals"]["kcal"] == 0
    assert conn.execute("SELECT count(*) FROM meals").fetchone()[0] == 0


@pytest.mark.anyio
async def test_update_meal_moves_it_to_another_day(server, conn):
    eggs = add_food(conn, "Egg", *EGGS)
    async with Client(server) as client:
        logged = await call(
            client,
            "log_entries",
            items=[{"food_id": eggs, "grams": 100}],
            meal_label="breakfast",
            note="at home",
            eaten_at="2026-10-06T08:00",
        )
        meal_id = logged["meal"]["meal_id"]
        out = await call(
            client, "update_meal", meal_id=meal_id, eaten_at="2026-10-05T12:30", meal_label="lunch"
        )
        assert out["meal"]["eaten_at"] == "2026-10-05T12:30:00-04:00"
        assert out["meal"]["label"] == "lunch" and out["meal"]["note"] == "at home"
        assert out["meal"]["totals"]["kcal"] == 143
        assert out["day"]["date"] == "2026-10-05" and out["day"]["totals"]["kcal"] == 143
        assert out["previous_day"]["date"] == "2026-10-06"
        assert out["previous_day"]["totals"]["kcal"] == 0
        sixth = await call(client, "get_day", date="2026-10-06")
    assert sixth["meals"] == []


@pytest.mark.anyio
async def test_update_meal_same_day_and_clearing(server, conn):
    eggs = add_food(conn, "Egg", *EGGS)
    async with Client(server) as client:
        logged = await call(
            client,
            "log_entries",
            items=[{"food_id": eggs, "grams": 100}],
            meal_label="snack",
            note="oops",
            eaten_at="2026-10-05T15:00",
        )
        meal_id = logged["meal"]["meal_id"]
        out = await call(client, "update_meal", meal_id=meal_id, eaten_at="2026-10-05T16:15")
        assert out["previous_day"] is None and out["meal"]["label"] == "snack"
        out = await call(client, "update_meal", meal_id=meal_id, note="")
    assert out["meal"]["note"] is None
    assert out["meal"]["eaten_at"] == "2026-10-05T16:15:00-04:00"


@pytest.mark.anyio
async def test_update_meal_errors(server, conn):
    async with Client(server) as client:
        assert "eaten_at, meal_label, note" in await call_error(client, "update_meal", meal_id=1)
        assert "no meal with id 1" in await call_error(
            client, "update_meal", meal_id=1, meal_label="lunch"
        )
        eggs = add_food(conn, "Egg", *EGGS)
        logged = await call(
            client, "log_entries", items=[{"food_id": eggs, "grams": 1}], eaten_at="now"
        )
        msg = await call_error(
            client, "update_meal", meal_id=logged["meal"]["meal_id"], eaten_at="2026-10-05"
        )
    assert "needs a time" in msg


@pytest.mark.anyio
async def test_log_entries_adds_to_an_existing_meal(server, conn):
    eggs = add_food(conn, "Egg", *EGGS)
    whites = add_food(conn, "Egg white", *WHITES)
    async with Client(server) as client:
        logged = await call(
            client,
            "log_entries",
            items=[{"food_id": eggs, "grams": 100}],
            meal_label="breakfast",
            eaten_at="2026-10-05T08:00",
        )
        meal_id = logged["meal"]["meal_id"]
        out = await call(
            client, "log_entries", items=[{"food_id": whites, "grams": 100}], meal_id=meal_id
        )
        assert out["meal"]["meal_id"] == meal_id
        assert [e["food_id"] for e in out["meal"]["entries"]] == [eggs, whites]
        assert out["meal"]["totals"]["kcal"] == 195
        assert out["day"]["date"] == "2026-10-05" and out["day"]["totals"]["kcal"] == 195
        msg = await call_error(
            client,
            "log_entries",
            items=[{"food_id": whites, "grams": 1}],
            meal_id=meal_id,
            meal_label="lunch",
        )
        assert "update_meal" in msg
        assert "no meal with id 999" in await call_error(
            client, "log_entries", items=[{"food_id": whites, "grams": 1}], meal_id=999
        )
    assert conn.execute("SELECT count(*) FROM meals").fetchone()[0] == 1


@pytest.mark.anyio
async def test_log_rejects_unknown_food_and_writes_nothing(server, conn):
    eggs = add_food(conn, "Egg", *EGGS)
    async with Client(server) as client:
        msg = await call_error(
            client,
            "log_entries",
            items=[{"food_id": eggs, "grams": 100}, {"food_id": 9999, "grams": 50}],
            eaten_at="now",
        )
    assert "no food with id 9999" in msg
    assert conn.execute("SELECT count(*) FROM meals").fetchone()[0] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("grams", [0, -10])
async def test_log_rejects_non_positive_grams(server, conn, grams):
    eggs = add_food(conn, "Egg", *EGGS)
    async with Client(server) as client:
        result = await client.call_tool(
            "log_entries", {"items": [{"food_id": eggs, "grams": grams}]}
        )
    assert result.is_error


@pytest.mark.anyio
async def test_log_requires_a_time(server, conn):
    eggs = add_food(conn, "Egg", *EGGS)
    async with Client(server) as client:
        msg = await call_error(client, "log_entries", items=[{"food_id": eggs, "grams": 1}])
    assert "eaten_at is required" in msg and "ask the user" in msg
    assert conn.execute("SELECT count(*) FROM meals").fetchone()[0] == 0


@pytest.mark.anyio
async def test_log_rejects_date_only_eaten_at(server, conn):
    eggs = add_food(conn, "Egg", *EGGS)
    async with Client(server) as client:
        msg = await call_error(
            client, "log_entries", items=[{"food_id": eggs, "grams": 1}], eaten_at="2026-10-05"
        )
    assert "needs a time" in msg


@pytest.mark.anyio
async def test_get_day_uses_local_midnight(server, conn):
    eggs = add_food(conn, "Egg", *EGGS)
    async with Client(server) as client:
        # 23:30 local on the 5th is 03:30Z on the 6th (EDT)
        await call(
            client,
            "log_entries",
            items=[{"food_id": eggs, "grams": 100}],
            eaten_at="2026-10-05T23:30",
        )
        fifth = await call(client, "get_day", date="2026-10-05")
        sixth = await call(client, "get_day", date="2026-10-06")
    assert fifth["totals"]["kcal"] == 143
    assert sixth["totals"]["kcal"] == 0 and sixth["meals"] == []


@pytest.mark.anyio
async def test_get_day_progress_against_targets(server, conn):
    eggs = add_food(conn, "Egg", *EGGS)
    conn.execute("INSERT INTO targets (user_id, kcal, protein_g) VALUES (1, 2000, 180)")
    async with Client(server) as client:
        await call(
            client,
            "log_entries",
            items=[{"food_id": eggs, "grams": 500}],
            eaten_at="2026-10-05T09:00",
        )
        day = await call(client, "get_day", date="2026-10-05")
    assert day["progress"]["protein_g"] == {
        "target": 180,
        "eaten": 63.0,
        "remaining": 117.0,
        "percent": 35.0,
    }
    # carbs and fat aren't set, so they count as a third of the calorie goal
    assert day["progress"]["carbs_g"]["target"] == 166
    assert day["progress"]["fat_g"]["target"] == 74


@pytest.mark.anyio
async def test_get_day_rejects_bad_date(server):
    async with Client(server) as client:
        msg = await call_error(client, "get_day", date="next tuesday")
    assert "YYYY-MM-DD" in msg


@pytest.mark.anyio
async def test_tool_annotations(server):
    async with Client(server) as client:
        tools = {t.name: t.annotations for t in (await client.list_tools()).tools}
    assert len(tools) == 17 and all(a is not None for a in tools.values())
    assert tools["get_day"].read_only_hint is True
    assert tools["get_totals"].read_only_hint is True
    assert tools["search_food"].destructive_hint is False
    assert tools["delete_entry"].destructive_hint is True
    assert tools["save_food_from_label"].destructive_hint is True
    assert tools["save_food"].destructive_hint is False
