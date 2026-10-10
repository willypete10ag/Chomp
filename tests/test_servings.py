"""Logging and editing by number of servings on the dashboard (#80)."""

from __future__ import annotations

import pytest
import test_web
from test_tools import add_food
from test_web import ORIGIN, sign_in
from test_web_edits import follow, meal_count, today

from chomp import store, web

public_settings = test_web.public_settings
http = test_web.http
logged = test_web.logged


@pytest.fixture
def pizza(conn):
    food = add_food(conn, "Pizza", 266, 11.4, 33.3, 9.7)
    conn.execute(
        "UPDATE foods SET serving_g = 85.4, serving_desc = '1 slice' WHERE id = ?", (food,)
    )
    return food


def test_whole_servings():
    assert web.whole_servings(341.6, 85.4) == 4
    assert web.whole_servings(128.1, 85.4) == 1.5
    assert web.whole_servings(150, 85.4) is None
    assert web.whole_servings(10, 85.4) is None  # under half a serving
    assert web.servings_text(1) == "1 serving" and web.servings_text(1.5) == "1.5 servings"
    assert web.parse_servings("4", 85.4) == pytest.approx(341.6)
    for bad in ("0", "-1", "abc", "nan", "inf", "200"):
        with pytest.raises(ValueError):
            web.parse_servings(bad, 85.4)


def test_add_food_starts_on_one_serving(http, conn, logged, pizza):
    sign_in(http)
    page = http.get("/app/add", params={"date": today(), "q": "pizza"}).text
    item = page[page.index("Pizza") :]
    assert 'value="servings" checked' in item and "data-servings" in item
    assert 'name="servings" data-servings' in item and 'value="1"' in item
    assert "1 serving ·" not in item  # the stepper covers it


def test_food_without_serving_size_shows_grams_only(http, conn, logged):
    sign_in(http)
    page = http.get("/app/add", params={"date": today(), "q": "egg white"}).text
    assert 'name="by"' not in page and "data-servings" not in page
    assert 'name="grams" data-amount' in page


def test_log_by_servings(http, conn, logged, pizza):
    sign_in(http)
    before = meal_count(conn)
    data = {
        "food_id": pizza,
        "by": "servings",
        "servings": "4",
        "grams": "85.4",
        "estimated": "1",
        "date": today(),
        "to": "new",
        "time": "12:00",
    }
    page = follow(http, http.post("/app/log", data=data, headers=ORIGIN))
    assert "Added 4 servings of Pizza (341.6 g)." in page
    assert meal_count(conn) == before + 1
    meal = conn.execute("SELECT id FROM meals ORDER BY id DESC").fetchone()["id"]
    entry = store.meal_entries(conn, meal)[0]
    assert entry.grams == pytest.approx(341.6) and not entry.estimated


def test_log_by_grams_with_the_toggle(http, conn, logged, pizza):
    sign_in(http)
    data = {
        "food_id": pizza,
        "by": "grams",
        "servings": "4",
        "grams": "100",
        "date": today(),
        "to": "new",
        "time": "12:00",
    }
    page = follow(http, http.post("/app/log", data=data, headers=ORIGIN))
    assert "Added 100 g Pizza." in page


@pytest.mark.parametrize("servings", ["0", "abc", "500", ""])
def test_log_rejects_bad_servings(http, conn, logged, pizza, servings):
    sign_in(http)
    before = meal_count(conn)
    data = {
        "food_id": pizza,
        "by": "servings",
        "servings": servings,
        "grams": "85.4",
        "date": today(),
        "to": "new",
        "time": "12:00",
    }
    r = http.post("/app/log", data=data, headers=ORIGIN)
    assert "problem=servings" in r.headers["location"] and meal_count(conn) == before


def test_servings_for_a_food_without_a_size_count_as_grams(http, conn, logged):
    _, whites, _ = logged
    sign_in(http)
    data = {
        "food_id": whites,
        "by": "servings",
        "servings": "2",
        "grams": "100",
        "date": today(),
        "to": "new",
        "time": "12:00",
    }
    page = follow(http, http.post("/app/log", data=data, headers=ORIGIN))
    assert "Added 100 g Egg white." in page


def test_edit_by_servings_and_reopen(http, conn, logged, pizza):
    _, _, entries = logged
    meal = entries[0].meal_id
    store.add_to_meal(conn, meal, [(pizza, 85.4)])
    entry = store.meal_entries(conn, meal)[-1]
    sign_in(http)
    page = http.get("/app").text
    item = page[page.index(f'id="entry-{entry.id}"') :]
    assert 'value="servings" checked' in item
    r = http.post(
        f"/app/entries/{entry.id}", data={"by": "servings", "servings": "3"}, headers=ORIGIN
    )
    assert store.get_entry(conn, entry.id).grams == pytest.approx(256.2)
    page = follow(http, r)
    item = page[page.index(f'id="entry-{entry.id}"') :]
    assert 'value="servings" checked' in item and 'value="3"' in item
    # a weighed amount opens on Grams
    http.post(f"/app/entries/{entry.id}", data={"by": "grams", "grams": "150"}, headers=ORIGIN)
    page = http.get("/app").text
    item = page[page.index(f'id="entry-{entry.id}"') :]
    assert 'value="grams" checked' in item and 'value="1.76"' in item


def test_edit_rejects_bad_servings(http, conn, logged, pizza):
    _, _, entries = logged
    meal = entries[0].meal_id
    store.add_to_meal(conn, meal, [(pizza, 85.4)])
    entry = store.meal_entries(conn, meal)[-1]
    sign_in(http)
    r = http.post(
        f"/app/entries/{entry.id}", data={"by": "servings", "servings": "-2"}, headers=ORIGIN
    )
    assert store.get_entry(conn, entry.id).grams == pytest.approx(85.4)
    page = http.get(r.headers["location"]).text
    item = page[page.index(f'id="entry-{entry.id}"') :]
    assert item.startswith(f'id="entry-{entry.id}"><details open>')
    assert 'value="servings" checked' in item and 'value="-2"' in item
    assert 'role="alert"' in item and "Servings must be" in item
