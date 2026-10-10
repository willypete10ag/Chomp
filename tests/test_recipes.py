"""Recipes (#82): storage, per-serving nutrition, the recipe's food row, and the chat tools."""

from __future__ import annotations

import pytest
from mcp import Client
from test_tools import add_food, call, call_error

from chomp import logic, store
from chomp.server import build_server


@pytest.fixture
def server(settings, conn, fdc):
    return build_server(settings, fdc=fdc)


def test_recipe_nutrition_sums_and_splits():
    beans = {"kcal": 100, "protein_g": 7, "carbs_g": 18, "fat_g": 0.5, "fiber_g": 6}
    beef = {"kcal": 250, "protein_g": 26, "carbs_g": 0, "fat_g": 15}
    n = logic.recipe_nutrition([(beans, 400), (beef, 500)], 4)
    assert n["total"]["kcal"] == pytest.approx(400 + 1250)
    assert n["per_serving"]["kcal"] == pytest.approx(1650 / 4)
    assert n["per_serving"]["protein_g"] == pytest.approx((28 + 130) / 4)
    assert n["total_g"] == 900
    assert n["serving_g"] == 225
    assert n["per_100g"]["kcal"] == pytest.approx(1650 / 9, abs=0.001)
    # beef doesn't report fiber, so the recipe doesn't claim the beans' fiber as its whole
    assert n["total"]["fiber_g"] is None
    with pytest.raises(ValueError):
        logic.recipe_nutrition([(beans, 100)], 0)
    with pytest.raises(ValueError):
        logic.recipe_nutrition([], 2)


def test_save_recipe_creates_a_loggable_food(conn):
    beans = add_food(conn, "black beans", 100, 7, 18, 0.5)
    beef = add_food(conn, "ground beef", 250, 26, 0, 15)
    recipe, replaced = store.save_recipe(
        conn, "Chili", 4, [(beans, 400), (beef, 500)], "Brown beef\n\nAdd beans\n"
    )
    assert not replaced
    assert recipe.steps == ["Brown beef", "Add beans"]
    food = store.get_food(conn, recipe.food_id)
    assert (food["name"], food["brand"], food["verified"]) == ("Chili", "Recipe", 1)
    assert food["serving_g"] == 225
    assert logic.snapshot(store.nutrients_of(food), food["serving_g"])["kcal"] == pytest.approx(
        412.5, abs=0.01
    )
    # saving the same name again replaces it and keeps the food row
    again, replaced = store.save_recipe(conn, "chili", 6, [(beans, 400), (beef, 500)])
    assert replaced and again.id == recipe.id and again.food_id == recipe.food_id
    assert store.get_food(conn, recipe.food_id)["serving_g"] == 150
    assert store.get_recipe(conn, "my chili").id == recipe.id


def test_recipe_food_follows_its_ingredients(conn):
    beans = add_food(conn, "black beans", 100, 7, 18, 0.5)
    recipe, _ = store.save_recipe(conn, "Beans", 2, [(beans, 200)])
    store.confirm_food(conn, beans, {"kcal": 150})
    assert store.get_food(conn, recipe.food_id)["kcal"] == 150
    # the recipe's own food row is changed through the recipe only
    with pytest.raises(ValueError, match="edit the recipe"):
        store.confirm_food(conn, recipe.food_id, {"kcal": 1})
    with pytest.raises(ValueError, match="edit the recipe"):
        store.delete_food(conn, recipe.food_id)
    with pytest.raises(ValueError, match="itself"):
        store.save_recipe(conn, "Beans", 2, [(recipe.food_id, 10)])


def test_delete_recipe_keeps_logged_entries(conn):
    beans = add_food(conn, "black beans", 100, 7, 18, 0.5)
    recipe, _ = store.save_recipe(conn, "Beans", 2, [(beans, 200)])
    meal_id = store.log_meal(conn, [(recipe.food_id, 100, False)], "2026-10-10T18:00:00Z", None)
    store.delete_recipe(conn, recipe.id)
    assert store.list_recipes(conn) == []
    assert store.meal_entries(conn, meal_id)[0].nutrients["kcal"] == 100
    with pytest.raises(store.NotFound):
        store.get_food(conn, recipe.food_id, live=True)


@pytest.mark.anyio
async def test_recipe_tools(server, conn):
    beans = add_food(conn, "black beans", 100, 7, 18, 0.5)
    beef = add_food(conn, "ground beef", 250, 26, 0, 15)
    async with Client(server) as client:
        out = await call(
            client,
            "save_recipe",
            name="Chili",
            servings=4,
            ingredients=[{"food_id": beans, "grams": 400}, {"food_id": beef, "grams": 500}],
            steps=["Brown the beef", "Simmer with beans"],
        )
        recipe = out["recipe"]
        assert recipe["per_serving"]["kcal"] == 412  # 412.5 rounds half to even
        assert recipe["serving_g"] == 225
        assert [i["name"] for i in recipe["ingredients"]] == ["black beans", "ground beef"]

        found = await call(client, "get_recipe", name="chili")
        assert found["recipes"][0]["steps"] == ["Brown the beef", "Simmer with beans"]
        assert len((await call(client, "get_recipe"))["recipes"]) == 1
        assert "recipes: 'Chili'" in await call_error(client, "get_recipe", name="lasagna")

        # logged like any food, one serving being serving_g
        logged = await call(
            client,
            "log_entries",
            items=[{"food_id": recipe["food_id"], "grams": 225}],
            eaten_at="now",
        )
        assert logged["meal"]["totals"]["kcal"] == 412

        await call(client, "delete_recipe", recipe_id=recipe["recipe_id"])
        assert (await call(client, "get_recipe"))["recipes"] == []


def test_export_includes_recipes(conn):
    from chomp import export

    beans = add_food(conn, "black beans", 100, 7, 18, 0.5)
    recipe, _ = store.save_recipe(conn, "Beans", 2, [(beans, 200)], ["Soak", "Boil"])
    data = export.to_json(conn, "America/New_York")
    assert data["recipes"] == [
        {
            "name": "Beans",
            "servings": 2,
            "steps": ["Soak", "Boil"],
            "food_id": recipe.food_id,
            "ingredients": [{"food_id": beans, "grams": 200}],
        }
    ]
