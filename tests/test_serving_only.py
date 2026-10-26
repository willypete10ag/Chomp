"""Foods with no serving weight (#141): restaurant dishes published per order are saved per
serving and logged in servings, in the connector, the dashboard and exports."""

from __future__ import annotations

import io
import json

import pytest
import test_web
from mcp import Client
from test_tools import add_food, call, call_error
from test_web import ORIGIN, sign_in
from test_web_edits import follow, today

from chomp import export, store, web
from chomp.server import build_server

http = test_web.http
public_settings = test_web.public_settings
logged = test_web.logged

QUESO = {"kcal": 1450, "protein_g": 62, "carbs_g": 102, "fat_g": 88, "sodium_mg": 4120}


@pytest.fixture
def server(settings, conn, fdc):
    return build_server(settings, fdc=fdc)


def save_queso(conn) -> int:
    row, _ = store.save_label_food(
        conn,
        name="Skillet Beef Queso & Chips",
        brand="Chili's",
        gtin=None,
        per_100g=QUESO,
        serving_g=None,
        serving_desc="1 appetizer",
        source="manual",
        verified=False,
    )
    return row["id"]


@pytest.mark.anyio
async def test_save_food_without_a_weight_logs_in_servings(server, conn):
    async with Client(server) as client:
        food = await call(
            client,
            "save_food",
            name="Skillet Beef Queso & Chips",
            brand="Chili's",
            serving_desc="1 appetizer",
            **QUESO,
        )
        assert food["serving_only"] is True and food["serving_g"] is None
        assert food["per_100g"] is None
        assert food["per_serving"]["kcal"] == 1450 and food["per_serving"]["sodium_mg"] == 4120
        logged = await call(
            client,
            "log_entries",
            items=[{"food_id": food["food_id"], "servings": 0.5}],
            eaten_at="2026-10-24T15:30",
        )
        entry = logged["meal"]["entries"][0]
        assert entry["grams"] is None and entry["servings"] == 0.5
        assert entry["nutrients"]["kcal"] == 725 and entry["nutrients"]["fat_g"] == 44
        # Grams would be made up: refused
        grams = await call_error(
            client,
            "log_entries",
            items=[{"food_id": food["food_id"], "grams": 200}],
            eaten_at="now",
        )
        assert "no serving weight: give servings" in grams
        fixed = await call(client, "update_entry", entry_id=entry["entry_id"], servings=1)
        assert fixed["entry"]["servings"] == 1 and fixed["entry"]["nutrients"]["kcal"] == 1450
        # No portions or recipes: they are weights
        portion = await call_error(
            client, "save_portion", food_id=food["food_id"], name="half", grams=50, count=1
        )
        assert "no serving weight" in portion
        recipe = await call_error(
            client,
            "save_recipe",
            name="Queso night",
            servings=2,
            ingredients=[{"food_id": food["food_id"], "grams": 100}],
        )
        assert "no serving weight" in recipe
        # Corrections are per serving, without a serving weight
        confirmed = await call(client, "confirm_food", food_id=food["food_id"], kcal=1460)
        assert confirmed["food"]["per_serving"]["kcal"] == 1460
        assert confirmed["food"]["serving_only"] is True
        weighed = await call_error(
            client, "confirm_food", food_id=food["food_id"], kcal=1460, per_serving_g=400
        )
        assert "leave out per_serving_g" in weighed


@pytest.mark.anyio
async def test_servings_work_for_weighed_foods_too(server, conn):
    pizza = add_food(conn, "Pizza", 266, 11.4, 33.3, 9.7)
    conn.execute("UPDATE foods SET serving_g = 85.4 WHERE id = ?", (pizza,))
    bare = add_food(conn, "Rice", 130, 2.7, 28, 0.3)
    async with Client(server) as client:
        logged = await call(
            client, "log_entries", items=[{"food_id": pizza, "servings": 2}], eaten_at="now"
        )
        entry = logged["meal"]["entries"][0]
        assert entry["grams"] == 170.8 and entry["servings"] == 2
        no_size = await call_error(
            client, "log_entries", items=[{"food_id": bare, "servings": 1}], eaten_at="now"
        )
        assert "no serving size: give grams" in no_size
        both = await call_error(
            client,
            "log_entries",
            items=[{"food_id": pizza, "servings": 1, "grams": 85.4}],
            eaten_at="now",
        )
        assert "one of grams, servings or a portion" in both


@pytest.mark.anyio
async def test_swapping_between_weighed_and_serving_only_needs_an_amount(server, conn):
    queso = save_queso(conn)
    rice = add_food(conn, "Rice", 130, 2.7, 28, 0.3)
    async with Client(server) as client:
        logged = await call(
            client, "log_entries", items=[{"food_id": rice, "grams": 150}], eaten_at="now"
        )
        entry_id = logged["meal"]["entries"][0]["entry_id"]
        swap = await call_error(client, "update_entry", entry_id=entry_id, food_id=queso)
        assert "give the amount" in swap
        swapped = await call(client, "update_entry", entry_id=entry_id, food_id=queso, servings=0.5)
        assert swapped["entry"]["nutrients"]["kcal"] == 725


def test_dashboard_types_in_a_food_without_a_weight(http, conn, logged):
    sign_in(http)
    form = {"mode": "manual", "name": "Chicken Fajitas (Custom)", "brand": "Chili's",
            "serving_desc": "1 order", "serving_g": "", "kcal": "980", "protein_g": "78",
            "carbs_g": "96", "fat_g": "30"}  # fmt: skip
    r = http.post("/app/foods/new", data=form, headers=ORIGIN)
    assert r.status_code == 303, r.text
    food_id = int(r.headers["location"].rsplit("added=", 1)[1])
    food = store.get_food(conn, food_id)
    assert food["serving_only"] == 1 and food["kcal"] == 980
    page = http.get(f"/app/foods/{food_id}").text
    assert "counted in servings" in page and "Per serving · 1 order" in page
    assert "Per 100 g" not in page
    # Log half an order: servings only, no grams anywhere
    sheet = http.get(f"/app/foods/{food_id}/log").text
    assert '<div class="by" hidden>' in sheet and "&times; 100" not in sheet
    data = {"food_id": food_id, "servings": "0.5", "grams": "50", "by": "grams",
            "date": today(), "to": "new", "time": "15:30"}  # fmt: skip
    page = follow(http, http.post("/app/log", data=data, headers=ORIGIN))
    assert "Added 0.5 order of Chicken Fajitas (Custom)." in page
    assert "0.5 order" in page and "50 g" not in page
    picks = http.get("/app/add", params={"date": today(), "q": "fajitas"}).text
    item = picks[picks.index("Chicken Fajitas") :]
    assert "per order" in item and "Log 0.5 order of" in item and "50 g" not in item
    # Editing it keeps it weightless; a weight is refused
    edit = http.get(f"/app/foods/{food_id}/edit").text
    assert 'name="serving_g"' not in edit
    form |= {"mode": "edit", "food_id": str(food_id), "kcal": "990"}
    assert http.post("/app/foods/new", data=form, headers=ORIGIN).status_code == 303
    assert store.get_food(conn, food_id)["kcal"] == 990
    weighed = http.post("/app/foods/new", data=form | {"serving_g": "400"}, headers=ORIGIN)
    assert weighed.status_code == 400 and "no serving weight" in weighed.text


def test_label_foods_still_need_a_serving_size(http, conn, logged):
    sign_in(http)
    form = {"mode": "label", "name": "Granola", "serving_g": "", "kcal": "230",
            "protein_g": "3", "carbs_g": "37", "fat_g": "8"}  # fmt: skip
    r = http.post("/app/foods/new", data=form, headers=ORIGIN)
    assert r.status_code == 400 and "Serving size" in r.text


def test_amounts_show_as_servings(conn):
    queso = store.get_food(conn, save_queso(conn))
    picker = web.amount_picker(queso, 50, 500)
    assert 'value="servings" checked' in picker and 'value="0.5"' in picker
    assert 'value="grams"' not in picker
    chips = web.grams_chips(queso)
    assert "0.5 appetizer" in chips and "2 appetizers" in chips and " g<" not in chips
    entry = store.EntryRow(
        1, 1, queso["id"], "Queso", None, 50, {}, serving_g=100, serving_desc="1 appetizer",
        serving_only=True,
    )  # fmt: skip
    assert web.entry_amount(entry) == "0.5 appetizer"


def test_export_has_servings_not_made_up_grams(conn):
    queso = save_queso(conn)
    store.log_meal(conn, [(queso, 50)], "2026-10-24T19:30:00Z", None)
    out = io.StringIO()
    export.write_csv(conn, out, "America/New_York")
    head, row = out.getvalue().splitlines()[:2]
    cols = dict(zip(head.split(","), row.split(","), strict=False))
    assert cols["grams"] == "" and cols["servings"] == "0.5"
    data = json.loads(json.dumps(export.to_json(conn, "America/New_York")))
    assert data["meals"][0]["entries"][0]["servings"] == 0.5
    assert data["meals"][0]["entries"][0]["grams"] is None
    assert any(f["serving_only"] == 1 for f in data["foods"])
