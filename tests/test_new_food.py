"""#59: New food on the dashboard: barcode scan or typed number, nutrition label photo (OCR
plus a tick per value), and typed-in foods; and the label text parser."""

from __future__ import annotations

import pytest
from conftest import FIXTURES
from starlette.testclient import TestClient
from test_barcode import EGG_WHITES_GTIN, NUTELLA, UNKNOWN, chain, off  # noqa: F401
from test_web import ORIGIN, http, public_settings, sign_in  # noqa: F401

from chomp import label, store, web
from chomp.server import build_server

PHOTO = FIXTURES / "label" / "granola.png"
LABEL_VALUES = {
    "kcal": 230.0,
    "fat_g": 8.0,
    "carbs_g": 37.0,
    "fiber_g": 4.0,
    "sugar_g": 12.0,
    "protein_g": 3.0,
    "sodium_mg": 160.0,
    "sat_fat_g": 1.0,
    "trans_fat_g": 0.0,
    "cholesterol_mg": 0.0,
    "added_sugar_g": 10.0,
    "vitamin_d_mcg": 2.0,
    "calcium_mg": 260.0,
    "iron_mg": 8.0,
    "potassium_mg": 240.0,
}


@pytest.fixture
def app(settings, conn, fdc, off):  # noqa: F811
    server = build_server(settings, fdc=fdc, off=off)
    with TestClient(server.streamable_http_app(), follow_redirects=False) as client:
        yield client


def food_form(**values) -> dict:
    form = {
        "mode": "manual",
        "name": "Trail mix",
        "serving_g": "40",
        "kcal": "200",
        "protein_g": "6",
        "carbs_g": "18",
        "fat_g": "12",
    }
    return form | {k: str(v) for k, v in values.items()}


def saved(conn, response):
    assert response.status_code == 303, response.text
    assert "added=" in response.headers["location"]
    food_id = int(response.headers["location"].rsplit("added=", 1)[1])
    return store.get_food(conn, food_id)


# --- Label text parsing ---------------------------------------------------------------------

VERTICAL = """Nutrition Facts
8 servings per container
Serving size 2/3 cup (55g)
Amount per serving
Calories
230
% Daily Value*
Total Fat 8g 10%
Saturated Fat 1g 5%
Trans Fat Og
Cholesterol Omg 0%
Sodium 160mg 7%
Total Carbohydrate 37g 13%
Dietary Fiber 4g 14%
Total Sugars 12g
Includes 10g Added Sugars 20%
Protein 3g
Vitamin D 2mcg 10% Calcium 260mg 20%
lron 8mg 45% Potassium 240mg 6%
* The % Daily Value (DV) tells you how much a nutrient in a serving of food
contributes to a daily diet. 2,000 calories a day is used for general nutrition advice.
"""


def test_parse_reads_a_vertical_label():
    r = label.parse(VERTICAL)
    assert r.serving_g == 55 and r.serving_desc == "2/3 cup (55g)" and not r.serving_ml
    assert r.values == LABEL_VALUES


def test_parse_fixes_ocr_slips():
    r = label.parse(
        "Serving Size 1 bar (559)\nCalories 190 Calories from Fat 70\nTotal Fat Og\n"
        "Sodium l0mg\nTotal Carb. 2,5 g\nSugars lg\nProtein 1Og\nVitamin D 5meg\n"
    )
    assert r.serving_g == 55 and r.serving_desc == "1 bar (55g)"
    assert r.values == {
        "kcal": 190,
        "fat_g": 0,
        "sodium_mg": 10,
        "carbs_g": 2.5,
        "sugar_g": 1,
        "protein_g": 10,
        "vitamin_d_mcg": 5,
    }


def test_parse_percent_only_units_and_ml():
    r = label.parse(
        "Serving size 1 cup (240mL)\nCalories 110\nTotal Fat 2.5g\nVitamin A 10% Vitamin C 4%"
        "\nProtein 8mg\nPotassium 0.4g\n"
    )
    assert r.serving_g == 240 and r.serving_ml
    assert r.values["vitamin_a_mcg"] == 90  # 10% of the 900 mcg Daily Value
    assert r.values["vitamin_c_mg"] == 3.6
    assert r.values["potassium_mg"] == 400  # converted to the stored unit
    assert "protein_g" not in r.values  # "8mg" of protein is a misread, so left for the person


def test_parse_leaves_out_what_it_cannot_read():
    r = label.parse("Nutrition Facts\nsomething blurry\n2,000 calories a day is used")
    assert r.serving_g is None and r.values == {}


@pytest.mark.skipif(not label.available(), reason="tesseract is not installed")
def test_read_photo_runs_ocr():
    r = label.read_photo(PHOTO.read_bytes())
    assert r.serving_g == 55
    assert r.values == LABEL_VALUES


def test_read_photo_refuses_non_images():
    with pytest.raises(label.LabelError):
        label.read_photo(b"not an image")


# --- Barcode ---------------------------------------------------------------------------------


def test_new_food_page_offers_all_three_ways(app):
    r = app.get("/app/foods/new")
    assert r.status_code == 200
    assert "data-scan-start" in r.text and 'name="barcode"' in r.text
    assert 'action="/app/foods/label"' in r.text and "how=type" in r.text
    assert "/app/static/zxing.min.js?v=" in r.text and "/app/static/scan.js?v=" in r.text
    assert "mediastream:" in r.headers["content-security-policy"]
    for name in ("zxing.min.js", "scan.js"):
        js = app.get(f"/app/static/{name}")
        assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert "Add a new food" in app.get("/app/foods").text


def test_typed_barcode_found_on_off_and_saved_as_confirmed(app, conn, chain):  # noqa: F811
    r = app.get(f"/app/foods/new?barcode={NUTELLA}")
    assert r.status_code == 200
    assert "Found on Open Food Facts" in r.text and "Save as confirmed" in r.text
    assert "unverified" not in r.text  # saving here confirms it
    food = store.food_by_gtin(conn, "0" + NUTELLA)
    assert not food["verified"]
    before = store.nutrients_of(food)
    shown = web.scanned_values(food)
    form = {
        "mode": "barcode",
        "food_id": food["id"],
        "barcode": "0" + NUTELLA,
        "name": food["name"],
        "brand": food["brand"],
        **shown,
    }
    row = saved(conn, app.post("/app/foods/new", data=form))
    assert row["id"] == food["id"] and row["verified"]
    assert store.nutrients_of(row) == before  # unchanged values aren't re-rounded
    page = app.get(app.get("/app/foods").url.path + f"?q=Nutella&added={row['id']}")
    assert "(confirmed)" in page.text and "Log it" in page.text


def test_scanned_food_corrections_and_serving_change(app, conn, chain):  # noqa: F811
    app.get("/app/foods/new?barcode=041220576463")  # FDC egg whites, 46 g serving
    food = store.food_by_gtin(conn, EGG_WHITES_GTIN)
    shown = web.scanned_values(food)
    form = {"mode": "barcode", "food_id": food["id"], "name": "Egg whites", **shown}
    row = saved(conn, app.post("/app/foods/new", data=form | {"protein_g": "6"}))
    assert row["name"] == "Egg whites" and row["verified"]
    assert row["protein_g"] == pytest.approx(600 / 46, abs=0.001)
    assert row["kcal"] == food["kcal"]  # untouched
    # A new serving size restates every value against it
    row = saved(conn, app.post("/app/foods/new", data=form | {"serving_g": "100"}))
    assert row["serving_g"] == 100
    assert row["fat_g"] == pytest.approx(float(shown["fat_g"]))


def test_unknown_barcode_offers_label_and_typing_with_the_barcode(app, chain):  # noqa: F811
    r = app.get(f"/app/foods/new?barcode={UNKNOWN}")
    assert r.status_code == 200 and "Nothing found" in r.text
    assert f'name="barcode" value="{UNKNOWN}"' in r.text
    assert f"how=type&amp;barcode={UNKNOWN}" in r.text
    r = app.get(f"/app/foods/new?how=type&barcode={UNKNOWN}")
    assert f'value="{UNKNOWN}"' in r.text and "Tick Verified" in r.text


def test_bad_barcode_and_unreachable_sources(app, upstream):
    r = app.get("/app/foods/new?barcode=12345")
    assert r.status_code == 400 and "a barcode number" in r.text
    upstream.add(f"/api/v2/product/{NUTELLA}", {}, status=503)
    r = app.get(f"/app/foods/new?barcode={NUTELLA}")
    assert "be reached right now" in r.text


# --- Label photo and typed-in foods ---------------------------------------------------------


def test_label_photo_prefills_unticked_values(app, monkeypatch):
    read = label.LabelRead(serving_g=55, serving_desc="2/3 cup (55g)", values=LABEL_VALUES)
    read.text = "Nutrition Facts ..."
    monkeypatch.setattr(label, "read_photo", lambda data: read)
    r = app.post(
        "/app/foods/label",
        files={"photo": ("label.jpg", b"jpeg bytes", "image/jpeg")},
        data={"barcode": UNKNOWN},
    )
    assert r.status_code == 200
    assert "data-ticks" in r.text and 'name="ok_kcal"' in r.text and " checked" not in r.text
    assert 'name="kcal" type="number"' in r.text and 'value="230"' in r.text
    assert f'value="{UNKNOWN}"' in r.text and "What Chomp read" in r.text


def test_label_photo_errors(app, monkeypatch):
    def broken(data):
        raise label.LabelError("Couldn't read that photo. Try another one.")

    monkeypatch.setattr(label, "read_photo", broken)
    r = app.post("/app/foods/label", files={"photo": ("x.jpg", b"x", "image/jpeg")})
    assert r.status_code == 400 and "Couldn&#x27;t read that photo" in r.text
    r = app.post("/app/foods/label", data={"barcode": ""})
    assert r.status_code == 400 and "Choose a photo" in r.text


def test_label_food_confirmed_only_when_every_value_is_ticked(app, conn):
    values = food_form(mode="label", fiber_g="3")
    ticks = {f"ok_{k}": "1" for k in ("serving_g", "kcal", "protein_g", "carbs_g", "fat_g")}
    row = saved(conn, app.post("/app/foods/new", data=values | ticks))
    assert row["source"] == "label" and not row["verified"]  # fiber wasn't ticked
    row = saved(conn, app.post("/app/foods/new", data=values | ticks | {"ok_fiber_g": "1"}))
    assert row["source"] == "label" and row["verified"]
    assert row["kcal"] == 500 and row["fiber_g"] == 7.5  # per 100 g
    assert row["serving_g"] == 40


def test_typed_in_food_stays_unconfirmed_and_takes_the_barcode(app, conn):
    ticks = {f"ok_{k}": "1" for k in ("serving_g", "kcal", "protein_g", "carbs_g", "fat_g")}
    data = food_form(barcode="041220576463", brand=" Acme ") | ticks  # ticks don't count
    row = saved(conn, app.post("/app/foods/new", data=data))
    assert row["source"] == "manual" and not row["verified"]
    assert row["gtin"] == EGG_WHITES_GTIN and row["brand"] == "Acme"
    page = app.get(f"/app/foods?q=Trail&added={row['id']}")
    assert "(unconfirmed)" in page.text and 'class="swipe unconfirmed"' in page.text


def test_typed_in_food_ticked_verified_is_confirmed(app, conn):
    assert 'name="verified"' in app.get("/app/foods/new?how=type").text
    row = saved(conn, app.post("/app/foods/new", data=food_form(verified="1")))
    assert row["source"] == "manual" and row["verified"]
    # an error keeps the tick
    r = app.post("/app/foods/new", data=food_form(verified="1", name=" "))
    assert r.status_code == 400 and 'name="verified" value="1" checked' in r.text
    # only typed foods take it: a label photo still needs every value ticked
    row = saved(conn, app.post("/app/foods/new", data=food_form(mode="label", verified="1")))
    assert row["source"] == "label" and not row["verified"]


def test_confirm_a_food_by_hand_and_take_it_back(app, conn):
    row = saved(conn, app.post("/app/foods/new", data=food_form(name="Mom's chili")))
    page = app.get("/app/foods").text
    assert f'action="/app/foods/{row["id"]}/confirm"' in page and ">Confirm</button>" in page
    r = app.post(f"/app/foods/{row['id']}/confirm", data={"confirmed": "1", "back": "/app/foods"})
    assert r.status_code == 303 and store.get_food(conn, row["id"])["verified"]
    page = app.get(r.headers["location"]).text
    assert "Marked Mom&#x27;s chili as confirmed." in page and "Mark unconfirmed" in page
    assert 'class="swipe unconfirmed"' not in page
    r = app.post(f"/app/foods/{row['id']}/confirm", data={"confirmed": "0", "back": "/app/foods"})
    assert not store.get_food(conn, row["id"])["verified"]
    assert "as unconfirmed." in app.get(r.headers["location"]).text
    assert app.post("/app/foods/999/confirm", data={"confirmed": "1"}).status_code == 404


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"name": " "}, "needs a name"),
        ({"protein_g": ""}, "Fill in Protein"),
        ({"kcal": "lots"}, "Calories must be a number"),
        ({"fat_g": "-1"}, "from 0 to"),
        ({"serving_g": "0"}, "more than 0 g"),
        ({"serving_g": "10"}, "more than the serving weighs"),
        ({"barcode": "123"}, "barcode isn&#x27;t valid"),
    ],
)
def test_new_food_form_errors_keep_what_was_typed(app, conn, change, message):
    r = app.post("/app/foods/new", data=food_form(**change))
    assert r.status_code == 400 and message in r.text
    assert 'value="18"' in r.text  # the carbs box keeps what was typed
    assert conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 0


def test_new_food_needs_sign_in_and_same_origin(http, conn):  # noqa: F811
    r = http.get("/app/foods/new")
    assert r.status_code == 303 and r.headers["location"].startswith("/app/login")
    sign_in(http)
    assert http.post("/app/foods/new", data=food_form()).status_code == 403
    r = http.post("/app/foods/label", files={"photo": ("x.jpg", b"x", "image/jpeg")})
    assert r.status_code == 403
    assert saved(conn, http.post("/app/foods/new", data=food_form(), headers=ORIGIN))


# --- Foods page filters and folding sections -----------------------------------------------


def test_foods_filters_narrow_and_reorder(app, conn):
    from test_tools import add_food

    old = add_food(conn, "Oat bran", 246, 17, 66, 7, verified=1)
    eaten = add_food(conn, "Oat milk", 48, 1, 7, 2)
    new = add_food(conn, "Oat cookie", 450, 6, 65, 18)
    store.log_meal(conn, [(old, 50)], "2026-10-01T12:00:00Z", None)
    store.log_meal(conn, [(old, 50)], "2026-10-02T12:00:00Z", None)
    store.log_meal(conn, [(eaten, 200)], "2026-10-03T12:00:00Z", None)
    store.save_template(conn, "oat breakfast", [(old, 40)])

    def names(view, q=""):
        rows = store.search_foods(conn, q, 10, view) if q else store.list_used_foods(conn, 10, view)
        return [r["name"] for r in rows]

    assert names("most") == ["Oat bran", "Oat milk", "Oat cookie"]
    assert names("recent") == ["Oat milk", "Oat bran"]  # only foods this user logged
    assert names("new")[0] == "Oat cookie" and new
    assert names("confirmed") == ["Oat bran"]
    assert names("unconfirmed", "oat") == ["Oat milk", "Oat cookie"]

    page = app.get("/app/foods").text
    assert 'data-fold="meals"' in page and 'data-fold="foods"' in page and " open>" in page
    assert "aria-current=page>Most used<" in page
    page = app.get("/app/foods?view=recent").text
    assert "aria-current=page>Recent<" in page and "last logged first" in page
    assert "last Sat, Oct 3" in page and "Saved meals" not in page  # filters show foods only
    page = app.get("/app/foods?view=confirmed&q=oat").text
    assert "Oat bran" in page and "Oat milk" not in page
    assert '<input type="hidden" name="view" value="confirmed">' in page
    assert "Oat milk" in app.get("/app/foods?view=bogus").text  # unknown view: everything


def test_read_photo_refuses_huge_images_before_decoding():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("1", (8000, 8000)).save(buf, format="PNG")  # 64 M pixels, a tiny file
    with pytest.raises(label.LabelError, match="too large"):
        label.read_photo(buf.getvalue())


# --- Deleting foods -------------------------------------------------------------------------


def test_delete_food_hides_it_for_everyone_and_keeps_history(app, conn, chain):  # noqa: F811
    from test_tools import add_food

    oats = add_food(conn, "Rolled oats", 379, 13, 68, 7, verified=1)
    milk = add_food(conn, "Oat milk", 48, 1, 7, 2)
    meal = store.log_meal(conn, [(oats, 50)], "2026-10-02T12:00:00Z", None)
    store.save_portion(conn, oats, "my oats", 50)
    store.save_template(conn, "oat bowl", [(oats, 50), (milk, 200)])
    store.save_template(conn, "plain oats", [(oats, 80)])
    app.get(f"/app/foods/new?barcode={NUTELLA}")
    nutella = store.food_by_gtin(conn, "0" + NUTELLA)

    page = app.get("/app/foods").text
    assert f'action="/app/foods/{oats}/delete"' in page and "Yes, delete Rolled oats" in page
    assert 'class="swipe-action js-only"' in page and 'class="sub confirm no-js"' in page
    assert "Press and hold a food" in page
    r = app.post(f"/app/foods/{oats}/delete", data={"back": "/app/foods?view=recent"})
    assert r.status_code == 303
    assert r.headers["location"] == "/app/foods?view=recent&removed=Rolled+oats&meals=2"
    assert "Deleted Rolled oats. It left 2 saved meals." in app.get(r.headers["location"]).text

    assert "oats" not in [r["name"] for r in store.list_used_foods(conn, 10)]
    assert [r["name"] for r in store.search_foods(conn, "oats", 5)] == ["Oat milk"]
    assert store.search_portions(conn, "my oats", 5) == []
    assert conn.execute("SELECT count(*) FROM portions").fetchone()[0] == 0
    assert [t["name"] for t, _ in store.list_templates(conn)] == ["oat bowl"]  # plain one empty
    assert store.meal_entries(conn, meal)[0].nutrients["kcal"] == 189.5  # history kept

    # A deleted barcode food lets go of its barcode, and a new scan brings the record back
    app.post(f"/app/foods/{nutella['id']}/delete", data={})
    assert store.food_by_gtin(conn, "0" + NUTELLA) is None
    app.get(f"/app/foods/new?barcode={NUTELLA}")
    again = store.food_by_gtin(conn, "0" + NUTELLA)
    assert again["id"] == nutella["id"]
    assert "Nutella" in [r["name"] for r in store.list_used_foods(conn, 10)]


def test_delete_food_needs_same_origin(http, conn):  # noqa: F811
    from test_tools import add_food

    oats = add_food(conn, "Rolled oats", 379, 13, 68, 7)
    sign_in(http)
    assert http.post(f"/app/foods/{oats}/delete").status_code == 403
    assert http.post("/app/foods/999/delete", headers=ORIGIN).status_code == 404


def test_deleted_food_cannot_be_picked_again(conn):
    from test_tools import add_food

    oats = add_food(conn, "Rolled oats", 379, 13, 68, 7)
    milk = add_food(conn, "Oat milk", 48, 1, 7, 2)
    meal = store.log_meal(conn, [(oats, 50)], "2026-10-02T12:00:00Z", None)
    entry = store.meal_entries(conn, meal)[0]
    store.delete_food(conn, oats)
    for attempt in (
        lambda: store.log_meal(conn, [(oats, 10)], "2026-10-03T12:00:00Z", None),
        lambda: store.add_to_meal(conn, meal, [(oats, 10)]),
        lambda: store.save_portion(conn, oats, "bowl", 40),
        lambda: store.save_template(conn, "oats", [(oats, 10)]),
    ):
        with pytest.raises(store.NotFound):
            attempt()
    # The old entry still works: new grams re-snapshot from the deleted food
    assert store.update_entry(conn, entry.id, 100, None).nutrients["kcal"] == 379
    other = store.meal_entries(
        conn, store.log_meal(conn, [(milk, 100)], "2026-10-03T12:00:00Z", None)
    )[0]
    with pytest.raises(store.NotFound, match="was deleted"):
        store.update_entry(conn, other.id, None, oats)
