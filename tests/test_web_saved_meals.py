"""Saved meals on the dashboard (#60): the Foods / Meals switch, a saved meal's page, the
editor (servings or grams) and deleting one."""

from __future__ import annotations

import pytest
import test_web
from test_tools import add_food
from test_web import ORIGIN, sign_in
from test_web_edits import follow, today

from chomp import store

public_settings = test_web.public_settings
http = test_web.http


@pytest.fixture
def foods(conn):
    oats = add_food(conn, "Rolled oats", 380, 13, 68, 7, verified=1)
    conn.execute("UPDATE foods SET serving_g = 40 WHERE id = ?", (oats,))
    milk = add_food(conn, "Whole milk", 60, 3.2, 4.8, 3.3)
    template_id, _ = store.save_template(conn, "oatmeal", [(oats, 80), (milk, 250, True)])
    return oats, milk, template_id


def test_meals_side_lists_saved_meals(http, conn, foods):
    _, _, template_id = foods
    sign_in(http)
    page = http.get("/app/saved-meals").text.replace("\xa0", " ")
    assert 'href="/app/saved-meals" aria-current=page>Meals<' in page
    assert "Oatmeal" in page and "454 <small>kcal" in page  # 304 + 150
    assert f'data-hold="/app/saved-meals/{template_id}/edit"' in page
    assert 'data-confirm="Delete the saved meal Oatmeal?"' in page
    assert "+ New saved meal" in page and 'href="/app/recipes">Recipes<' in page
    assert "Oatmeal" not in http.get("/app/saved-meals", params={"q": "pizza"}).text
    assert "Oatmeal" in http.get("/app/saved-meals", params={"q": "oatmeal"}).text


def test_saved_meal_page_and_logging_it(http, conn, foods):
    _, _, template_id = foods
    sign_in(http)
    page = http.get(f"/app/saved-meals/{template_id}").text.replace("\xa0", " ")
    assert "Rolled oats" in page and "2 servings · 80 g" in page
    assert 'title="Estimated amount">~</span>250 g' in page
    assert 'type="date" name="date"' in page and "Log this meal" in page
    r = http.post(
        "/app/log-template",
        data={"template_id": template_id, "to": "new", "date": today(), "time": "08:00"},
        headers=ORIGIN,
    )
    assert "Logged Oatmeal" in follow(http, r)


def test_editor_in_servings_or_grams(http, conn, foods):
    oats, milk, template_id = foods
    sign_in(http)
    page = http.get(f"/app/saved-meals/{template_id}/edit").text
    assert 'value="2"' in page and '<option value="servings" selected>' in page
    assert 'name="est" value="250"' in page
    # adding a food with a serving size starts at 1 serving, one without at 100 g
    base = {"template_id": template_id, "name": "Oatmeal"}
    page = http.post(
        "/app/saved-meals/draft",
        data={**base, "food_id": [milk], "amount": ["250"], "unit": ["g"], "est": [""]},
        headers=ORIGIN,
    ).text
    page = http.post(
        "/app/saved-meals/draft",
        data={
            **base,
            "food_id": [milk],
            "amount": ["250"],
            "unit": ["g"],
            "est": [""],
            "add": oats,
        },
        headers=ORIGIN,
    ).text
    assert page.count('name="amount"') == 2 and 'name="amount" value="1"' in page
    r = http.post(
        "/app/saved-meals/save",
        data={
            **base,
            "name": "Big oatmeal",
            "food_id": [oats, milk],
            "amount": ["3", "250"],
            "unit": ["servings", "g"],
            "est": ["", "250"],
        },
        headers=ORIGIN,
    )
    assert "Saved Big oatmeal" in follow(http, r)
    template, items = store.get_template_by_id(conn, template_id)
    assert template["name"] == "Big oatmeal"
    assert [(i["food_id"], i["grams"], i["estimated"]) for i in items] == [
        (oats, 120, 0),
        (milk, 250, 1),  # unchanged grams stay estimated
    ]


def test_new_saved_meal_and_its_problems(http, conn, foods):
    _, milk, _ = foods
    sign_in(http)
    assert "New saved meal" in http.get("/app/saved-meals/new").text
    base = {"food_id": [milk], "amount": ["200"], "unit": ["g"], "est": [""]}
    r = http.post("/app/saved-meals/save", data={**base, "name": "OATMEAL"}, headers=ORIGIN)
    assert r.status_code == 400 and "already have a saved meal named" in r.text
    r = http.post("/app/saved-meals/save", data={"name": "Milk"}, headers=ORIGIN)
    assert r.status_code == 400 and "Add at least one food" in r.text
    r = http.post(
        "/app/saved-meals/save", data={**base, "name": "Milk", "amount": ["0"]}, headers=ORIGIN
    )
    assert r.status_code == 400 and "Grams must be more than 0" in r.text
    r = http.post("/app/saved-meals/save", data={**base, "name": "Glass of milk"}, headers=ORIGIN)
    assert "Saved Glass of milk" in follow(http, r)
    _, items = store.get_template(conn, "Glass of milk")
    assert [(i["food_id"], i["grams"], i["estimated"]) for i in items] == [(milk, 200, 0)]


def test_delete_saved_meal(http, conn, foods):
    _, _, template_id = foods
    sign_in(http)
    r = http.post(f"/app/saved-meals/{template_id}/delete", headers=ORIGIN)
    assert "Deleted Oatmeal" in follow(http, r)
    assert conn.execute("SELECT COUNT(*) FROM meal_template_items").fetchone()[0] == 0
    with pytest.raises(store.NotFound):
        store.get_template_by_id(conn, template_id)
    r = http.post(f"/app/saved-meals/{template_id}/delete", headers=ORIGIN)
    assert r.status_code == 303  # already gone: back to the list
    assert http.get(f"/app/saved-meals/{template_id}").status_code == 404


def test_a_food_deleted_while_editing_drops_out(http, conn, foods):
    _, milk, _ = foods
    honey = add_food(conn, "Honey", 304, 0.3, 82, 0)
    store.delete_food(conn, honey)  # deleted for everyone while the editor was open
    sign_in(http)
    data = {
        "name": "Sweet milk",
        "food_id": [milk, honey],
        "amount": ["200", "20"],
        "unit": ["g", "g"],
        "est": ["", ""],
    }
    r = http.post("/app/saved-meals/save", data=data, headers=ORIGIN)
    assert r.status_code == 400 and "deleted in the meantime" in r.text
    assert r.text.count('name="amount"') == 1 and "Honey" not in r.text


def test_today_note_is_marked_for_the_script(http, conn, foods):
    _, _, template_id = foods
    sign_in(http)
    page = http.get(f"/app/saved-meals/{template_id}").text
    assert f'data-today="{today()}"' in page
