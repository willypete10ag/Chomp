"""Vitamins and minerals (#53): parsing from FDC and OFF, label input, snapshots, totals,
the dashboard and export."""

from __future__ import annotations

import csv
import io

import pytest
from mcp import Client
from test_tools import EGGS, add_food, call, call_error
from test_web import http, logged, public_settings, sign_in  # noqa: F401 (fixtures)

from chomp import export, logic, store
from chomp.server import build_server
from chomp.sources import FoodRecord
from chomp.sources import fdc as fdcmod
from chomp.sources import off as offmod


@pytest.fixture
def server(settings, conn, fdc):
    return build_server(settings, fdc=fdc)


# --- logic ----------------------------------------------------------------------------------


def test_units_come_from_the_name_and_convert():
    assert logic.unit_of("iron_mg") == "mg" and logic.unit_of("folate_mcg") == "mcg"
    assert logic.convert_amount("iron_mg", 0.0018, "g") == pytest.approx(1.8)
    assert logic.convert_amount("vitamin_d_mcg", 2.5, "UG") == 2.5
    assert logic.convert_amount("vitamin_d_mcg", 2.5, "µg") == 2.5
    assert logic.convert_amount("vitamin_d_mcg", 400, "IU") == 10  # 40 IU per mcg
    assert logic.convert_amount("vitamin_a_mcg", 1000, "IU") is None  # depends on the food
    assert logic.convert_amount("potassium_mg", 1, "furlong") is None


def test_percent_daily_value_both_ways():
    assert logic.percent_dv_to_amounts({"iron_mg": 10, "vitamin_d_mcg": 25}) == {
        "iron_mg": pytest.approx(1.8),
        "vitamin_d_mcg": 5,
    }
    assert logic.percent_dv("potassium_mg", 470) == 10
    assert logic.percent_dv("potassium_mg", None) is None
    with pytest.raises(ValueError, match="Daily Value"):
        logic.percent_dv_to_amounts({"protein_g": 10})


def test_snapshot_scales_micros_and_sums_name_partial_ones():
    per_100g = {"kcal": 100, "protein_g": 1, "carbs_g": 1, "fat_g": 1, "iron_mg": 2.0}
    snap = logic.snapshot(per_100g, 50)
    assert snap["iron_mg"] == 1.0 and snap["calcium_mg"] is None
    other = logic.snapshot({**per_100g, "iron_mg": None, "calcium_mg": 30}, 100)
    total = logic.sum_nutrients([snap, other])
    assert total["iron_mg"] == 1.0 and total["calcium_mg"] == 30
    assert logic.partial_micros([snap, other]) == ["calcium_mg", "iron_mg"]
    assert logic.partial_micros([snap, snap]) == []


# --- sources --------------------------------------------------------------------------------


def test_fdc_micros_in_both_formats_with_units():
    n = fdcmod.parse_nutrients(
        [
            {"nutrientId": 1008, "unitName": "KCAL", "value": 100},
            {"nutrientId": 1003, "unitName": "G", "value": 1},
            {"nutrientId": 1005, "unitName": "G", "value": 1},
            {"nutrientId": 1004, "unitName": "G", "value": 1},
            {"nutrientId": 1110, "unitName": "IU", "value": 80},
            {"nutrientId": 1089, "unitName": "MG", "value": 1.2},
            {"nutrientId": 1104, "unitName": "IU", "value": 500},  # vitamin A IU: unusable
            {"nutrient": {"id": 1092, "unitName": "mg"}, "amount": 320},
            {"nutrientId": 1177, "unitName": "UG", "value": 44},
        ]
    )
    assert n["vitamin_d_mcg"] == 2 and n["iron_mg"] == 1.2 and n["potassium_mg"] == 320
    assert n["vitamin_a_mcg"] is None
    assert n["folate_mcg"] == 44  # total folate when DFE is absent


def test_fdc_label_check_reads_calcium_iron_potassium():
    rec = FoodRecord(
        source="fdc",
        source_id="1",
        name="Bar",
        nutrients={"kcal": 100, "protein_g": 10, "carbs_g": 10, "fat_g": 1},
        serving_g=50,
    )
    detail = {
        "labelNutrients": {
            "calories": {"value": 200},  # differs from per-100 g, so the label wins
            "protein": {"value": 5},
            "carbohydrates": {"value": 5},
            "fat": {"value": 0.5},
            "iron": {"value": 1.8},
            "potassium": {"value": 100},
        }
    }
    out = fdcmod.apply_label_check(rec, detail)
    assert out.nutrients["iron_mg"] == pytest.approx(3.6)
    assert out.nutrients["potassium_mg"] == pytest.approx(200)


def test_off_micros_are_grams_per_100g():
    n = offmod.parse_nutrients(
        {
            "energy-kcal_100g": 50,
            "proteins_100g": 1,
            "carbohydrates_100g": 1,
            "fat_100g": 1,
            "vitamin-d_100g": 0.0000025,
            "iron_100g": 0.0021,
            "vitamin-b9_100g": "0.0001",
        }
    )
    assert n["vitamin_d_mcg"] == pytest.approx(2.5)
    assert n["iron_mg"] == pytest.approx(2.1)
    assert n["folate_mcg"] == pytest.approx(100)
    assert n["zinc_mg"] is None


def test_cached_food_gains_micros_it_lacked(conn):
    base = {"kcal": 50, "protein_g": 1, "carbs_g": 1, "fat_g": 1}
    rec = FoodRecord(source="fdc", source_id="9", name="Kale", nutrients=dict(base))
    row, created = store.cache_record(conn, rec)
    assert created and row["iron_mg"] is None
    rec.nutrients = {**base, "kcal": 999, "iron_mg": 1.5}
    row, created = store.cache_record(conn, rec)
    assert not created
    assert row["iron_mg"] == 1.5 and row["kcal"] == 50  # macros never change this way
    conn.execute("UPDATE foods SET verified = 1, iron_mg = NULL WHERE id = ?", (row["id"],))
    row, _ = store.cache_record(conn, rec)
    assert row["iron_mg"] is None  # a food the user confirmed is left alone


# --- tools ----------------------------------------------------------------------------------

LABEL = dict(
    name="Fortified cereal",
    serving_g=40,
    kcal=150,
    protein_g=3,
    carbs_g=33,
    fat_g=1,
)


@pytest.mark.anyio
async def test_label_micros_by_amount_and_percent_then_logged_and_totalled(server, conn):
    eggs = add_food(conn, "Egg, whole", *EGGS)
    async with Client(server) as client:
        saved = await call(
            client,
            "save_food_from_label",
            **LABEL,
            micronutrients={"vitamin_d_mcg": 2, "iron_mg": 8, "potassium_mg": 140},
            micronutrients_percent_dv={"thiamin_mg": 25, "folate_mcg": 50},
        )
        per_100g = saved["food"]["per_100g"]["micronutrients"]
        assert per_100g == {
            "vitamin_d_mcg": 5,
            "iron_mg": 20,
            "potassium_mg": 350,
            "thiamin_mg": 0.75,
            "folate_mcg": 500,
        }
        out = await call(
            client,
            "log_entries",
            items=[
                {"food_id": saved["food"]["food_id"], "grams": 40},
                {"food_id": eggs, "grams": 100},
            ],
            eaten_at="now",
        )
        cereal = out["meal"]["entries"][0]["nutrients"]
        assert cereal["micronutrients"]["iron_mg"] == 8
        assert out["meal"]["entries"][1]["nutrients"]["micronutrients"] == {}
        # the eggs report no vitamins, so every total is partial
        assert out["day"]["totals"]["micronutrients"]["vitamin_d_mcg"] == 2
        assert "vitamin_d_mcg" in out["day"]["totals"]["micronutrients_partial"]
        day = await call(client, "get_day")
        assert day["totals"]["micronutrients"]["potassium_mg"] == 140


@pytest.mark.anyio
async def test_label_rejects_a_nutrient_given_twice_or_unknown(server):
    async with Client(server) as client:
        msg = await call_error(
            client,
            "save_food_from_label",
            **LABEL,
            micronutrients={"iron_mg": 1},
            micronutrients_percent_dv={"iron_mg": 10},
        )
        assert "both" in msg
        bad = await client.call_tool(
            "save_food_from_label", {**LABEL, "micronutrients": {"unobtainium_mg": 1}}
        )
        assert bad.is_error


@pytest.mark.anyio
async def test_confirm_food_adds_micros_per_serving(server, conn):
    food = add_food(conn, "Milk", 42, 3.4, 5, 1)
    async with Client(server) as client:
        out = await call(
            client,
            "confirm_food",
            food_id=food,
            micronutrients={"calcium_mg": 300},
            micronutrients_percent_dv={"vitamin_d_mcg": 15},
            per_serving_g=240,
        )
    assert out["changed"] == ["vitamin_d_mcg", "calcium_mg"]
    assert out["food"]["per_100g"]["micronutrients"] == {"vitamin_d_mcg": 1.2, "calcium_mg": 125}
    assert out["food"]["per_100g"]["kcal"] == 42


@pytest.mark.anyio
async def test_confirm_food_percent_dv_needs_the_serving(server, conn):
    food = add_food(conn, "Milk", 42, 3.4, 5, 1)
    async with Client(server) as client:
        msg = await call_error(
            client, "confirm_food", food_id=food, micronutrients_percent_dv={"thiamin_mg": 25}
        )
    assert "per_serving_g" in msg
    assert store.get_food(conn, food)["thiamin_mg"] is None


@pytest.mark.anyio
async def test_range_totals_name_partial_micros_per_day(server, conn):
    eggs = add_food(conn, "Egg, whole", *EGGS)
    milk = add_food(conn, "Milk", 42, 3.4, 5, 1)
    conn.execute("UPDATE foods SET calcium_mg = 120 WHERE id = ?", (milk,))
    store.log_meal(conn, [(eggs, 100), (milk, 100)], "2026-10-05T16:00:00Z", None)
    store.log_meal(conn, [(milk, 100)], "2026-10-06T16:00:00Z", None)
    async with Client(server) as client:
        out = await call(client, "get_totals", start="2026-10-05", end="2026-10-06")
    first, second = out["per_day"]
    assert first["totals"]["micronutrients_partial"] == ["calcium_mg"]
    assert second["totals"]["micronutrients_partial"] == []
    assert out["totals"]["micronutrients_partial"] == ["calcium_mg"]


# --- dashboard and export -------------------------------------------------------------------


def test_today_lists_vitamins_with_daily_value(http, conn, logged):  # noqa: F811
    eggs, _, _ = logged
    conn.execute("UPDATE entries SET iron_mg = 1.8, vitamin_d_mcg = 2 WHERE food_id = ?", (eggs,))
    sign_in(http)
    text = http.get("/app").text.replace("\xa0", " ")
    assert '<summary class="small">Vitamins and minerals</summary>' in text
    assert '<th scope="row">Iron</th><td>1.8 mg' in text
    assert '<td class="muted">10%</td>' in text  # 1.8 of 18 mg
    assert "real amount is higher" in text  # the egg whites don't report iron
    (entry,) = conn.execute("SELECT id FROM entries WHERE food_id = ?", (eggs,)).fetchall()
    sheet = http.get(f"/app/entries/{entry[0]}").text.replace("\xa0", " ")
    assert "Iron 1.8 mg" in sheet  # on the entry's sheet


def test_export_carries_micros(conn):
    food = add_food(conn, "Egg, whole", *EGGS)
    conn.execute("UPDATE foods SET iron_mg = 1.75 WHERE id = ?", (food,))
    store.log_meal(conn, [(food, 200)], "2026-10-05T12:00:00Z", None)
    out = io.StringIO()
    export.write_csv(conn, out, "America/New_York")
    (row,) = csv.DictReader(io.StringIO(out.getvalue()))
    assert row["iron_mg"] == "3.5"
    data = export.to_json(conn, "America/New_York")
    assert data["foods"][0]["iron_mg"] == 1.75
