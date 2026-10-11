"""Dashboard edits (#54): log food, edit and combine meals, move entries, goals, rename, undo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import test_web
from conftest import fixture_json
from starlette.testclient import TestClient
from test_auth import PUBLIC
from test_tools import SEARCH, WHITES, add_food
from test_web import ORIGIN, sign_in

from chomp import auth, logic, store, web
from chomp.server import build_server

# Shared with test_web: signed-out client, settings with sign-in on, a logged breakfast
public_settings = test_web.public_settings
http = test_web.http
logged = test_web.logged

TZ = "America/New_York"


def today() -> str:
    return logic.parse_local_date(None, TZ).isoformat()


def follow(http, r) -> str:
    assert r.status_code == 303, r.text
    return http.get(r.headers["location"]).text.replace("\xa0", " ")


def undo_from(page: str) -> str:
    start = page.index('action="/app/undo/') + len('action="')
    return page[start : page.index('"', start)]


def undo(http, page: str, back: str = "/app"):
    return http.post(undo_from(page), data={"back": back}, headers=ORIGIN)


def meal_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM meals").fetchone()[0]


# --- store ----------------------------------------------------------------------------------


def two_meals(conn):
    eggs = add_food(conn, "Egg, whole", 143, 12.6, 0.72, 9.51)
    whites = add_food(conn, "Egg white", *WHITES)
    a = store.log_meal(conn, [(eggs, 100), (whites, 100)], "2026-10-05T12:00:00Z", "breakfast")
    b = store.log_meal(conn, [(whites, 50)], "2026-10-05T16:00:00Z", None)
    return eggs, whites, a, b


def test_move_entry_between_meals_and_to_a_new_one(conn):
    _, _, a, b = two_meals(conn)
    first, second = store.meal_entries(conn, a)
    assert store.move_entry(conn, first.id, b) == (b, False)
    assert first.id in [x.id for x in store.meal_entries(conn, b)]
    new, gone = store.move_entry(conn, second.id, None, "2026-10-05T13:00:00Z")
    assert gone  # breakfast is now empty, so it goes
    assert store.get_meal(conn, new)["eaten_at"] == "2026-10-05T13:00:00Z"
    with pytest.raises(store.NotFound):
        store.get_meal(conn, a)
    with pytest.raises(ValueError):
        store.move_entry(conn, first.id, None)


def test_merge_and_delete_meals(conn):
    _, _, a, b = two_meals(conn)
    store.merge_meals(conn, b, a)
    assert len(store.meal_entries(conn, a)) == 3
    assert store.get_meal(conn, a)["label"] == "breakfast"  # the target keeps its details
    with pytest.raises(ValueError):
        store.merge_meals(conn, a, a)
    store.delete_meal(conn, a)
    assert meal_count(conn) == 0
    assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


def test_undo_restores_every_kind_of_edit(conn):
    eggs, _, a, b = two_meals(conn)
    first = store.meal_entries(conn, a)[0]

    # Each edit is captured before, made, recorded, undone, and checked against the start
    for edit in (
        lambda: store.update_entry(conn, first.id, 30, None),
        lambda: store.delete_meal(conn, a),
        lambda: store.merge_meals(conn, a, b),
        lambda: store.move_entry(conn, first.id, b),
        lambda: store.rename_food(conn, eggs, "Eggs"),
        lambda: store.set_targets(conn, {"kcal": 2000}),
    ):
        state = store.capture(conn, meals=[a, b], entries=[first.id], foods=[eggs], targets=True)
        edit()
        undo_id = store.record_undo(conn, "edit", state)
        store.undo(conn, undo_id)
        assert (
            store.capture(conn, meals=[a, b], entries=[first.id], foods=[eggs], targets=True)
            == state
        )
        with pytest.raises(store.NotFound):  # once only
            store.undo(conn, undo_id)

    # Logging a new meal: undo removes it again
    state = store.capture(conn)
    new = store.log_meal(conn, [(eggs, 10)], "2026-10-05T18:00:00Z", None)
    store.undo(conn, store.record_undo(conn, "log", state, meals=[new]))
    with pytest.raises(store.NotFound):
        store.get_meal(conn, new)
    assert meal_count(conn) == 2


def test_undo_refuses_when_something_changed_since(conn):
    eggs, _, a, _ = two_meals(conn)
    first = store.meal_entries(conn, a)[0]
    state = store.capture(conn, entries=[first.id])
    store.update_entry(conn, first.id, 30, None)
    undo_id = store.record_undo(conn, "edit", state)
    store.update_entry(conn, first.id, 40, None)  # e.g. Claude corrects it meanwhile
    with pytest.raises(store.UndoConflict):
        store.undo(conn, undo_id)
    assert store.get_entry(conn, first.id).grams == 40

    # A meal that gained an entry since it was logged isn't silently deleted
    state = store.capture(conn)
    new = store.log_meal(conn, [(eggs, 10)], "2026-10-05T18:00:00Z", None)
    undo_id = store.record_undo(conn, "log", state, meals=[new])
    store.add_to_meal(conn, new, [(eggs, 20)])
    with pytest.raises(store.UndoConflict):
        store.undo(conn, undo_id)
    assert len(store.meal_entries(conn, new)) == 2


def test_undo_records_are_pruned(conn):
    store.record_undo(conn, "old", store.capture(conn))
    conn.execute("UPDATE undo_actions SET created_at = 0")
    store.record_undo(conn, "new", store.capture(conn))
    assert [r[0] for r in conn.execute("SELECT summary FROM undo_actions")] == ["new"]


def test_macro_kcal():
    assert logic.macro_kcal({"protein_g": 180, "carbs_g": 200, "fat_g": 70}) == {
        "protein_g": 720,
        "carbs_g": 800,
        "fat_g": 630,
        "total": 2150,
    }
    assert logic.macro_kcal({"protein_g": 180, "carbs_g": None, "fat_g": 70}) is None


# --- Add food -------------------------------------------------------------------------------


def test_add_page_lists_foods_saved_meals_and_sliders(http, logged):
    sign_in(http)
    text = http.get(f"/app/add?date={today()}").text.replace("\xa0", " ")
    assert "<h1>Add food</h1>" in text and "Logging a new meal" in text
    assert "Usual breakfast" in text and "Log this meal" in text
    assert "Egg white" in text and "Egg, whole" in text
    assert 'type="range"' in text and "data-per100=" in text
    assert "1 serving" not in text  # no serving size known for these foods
    assert '<option value="new">A new meal</option>' in text
    assert "8:" in text or "AM" in text or "PM" in text  # existing meal offered in Add to
    # searching finds a portion with its usual grams first
    text = http.get("/app/add", params={"date": today(), "q": "my whites"}).text
    text = text.replace("\xa0", " ")
    assert "“my whites”" in text and 'value="300"' in text and "1 my whites · 300 g" in text


def test_log_food_as_a_new_meal_then_undo(http, conn, logged):
    _, whites, _ = logged
    sign_in(http)
    before = meal_count(conn)
    r = http.post(
        "/app/log",
        data={
            "food_id": whites,
            "grams": "150",
            "date": today(),
            "to": "new",
            "time": "07:30",
            "label": "Post-workout",
        },
        headers=ORIGIN,
    )
    page = follow(http, r)
    assert "Added 150 g Egg white." in page and "Post-workout" in page
    assert meal_count(conn) == before + 1
    meal = conn.execute("SELECT id, eaten_at, label FROM meals ORDER BY id DESC").fetchone()
    assert logic.local_iso(meal["eaten_at"], TZ)[11:16] == "07:30"
    entry = store.meal_entries(conn, meal["id"])[0]
    assert entry.grams == 150 and entry.nutrients["kcal"] == pytest.approx(78)  # 52 per 100 g
    page = follow(http, undo(http, page))
    assert "Undone: added 150 g Egg white." in page
    assert meal_count(conn) == before


def test_log_food_into_an_existing_meal(http, conn, logged):
    eggs, _, entries = logged
    meal = entries[0].meal_id
    sign_in(http)
    text = http.get(f"/app/add?date={today()}&meal={meal}").text
    assert "Adding to" in text and 'name="meal"' in text and "A new meal" not in text
    r = http.post(
        "/app/log",
        data={"food_id": eggs, "grams": "50", "date": today(), "meal": meal},
        headers=ORIGIN,
    )
    assert r.headers["location"].endswith(f"#meal-{meal}")
    assert len(store.meal_entries(conn, meal)) == 3
    # also by picking the meal from the Add to menu
    r = http.post(
        "/app/log",
        data={"food_id": eggs, "grams": "50", "date": today(), "to": meal},
        headers=ORIGIN,
    )
    assert len(store.meal_entries(conn, meal)) == 4


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"grams": "0"}, "grams"),
        ({"grams": "abc"}, "grams"),
        ({"time": ""}, "when"),
        ({"time": "25:00"}, "when"),
        ({"date": "2999-01-01"}, "when"),
        ({"food_id": "99999"}, "gone"),
        ({"to": "99999"}, "gone"),
    ],
)
def test_log_food_rejects_bad_input(http, conn, logged, fields, problem):
    _, whites, _ = logged
    sign_in(http)
    before = meal_count(conn)
    data = {"food_id": whites, "grams": "100", "date": today(), "to": "new", "time": "08:00"}
    r = http.post("/app/log", data={**data, **fields}, headers=ORIGIN)
    assert r.status_code == 303 and f"problem={problem}" in r.headers["location"]
    assert r.headers["location"].startswith("/app/add?")
    assert meal_count(conn) == before
    assert 'class="flash toast bad"' in http.get(r.headers["location"]).text


def test_log_a_saved_meal(http, conn, logged):
    sign_in(http)
    template_id = conn.execute("SELECT id FROM meal_templates").fetchone()[0]
    r = http.post(
        "/app/log-template",
        data={"template_id": template_id, "date": today(), "to": "new", "time": "09:15"},
        headers=ORIGIN,
    )
    page = follow(http, r)
    assert "Logged Usual breakfast." in page
    meal = conn.execute("SELECT id, label FROM meals ORDER BY id DESC").fetchone()
    assert meal["label"] == "usual breakfast"
    assert [x.grams for x in store.meal_entries(conn, meal["id"])] == [100]


def test_add_page_searches_usda_when_configured(public_settings, conn, logged, fdc, upstream):
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
    app = build_server(public_settings, fdc=fdc).streamable_http_app(
        host=public_settings.host, transport_security=auth.transport_security(public_settings)
    )
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as client:
        sign_in(client)
        text = client.get("/app/add", params={"date": today(), "q": "egg white"}).text
        assert "Search USDA for" in text
        text = client.get("/app/add", params={"date": today(), "q": "egg white", "usda": 1}).text
        assert "USDA FoodData Central" in text and "Unconfirmed" in text
    assert upstream.requests


def test_add_page_without_usda_says_how_to_find_new_foods(http, logged):
    sign_in(http)
    text = http.get("/app/add", params={"date": today(), "q": "zzz"}).text
    assert "None of your foods match" in text and "FDC_API_KEY" in text
    assert http.get("/app/add?date=2999-01-01").status_code == 400


# --- Entries and meals ----------------------------------------------------------------------


def test_entry_slider_preview_and_quick_picks(http, logged):
    _, _, entries = logged
    sign_in(http)
    text = http.get("/app").text.replace("\xa0", " ")
    item = text[text.index(f'id="entry-{entries[0].id}"') :]
    item = item[: item.index("</li>")]
    assert 'type="range"' in item and 'max="500"' in item
    assert 'data-set="100"' in item and 'data-set="400"' in item  # half and double
    # preview from the food's per-100 g values: 200 g eggs = 286 kcal; day 442 kcal
    assert '<span data-n="kcal">286</span>' in item
    # no calorie goal set: the default is for the Today summary only, not this line
    assert "Today after this: <span data-day>442 kcal</span>" in item
    assert "Move to another meal" in item  # a two-item meal can split


def test_move_entry_to_a_new_meal_and_back(http, conn, logged):
    _, _, entries = logged
    sign_in(http)
    r = http.post(
        f"/app/entries/{entries[1].id}/move", data={"to": "new", "time": "10:00"}, headers=ORIGIN
    )
    page = follow(http, r)
    assert "Moved Egg white to 10:00 AM." in page
    moved = store.get_entry(conn, entries[1].id)
    assert moved.meal_id != entries[0].meal_id and meal_count(conn) == 2
    # and into the original meal again; the emptied meal goes
    r = http.post(
        f"/app/entries/{entries[1].id}/move", data={"to": entries[0].meal_id}, headers=ORIGIN
    )
    assert r.status_code == 303 and meal_count(conn) == 1
    page = follow(http, undo(http, http.get(r.headers["location"]).text))
    assert "Undone" in page and meal_count(conn) == 2
    assert store.get_entry(conn, entries[1].id).meal_id == moved.meal_id


def test_edit_meal_name_time_and_note(http, conn, logged):
    _, _, entries = logged
    meal = entries[0].meal_id
    sign_in(http)
    yesterday = (logic.parse_local_date(None, TZ) - timedelta(days=1)).isoformat()
    r = http.post(
        f"/app/meals/{meal}",
        data={"label": "Brunch", "time": "11:45", "date": yesterday, "note": "at home"},
        headers=ORIGIN,
    )
    assert f"date={yesterday}" in r.headers["location"]
    page = follow(http, r)
    assert "11:45 AM" in page and "Brunch" in page and "at home" in page
    row = store.get_meal(conn, meal)
    assert (row["label"], row["note"]) == ("Brunch", "at home")
    # clearing the name; a bad time changes nothing
    http.post(
        f"/app/meals/{meal}",
        data={"label": "", "time": "11:45", "date": yesterday, "note": ""},
        headers=ORIGIN,
    )
    assert store.get_meal(conn, meal)["label"] is None
    r = http.post(f"/app/meals/{meal}", data={"time": "nope", "date": yesterday}, headers=ORIGIN)
    assert "problem=when" in r.headers["location"]
    future = (logic.parse_local_date(None, TZ) + timedelta(days=2)).isoformat()
    r = http.post(f"/app/meals/{meal}", data={"time": "08:00", "date": future}, headers=ORIGIN)
    assert "problem=when" in r.headers["location"]


def test_combine_and_delete_meals(http, conn, logged):
    eggs, _, entries = logged
    first = entries[0].meal_id
    second = store.log_meal(conn, [(eggs, 100)], logic.format_utc(datetime.now(UTC)), None)
    sign_in(http)
    text = http.get("/app").text
    assert "Combine with another meal" in text and "Delete meal" in text
    r = http.post(f"/app/meals/{second}/merge", data={"into": first}, headers=ORIGIN)
    page = follow(http, r)
    assert "Combined" in page and len(store.meal_entries(conn, first)) == 3
    page = follow(http, undo(http, page))
    assert len(store.meal_entries(conn, second)) == 1
    r = http.post(f"/app/meals/{first}/delete", headers=ORIGIN)
    page = follow(http, r)
    assert "Deleted the" in page and meal_count(conn) == 1
    follow(http, undo(http, page))
    assert len(store.meal_entries(conn, first)) == 2


def test_undo_of_delete_brings_back_the_meal(http, conn, logged):
    _, _, entries = logged
    sign_in(http)
    for entry in entries:
        page = follow(http, http.post(f"/app/entries/{entry.id}/delete", headers=ORIGIN))
    assert meal_count(conn) == 0
    page = follow(http, undo(http, page))
    assert "Undone: deleted Egg white." in page
    assert [x.id for x in store.meal_entries(conn, entries[0].meal_id)] == [entries[1].id]
    # the Undo button goes once used, and a second post does nothing
    assert 'class="undo"' not in page


def test_undo_after_a_change_from_elsewhere_is_refused(http, conn, logged):
    _, _, entries = logged
    sign_in(http)
    r = http.post(f"/app/entries/{entries[0].id}", data={"grams": "100"}, headers=ORIGIN)
    page = follow(http, r)
    store.update_entry(conn, entries[0].id, 120, None)  # Claude edits it meanwhile
    r = undo(http, page)
    assert "problem=undo" in r.headers["location"]
    assert "Couldn't undo" in http.get(r.headers["location"]).text.replace("&#x27;", "'")
    assert store.get_entry(conn, entries[0].id).grams == 120


# --- Goals and food names -------------------------------------------------------------------


def test_goals_page_calorie_bar(http, conn, logged):
    sign_in(http)
    text = http.get("/app").text
    assert "Edit goals" in text
    text = http.get("/app/targets?back=/app").text.replace("\xa0", " ")
    assert "<h1>Daily goals</h1>" in text
    assert 'type="range"' not in text and text.count('data-handle="') == 2
    # no calorie goal yet: 2,000 kcal, and protein 180 g alone isn't a split, so even thirds
    assert 'autocomplete="off" required value="2000"' in text
    assert 'name="protein_g" inputmode="decimal" autocomplete="off" required value="167"' in text
    assert 'value="74"' in text and 'value="166"' in text
    r = http.post(
        "/app/targets",
        data={"kcal": "2200", "protein_g": "180", "carbs_g": "200", "fat_g": "70", "back": "/app"},
        headers=ORIGIN,
    )
    page = follow(http, r)
    assert "Updated your daily goals." in page and "Goal 2,200" in page
    assert store.get_targets(conn) == {
        "kcal": 2200,
        "protein_g": 180,
        "carbs_g": 200,
        "fat_g": 70,
    }
    # they add up to 2,150 kcal, 50 off: fitted to the calories, keeping each macro's share
    text = http.get("/app/targets").text.replace("\xa0", " ")
    assert "added up to 2,150 kcal" in text
    assert 'value="184"' in text and 'value="204"' in text and 'value="72"' in text
    # within 25 kcal: shown as saved
    store.set_targets(conn, {"kcal": 2160})
    text = http.get("/app/targets").text.replace("\xa0", " ")
    assert 'value="180"' in text and "data-refit" not in text
    assert "<b data-g>200</b> g · <span data-pct>37</span>%" in text


def test_goals_take_what_the_bar_can_drag(http, conn, logged):
    # 20,000 kcal with protein dragged to 95%: 4,750 g
    sign_in(http)
    data = {"kcal": "20000", "protein_g": "4750", "carbs_g": "250", "fat_g": "112"}
    r = http.post("/app/targets", data=data, headers=ORIGIN)
    assert r.status_code == 303 and store.get_targets(conn)["protein_g"] == 4750


def test_goals_need_every_number(http, conn, logged):
    sign_in(http)
    r = http.post("/app/targets", data={"kcal": "2000", "protein_g": "150"}, headers=ORIGIN)
    assert r.status_code == 400 and "Carbs must be a number" in r.text
    assert store.get_targets(conn)["kcal"] is None


@pytest.mark.parametrize("value", ["-1", "0", "abc", "inf", "99999", ""])
def test_goals_reject_bad_values(http, conn, logged, value):
    sign_in(http)
    data = {"kcal": value, "protein_g": "180", "carbs_g": "200", "fat_g": "70"}
    r = http.post("/app/targets", data=data, headers=ORIGIN)
    assert r.status_code == 400 and 'role="alert"' in r.text
    assert f'value="{value}"' in r.text  # typed values stay
    assert store.get_targets(conn)["kcal"] is None


def test_goals_undo(http, conn, logged):
    sign_in(http)
    store.set_targets(conn, {"kcal": 2200, "protein_g": 180, "carbs_g": 200, "fat_g": 70})
    data = {"kcal": "1800", "protein_g": "150", "carbs_g": "150", "fat_g": "60"}
    http.post("/app/targets", data=data, headers=ORIGIN)
    assert store.get_targets(conn)["kcal"] == 1800
    page = http.get(
        "/app?done=" + str(conn.execute("SELECT max(id) FROM undo_actions").fetchone()[0])
    )
    follow(http, undo(http, page.text))
    assert store.get_targets(conn)["kcal"] == 2200


def test_rename_food(http, conn, logged):
    eggs, _, _ = logged
    sign_in(http)
    assert f'data-food="/app/foods/{eggs}" data-name="Egg, whole"' in http.get("/app/foods").text
    assert "Rename…" in http.get(f"/app/foods/{eggs}").text
    r = http.post(
        f"/app/foods/{eggs}/rename", data={"name": "  Eggs  ", "back": "/app/foods"}, headers=ORIGIN
    )
    page = follow(http, r)
    assert "Renamed Egg, whole to Eggs." in page
    assert store.get_food(conn, eggs)["name"] == "Eggs"
    assert eggs in [r["id"] for r in store.search_foods(conn, "eggs", 5)]
    follow(http, undo(http, page, "/app/foods"))
    assert store.get_food(conn, eggs)["name"] == "Egg, whole"
    r = http.post(f"/app/foods/{eggs}/rename", data={"name": " "}, headers=ORIGIN)
    assert "problem=name" in r.headers["location"]


# --- Portions (#75) -------------------------------------------------------------------------


def test_estimated_entries_show_a_tilde_until_new_grams(http, conn, logged):
    _, _, entries = logged
    store.update_entry(conn, entries[0].id, 150, None, estimated=True)
    sign_in(http)
    page = http.get("/app").text.replace("\xa0", " ")
    assert '<span class="approx" title="Estimated amount">~</span>150 g' in page
    r = http.post(f"/app/entries/{entries[0].id}", data={"grams": "120"}, headers=ORIGIN)
    assert "~</span>" not in follow(http, r)
    assert store.get_entry(conn, entries[0].id).estimated is False


def test_portions_in_food_details_add_edit_delete(http, conn, logged):
    eggs, _, _ = logged
    sign_in(http)
    here = f"/app/foods/{eggs}?back=%2Fapp%2Ffoods"
    page = http.get(here).text
    assert "Your portions" in page and "+ Add portion" in page and "None yet" in page
    assert f'name="back" value="{here}"' in page
    back = {"back": here}
    r = http.post(
        f"/app/foods/{eggs}/portions", data={"name": "1 Egg", "grams": "50", **back}, headers=ORIGIN
    )
    assert r.headers["location"].startswith(here) and r.headers["location"].endswith("#portions")
    page = follow(http, r)
    assert "Saved 1 Egg = 50 g." in page and 'id="portions"' in page  # back where it was
    # An estimated portion shows its "~"; saving a weight renames it and makes it exact
    store.save_portion(conn, eggs, "Egg", 48, estimated=True)
    page = http.get(here).text.replace("\xa0", " ")
    assert "Estimated, not weighed" in page and "~</span>48 g" in page
    r = http.post(
        f"/app/foods/{eggs}/portions",
        data={"old": "Egg", "name": "large egg", "grams": "56", **back},
        headers=ORIGIN,
    )
    assert "Saved 1 large egg = 56 g." in follow(http, r)
    [p] = store.portions_of(conn, [eggs])[eggs]
    assert (p["name"], p["grams"], p["estimated"]) == ("large egg", 56, 0)
    # Bad input comes back with a problem, not an error page
    r = http.post(
        f"/app/foods/{eggs}/portions", data={"name": "x", "grams": "0", **back}, headers=ORIGIN
    )
    assert "problem=portion" in r.headers["location"]
    r = http.post(
        f"/app/foods/{eggs}/portions/delete", data={"old": "Large Egg", **back}, headers=ORIGIN
    )
    assert "Deleted the large egg portion." in follow(http, r)
    assert store.portions_of(conn, [eggs])[eggs] == []
    r = http.post(f"/app/foods/{eggs}/portions/delete", data={"old": "egg"}, headers=ORIGIN)
    assert r.status_code == 404


def test_add_food_offers_portion_chips(http, conn, logged):
    eggs, _, _ = logged
    store.save_portion(conn, eggs, "egg", 50)
    store.save_portion(conn, eggs, "handful", 30, estimated=True)
    sign_in(http)
    text = http.get("/app/add", params={"date": today(), "q": "egg"}).text.replace("\xa0", " ")
    assert 'data-set="50">1 egg · 50 g</button>' in text
    assert 'data-set="30" data-estimated="1">1 handful · ~30 g</button>' in text
    # A search naming an estimated portion starts at it, marked estimated; so does its entry
    text = http.get("/app/add", params={"date": today(), "q": "handful"}).text
    assert 'name="estimated" value="1"' in text and 'value="30"' in text
    r = http.post(
        "/app/log",
        data={"food_id": eggs, "grams": "30", "estimated": "1", "date": today(), "time": "09:00"},
        headers=ORIGIN,
    )
    assert "~</span>30 g" in follow(http, r)


# --- Guards ---------------------------------------------------------------------------------


def test_new_routes_need_sign_in_and_same_origin(http, conn, logged):
    eggs, _, entries = logged
    meal = entries[0].meal_id
    posts = [
        ("/app/log", {"food_id": eggs, "grams": "1", "date": today(), "time": "08:00"}),
        ("/app/log-template", {"template_id": "1", "date": today(), "time": "08:00"}),
        (f"/app/entries/{entries[0].id}/move", {"to": "new", "time": "08:00"}),
        (f"/app/meals/{meal}", {"label": "x", "time": "08:00", "date": today()}),
        (f"/app/meals/{meal}/merge", {"into": meal}),
        (f"/app/meals/{meal}/delete", {}),
        ("/app/targets", {"kcal": "100"}),
        (f"/app/foods/{eggs}/rename", {"name": "x"}),
        (f"/app/foods/{eggs}/confirm", {"confirmed": "1"}),
        (f"/app/foods/{eggs}/portions", {"name": "egg", "grams": "50"}),
        (f"/app/foods/{eggs}/portions/delete", {"old": "egg"}),
        ("/app/undo/1", {}),
    ]
    for path, data in posts:
        r = http.post(path, data=data, headers=ORIGIN)
        assert r.status_code == 303 and "/app/login" in r.headers["location"], path
    assert http.get("/app/add").status_code == 303
    assert http.get("/app/targets").status_code == 303
    sign_in(http)
    evil = {"Origin": "https://evil.example"}
    for path, data in posts:
        assert http.post(path, data=data, headers=evil).status_code == 403, path
    assert meal_count(conn) == 1 and store.get_targets(conn)["kcal"] is None
    assert not store.get_food(conn, eggs)["verified"]


def test_script_is_served_and_versioned(http):
    r = http.get("/app/static/app.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert "data-amount" in r.text
    assert len(web.JS_VERSION) == 10
