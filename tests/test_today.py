"""Today's hub and its sheets (#105): the Energy card, Your day tiles, the entry, meal,
weigh-in and Log food sheets, and editing a weigh-in."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import test_web
from test_web import ORIGIN, sign_in
from test_web_edits import follow, today

from chomp import hevy, logic, store

public_settings = test_web.public_settings
http = test_web.http
logged = test_web.logged

TZ = "America/New_York"


def hours_ago(n: float) -> str:
    return logic.format_utc(datetime.now(UTC).replace(microsecond=0) - timedelta(hours=n))


# --- logic and store ------------------------------------------------------------------------


def test_week_change_needs_a_reading_that_week_and_one_before():
    d = date(2026, 10, 15)
    trend = [(d - timedelta(days=10), 80.0), (d - timedelta(days=2), 79.5)]
    assert logic.trend_on(trend, d - timedelta(days=11)) is None
    assert logic.trend_on(trend, d - timedelta(days=5)) == 80.0
    assert logic.week_change(trend, d) == pytest.approx(-0.5)
    assert logic.week_change(trend[:1], d) is None  # nothing weighed this week
    assert logic.week_change(trend[1:], d) is None  # nothing before it to compare


def test_edited_weigh_in_survives_the_phone_resending_it(conn):
    row = store.add_weight(conn, "2026-10-10T11:00:00Z", 80.0, "health")
    edited = store.update_weight(conn, row["id"], row["measured_at"], 79.0)
    assert edited["id"] == row["id"] and edited["kg"] == 79.0
    counts = store.add_weights(conn, [("2026-10-10T11:00:00Z", 80.0)], "health")
    assert counts.unchanged == 1 and store.get_weight(conn, row["id"])["kg"] == 79.0
    # chat or the app can still change it
    store.add_weights(conn, [("2026-10-10T11:00:00Z", 78.5)], "chat")
    assert store.get_weight(conn, row["id"])["kg"] == 78.5


def test_moving_a_weigh_in_leaves_the_old_time_deleted(conn):
    row = store.add_weight(conn, "2026-10-10T11:00:00Z", 80.0, "health")
    other = store.add_weight(conn, "2026-10-11T11:00:00Z", 81.0, "health")
    moved = store.update_weight(conn, row["id"], "2026-10-10T13:00:00Z", 80.2)
    assert moved["id"] != row["id"] and moved["source"] == "health"
    with pytest.raises(store.NotFound):
        store.get_weight(conn, row["id"])
    store.add_weights(conn, [("2026-10-10T11:00:00Z", 80.0)], "health")  # not brought back
    assert [w["kg"] for w in store.weights_between(conn, "2026-10-01", "2026-10-31")] == [
        80.2,
        81.0,
    ]
    # onto a time already taken: replaces that weigh-in
    onto = store.update_weight(conn, moved["id"], other["measured_at"], 80.6)
    assert onto["id"] == other["id"] and onto["kg"] == 80.6
    assert len(store.weights_between(conn, "2026-10-01", "2026-10-31")) == 1


def test_last_amounts_and_latest_workout(conn, logged):
    eggs, whites, _ = logged
    store.log_meal(conn, [(eggs, 120, True)], hours_ago(0.5), None)
    last = store.last_amounts(conn, [eggs, whites, 999])
    assert last == {eggs: (120, True), whites: (300, False)}
    assert store.latest_workout(conn, "9999") is None
    store.import_workouts(conn, [hevy.Workout("2026-10-10T22:00:00Z", "Push")], "hevy")
    assert store.latest_workout(conn, "9999")["title"] == "Push"
    assert store.latest_workout(conn, "2026-10-10T22:00:00Z") is None


# --- Today ----------------------------------------------------------------------------------


def test_today_has_energy_your_day_and_meal_sheets(http, conn, logged):
    _, _, entries = logged
    sign_in(http)
    text = http.get("/app").text.replace("\xa0", " ")
    assert '<section class="card energy"' in text and 'class="ring"' in text
    assert f'href="/app/day-menu?date={today()}" data-sheet' in text
    assert f'<a class="entry" href="/app/entries/{entries[0].id}" data-sheet>' in text
    assert f'href="/app/meals/{entries[0].meal_id}" data-sheet' in text
    # nothing from the phone or Hevy yet: the tiles say where it comes from
    assert "Connect Apple Health" in text and "Import from Hevy" in text
    assert 'href="/app/weight/new?back=%2Fapp%3Fdate%3D' in text and "Add a weigh-in" in text


def test_your_day_weight_and_training_tiles(http, conn, logged):
    for days, lb in ((9, 182.0), (1, 181.0)):
        store.add_weight(conn, hours_ago(24 * days), logic.to_kg(lb, "lb"), "health")
    store.import_workouts(conn, [hevy.Workout(hours_ago(72), "Legs")], "hevy")
    sign_in(http)
    text = http.get("/app").text.replace("\xa0", " ")
    tile = text[text.index("tone-weight") :]
    tile = tile[: tile.index("</a>")]
    assert "181<small> lb</small>" in tile and "lb</span> this week" in tile
    assert '<span class="clamp1">Legs</span>' in text and "Last workout, 3 days ago" in text
    assert 'href="/app/progress/training"' in text  # not today's: Training, not the workout


def test_entry_sheet_is_a_fragment_or_a_page(http, logged):
    _, _, entries = logged
    sign_in(http)
    part = http.get(f"/app/entries/{entries[0].id}", params={"part": "1"}).text
    assert not part.startswith("<!doctype") and 'id="entry-title"' in part
    whole = http.get(f"/app/entries/{entries[0].id}").text
    assert whole.startswith("<!doctype") and 'class="backlink"' in whole
    assert http.get("/app/entries/9999").status_code == 404


def test_save_a_logged_meal_as_a_saved_meal(http, conn, logged):
    _, _, entries = logged
    meal = entries[0].meal_id
    sign_in(http)
    assert f'action="/app/meals/{meal}/save"' in http.get(f"/app/meals/{meal}").text
    r = http.post(f"/app/meals/{meal}/save", data={"name": "Big eggs"}, headers=ORIGIN)
    assert r.headers["location"].endswith("?saved=1")
    assert "Big eggs" in [t["name"] for t, _ in store.list_templates(conn)]
    again = http.post(f"/app/meals/{meal}/save", data={"name": "big eggs"}, headers=ORIGIN)
    assert "You already have a saved meal by that name" in follow(http, again)
    blank = http.post(f"/app/meals/{meal}/save", data={"name": " "}, headers=ORIGIN)
    page = follow(http, blank)
    assert "A saved meal needs a name." in page


def test_log_food_quick_add_logs_the_last_amount(http, logged):
    sign_in(http)
    text = http.get(f"/app/add?date={today()}", params={"part": "1"}).text
    assert not text.startswith("<!doctype") and "data-sheet-full" in text
    assert 'aria-label="Log 200 g of Egg, whole again"' in text
    # typing a few letters finds foods by the start of a word
    text = http.get("/app/add", params={"date": today(), "q": "whi", "live": "1"}).text
    assert "Egg white" in text


# --- weigh-ins ------------------------------------------------------------------------------


def test_add_a_weigh_in_from_the_sheet_goes_back(http, conn):
    store.add_weight(conn, hours_ago(30), logic.to_kg(182.5, "lb"), "health")
    sign_in(http)
    sheet = http.get("/app/weight/new", params={"back": "/app", "part": "1"}).text
    assert 'value="182.5"' in sheet and 'name="back" value="/app"' in sheet
    r = http.post(
        "/app/weight",
        data={"lb": "181.8", "date": today(), "time": "07:00", "back": "/app"},
        headers=ORIGIN,
    )
    assert r.headers["location"].startswith("/app?") and "weighed=added" in r.headers["location"]
    assert "Weigh-in saved." in follow(http, r)
    # a refused one comes back to the sheet with the reason
    r = http.post(
        "/app/weight",
        data={"lb": "x", "date": today(), "time": "07:00", "back": "/app"},
        headers=ORIGIN,
    )
    assert r.headers["location"].startswith("/app/weight/new?")
    assert "Weight must be a number" in follow(http, r)


def test_edit_and_delete_a_weigh_in(http, conn):
    row = store.add_weight(conn, hours_ago(30), logic.to_kg(182.5, "lb"), "health")
    sign_in(http)
    back = "/app/progress/weight?days=30"
    page = http.get("/app/progress/weight").text
    assert f'href="/app/weight/{row["id"]}?back=' in page
    sheet = http.get(f"/app/weight/{row['id']}", params={"back": back}).text
    assert 'value="182.5"' in sheet and "Apple Health" in sheet and "data-confirm" in sheet
    local = logic.local_iso(row["measured_at"], TZ)
    r = http.post(
        f"/app/weight/{row['id']}",
        data={"lb": "180.4", "date": local[:10], "time": local[11:16], "back": back},
        headers=ORIGIN,
    )
    assert r.headers["location"].startswith(back)
    assert "Weigh-in changed." in follow(http, r)
    saved = store.latest_weight(conn)
    assert saved["id"] == row["id"] and logic.weight_out(saved["kg"])["lb"] == 180.4
    r = http.post(f"/app/weight/{row['id']}/delete", data={"back": back}, headers=ORIGIN)
    assert "Weigh-in deleted." in follow(http, r) and store.latest_weight(conn) is None
    assert http.get(f"/app/weight/{row['id']}").status_code == 303  # gone: back it goes
