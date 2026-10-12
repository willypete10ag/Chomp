"""Recipes on the dashboard (#82): the book on Today, the list, a recipe, the editor."""

from __future__ import annotations

import test_web
from test_tools import add_food
from test_web import ORIGIN, sign_in
from test_web_edits import follow, today

from chomp import store

public_settings = test_web.public_settings
http = test_web.http


def chili(conn):
    beans = add_food(conn, "Black beans", 100, 7, 18, 0.5)
    beef = add_food(conn, "Ground beef", 250, 26, 0, 15)
    recipe, _ = store.save_recipe(
        conn, "Chili", 4, [(beans, 400), (beef, 500)], "Brown the beef\nAdd beans"
    )
    return beans, beef, recipe


def test_foods_has_the_book_and_the_list_shows_recipes(http, conn):
    _, _, recipe = chili(conn)
    sign_in(http)
    assert 'href="/app/recipes" aria-label="Recipes"' not in http.get("/app").text
    assert 'href="/app/recipes" aria-label="Recipes"' in http.get("/app/foods").text
    page = http.get("/app/recipes").text.replace("\xa0", " ")
    assert "Chili" in page and "412 <small>kcal" in page  # 1650 kcal / 4, half to even
    assert f'data-hold="/app/recipes/{recipe.id}/edit"' in page


def test_recipe_page_and_logging_it_in_servings(http, conn):
    _, _, recipe = chili(conn)
    sign_in(http)
    page = http.get(f"/app/recipes/{recipe.id}").text
    assert "Brown the beef" in page and "Ground beef" in page
    assert f'name="food_id" value="{recipe.food_id}"' in page
    r = http.post(
        "/app/log",
        data={
            "food_id": recipe.food_id,
            "by": "servings",
            "servings": "2",
            "grams": "",
            "date": today(),
            "time": "12:00",
        },
        headers=ORIGIN,
    )
    assert "Added 2 servings of Chili (450 g)" in follow(http, r)


def test_editor_adds_and_removes_ingredients_then_saves(http, conn):
    beans, beef, _ = chili(conn)
    sign_in(http)
    base = {"name": "Beans and beef", "servings": "2", "steps": "Cook\n\nEat"}
    page = http.post(
        "/app/recipes/draft", data={**base, "q": "beef", "do": "search"}, headers=ORIGIN
    ).text
    assert f'name="add" value="{beef}"' in page
    page = http.post(
        "/app/recipes/draft",
        data={**base, "food_id": [beans], "grams": ["200"], "add": beef},
        headers=ORIGIN,
    ).text
    assert page.count('name="grams"') == 2
    page = http.post(
        "/app/recipes/draft",
        data={**base, "food_id": [beans, beef], "grams": ["200", "100"], "remove": "0"},
        headers=ORIGIN,
    ).text
    assert page.count('name="grams"') == 1
    r = http.post(
        "/app/recipes/save",
        data={**base, "food_id": [beans, beef], "grams": ["200", "100"]},
        headers=ORIGIN,
    )
    saved = store.get_recipe(conn, "beans and beef")
    assert r.headers["location"] == f"/app/recipes/{saved.id}?saved=1"
    assert saved.steps == ["Cook", "Eat"] and saved.servings == 2


def test_editor_says_what_is_wrong(http, conn):
    beans, _, recipe = chili(conn)
    sign_in(http)
    r = http.post(
        "/app/recipes/save",
        data={"name": "Soup", "servings": "0", "food_id": [beans], "grams": ["100"]},
        headers=ORIGIN,
    )
    assert r.status_code == 400 and "Servings must be" in r.text
    r = http.post(
        "/app/recipes/save",
        data={"name": "Soup", "servings": "2"},
        headers=ORIGIN,
    )
    assert "Add at least one ingredient" in r.text
    # renaming onto another recipe's name is refused
    store.save_recipe(conn, "Soup", 2, [(beans, 100)])
    r = http.post(
        "/app/recipes/save",
        data={
            "recipe_id": recipe.id,
            "name": "soup",
            "servings": "2",
            "food_id": [beans],
            "grams": ["100"],
        },
        headers=ORIGIN,
    )
    assert r.status_code == 400 and "already have a recipe" in r.text


def test_recipe_foods_stay_out_of_foods_and_open_as_recipes(http, conn):
    _, _, recipe = chili(conn)
    sign_in(http)
    assert f'id="food-{recipe.food_id}"' not in http.get("/app/foods").text
    r = http.get(f"/app/foods/{recipe.food_id}")
    assert r.headers["location"] == f"/app/recipes/{recipe.id}"
    r = http.get(f"/app/foods/{recipe.food_id}/edit")
    assert r.headers["location"] == f"/app/recipes/{recipe.id}/edit"


def test_swipe_delete(http, conn):
    _, _, recipe = chili(conn)
    sign_in(http)
    r = http.post(f"/app/recipes/{recipe.id}/delete", headers=ORIGIN)
    assert "Deleted Chili." in follow(http, r)
    assert store.list_recipes(conn) == []


def test_posts_need_the_dashboard(http, conn):
    _, _, recipe = chili(conn)
    sign_in(http)
    assert http.post(f"/app/recipes/{recipe.id}/delete").status_code == 403
    assert len(store.list_recipes(conn)) == 1
