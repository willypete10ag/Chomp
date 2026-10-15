"""Day breakdown (#63): fat subtypes, cholesterol, added sugars and sugar alcohols from the
sources and tools, and the /app/breakdown page with its links from Today and History."""

from __future__ import annotations

import pytest
from mcp import Client
from test_tools import call
from test_web import http, logged, public_settings, sign_in  # noqa: F401 (fixtures)

from chomp import label, store
from chomp.server import build_server
from chomp.sources import FoodRecord
from chomp.sources import fdc as fdcmod
from chomp.sources import off as offmod


@pytest.fixture
def server(settings, conn, fdc):
    return build_server(settings, fdc=fdc)


# --- sources --------------------------------------------------------------------------------


def test_fdc_reads_fat_breakdown_cholesterol_and_sugars():
    hit = [
        {"nutrientId": 1008, "unitName": "KCAL", "value": 143},
        {"nutrientId": 1003, "unitName": "G", "value": 12.6},
        {"nutrientId": 1005, "unitName": "G", "value": 0.7},
        {"nutrientId": 1004, "unitName": "G", "value": 9.5},
        {"nutrientId": 1258, "unitName": "G", "value": 3.1},
        {"nutrientId": 1257, "unitName": "G", "value": 0.04},
        {"nutrientId": 1293, "unitName": "G", "value": 1.9},
        {"nutrientId": 1292, "unitName": "G", "value": 3.7},
        {"nutrientId": 1253, "unitName": "MG", "value": 372},
        {"nutrientId": 1235, "unitName": "G", "value": 0},
        {"nutrientId": 1086, "unitName": "G", "value": 2},
    ]
    n = fdcmod.parse_nutrients(hit)
    assert n["sat_fat_g"] == 3.1 and n["trans_fat_g"] == 0.04
    assert n["poly_fat_g"] == 1.9 and n["mono_fat_g"] == 3.7
    assert n["cholesterol_mg"] == 372
    assert n["added_sugar_g"] == 0 and n["sugar_alcohol_g"] == 2


def test_off_details_are_grams_per_100g():
    n = offmod.parse_nutrients(
        {
            "energy-kcal_100g": 400,
            "proteins_100g": 5,
            "carbohydrates_100g": 60,
            "fat_100g": 15,
            "saturated-fat_100g": 8,
            "trans-fat_100g": 0,
            "cholesterol_100g": 0.015,
            "added-sugars_100g": 20,
            "polyols_100g": 10,
        }
    )
    assert n["sat_fat_g"] == 8 and n["trans_fat_g"] == 0
    assert n["cholesterol_mg"] == pytest.approx(15)
    assert n["added_sugar_g"] == 20 and n["sugar_alcohol_g"] == 10
    assert n["poly_fat_g"] is None and n["mono_fat_g"] is None


def test_cached_food_gains_details_it_lacked(conn):
    base = {"kcal": 50, "protein_g": 1, "carbs_g": 1, "fat_g": 1}
    rec = FoodRecord(source="off", source_id="7", name="Cookie", nutrients=dict(base))
    row, _ = store.cache_record(conn, rec)
    assert row["sat_fat_g"] is None
    rec.nutrients = {**base, "sat_fat_g": 0.5, "cholesterol_mg": 10}
    row, created = store.cache_record(conn, rec)
    assert not created and row["sat_fat_g"] == 0.5 and row["cholesterol_mg"] == 10


@pytest.mark.parametrize(
    ("line", "grams"),
    [
        ("Includes 10g Added Sugars 20%", 10),
        ("Includes <1g Added Sugars 1%", 1),
        ("Includes less than 1g Added Sugars", 1),
        ("Added Sugars 4g", 4),
    ],
)
def test_label_reads_added_sugars(line, grams):
    assert label.parse(f"Total Sugars 12g\n{line}\nProtein 3g").values["added_sugar_g"] == grams


# --- tools ----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_label_tool_takes_details_and_totals_them(server, conn):
    async with Client(server) as client:
        saved = await call(
            client,
            "save_food_from_label",
            name="Protein bar",
            serving_g=50,
            kcal=200,
            protein_g=20,
            carbs_g=22,
            fat_g=7,
            sat_fat_g=3,
            trans_fat_g=0,
            cholesterol_mg=10,
            added_sugar_g=1,
            sugar_alcohol_g=8,
        )
        per_100g = saved["food"]["per_100g"]
        assert per_100g["sat_fat_g"] == 6 and per_100g["sugar_alcohol_g"] == 16
        assert per_100g["poly_fat_g"] is None
        out = await call(
            client,
            "log_entries",
            items=[{"food_id": saved["food"]["food_id"], "grams": 50}],
            eaten_at="now",
        )
        assert out["day"]["totals"]["cholesterol_mg"] == 10
        fixed = await call(client, "confirm_food", food_id=saved["food"]["food_id"], mono_fat_g=4)
        assert fixed["changed"] == ["mono_fat_g"]


# --- dashboard ------------------------------------------------------------------------------


def page(client, url: str) -> str:
    r = client.get(url)
    assert r.status_code == 200
    return r.text.replace("\xa0", " ")


def test_today_links_into_the_breakdown(http, logged):  # noqa: F811
    sign_in(http)
    text = page(http, "/app")
    assert 'class="hero" href="/app/breakdown?date=' in text
    assert "#top-protein_g" in text and "#top-fat_g" in text


def test_breakdown_shows_split_meals_top_foods_and_facts(http, conn, logged):  # noqa: F811
    eggs, _, _ = logged  # 200 g eggs (unconfirmed), 300 g Kirkland whites (confirmed)
    conn.execute(
        "UPDATE entries SET sat_fat_g = 6.2, cholesterol_mg = 744, sugar_alcohol_g = 0"
        " WHERE food_id = ?",
        (eggs,),
    )
    sign_in(http)
    text = page(http, "/app/breakdown")
    assert "<h1>Breakdown</h1>" in text
    # 58 g protein (232 kcal), 3.6 g carbs (14), 19.5 g fat (176): 55% / 3% / 42%
    assert "<strong>55%</strong>" in text and "<strong>42%</strong>" in text
    assert 'href="#top-protein_g"' in text
    assert 'data-fold="bd-meals"' in text and "Breakfast" in text
    # top protein: whites 32.7 g beat eggs 25.2 g; the eggs get the amber dot
    top = text[text.index('id="top-protein_g"') :]
    assert top.index("Egg white") < top.index("Egg, whole")
    assert '<span class="dot unsure"' in top
    # facts: saturated fat and cholesterol known only for the eggs, shown without a star
    assert "Saturated fat</span><span>6.2 g" in text
    assert "Cholesterol</span><span>744 mg" in text
    assert "*" not in text[text.index('data-fold="bd-facts"') :]
    assert "Trans fat" not in text  # no food that day reports it
    assert "Net carbs</span><span>3.6 g" in text  # sugar alcohols known (0)
    assert 'data-fold="bd-micros"' not in text  # no vitamins logged


def test_breakdown_vitamins_start_folded(http, conn, logged):  # noqa: F811
    eggs, _, _ = logged
    conn.execute("UPDATE entries SET iron_mg = 1.8 WHERE food_id = ?", (eggs,))
    sign_in(http)
    text = page(http, "/app/breakdown")
    assert '<details class="fold" data-fold="bd-micros"><summary>' in text
    assert "Iron</span><span>1.8 mg" in text and "10% DV" in text


def test_breakdown_empty_day_bad_date_and_back_link(http, logged):  # noqa: F811
    sign_in(http)
    assert "Nothing logged on this day" in page(http, "/app/breakdown?date=2020-01-01")
    assert http.get("/app/breakdown?date=nope").status_code == 400
    text = page(http, "/app/breakdown?back=/app/history%3Fdays%3D30")
    assert 'href="/app/history?days=30" aria-label="Back"' in text
    text = page(http, "/app/breakdown?back=https://evil.example/")
    assert "evil.example" not in text


def test_history_days_link_to_their_breakdown(http, logged):  # noqa: F811
    sign_in(http)
    text = page(http, "/app/progress/nutrition?days=7")
    assert 'class="small bd" href="/app/breakdown?date=' in text
    assert "back=%2Fapp%2Fprogress%2Fnutrition%3Fdays%3D7" in text


def test_breakdown_needs_sign_in(http):  # noqa: F811
    r = http.get("/app/breakdown")
    assert r.status_code in (302, 303, 307)


def test_new_food_form_asks_for_the_details(http):  # noqa: F811
    sign_in(http)
    text = page(http, "/app/foods/new?how=type")
    for key in ("sat_fat_g", "cholesterol_mg", "added_sugar_g", "sugar_alcohol_g"):
        assert f'name="{key}"' in text
    assert '<div class="vrow indent"><label for="f-sat_fat_g">' in text
