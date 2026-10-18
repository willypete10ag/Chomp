"""The Foods tab's redesign (#105): rows with a … menu, the food page's Log it sheet and
#photo, saved meals swiped right to log them now, and New food's chooser."""

from __future__ import annotations

import test_web
from test_tools import add_food
from test_web import ORIGIN, sign_in
from test_web_edits import follow

from chomp import store

public_settings = test_web.public_settings
http = test_web.http


def test_food_rows_have_a_menu_and_their_page_keeps_photo_anchor(http, conn):
    oats = add_food(conn, "Rolled oats", 380, 13, 68, 7, verified=1)
    conn.execute("UPDATE foods SET saved = 1, serving_g = 40 WHERE id = ?", (oats,))
    sign_in(http)
    page = http.get("/app/foods").text
    assert f'href="/app/foods/{oats}/menu?back=%2Fapp%2Ffoods" data-sheet' in page
    assert f'data-log="/app/foods/{oats}/log?back=%2Fapp%2Ffoods"' in page
    assert "Swipe left to delete" not in page  # the hint went; the … says it instead
    food = http.get(f"/app/foods/{oats}").text
    assert 'id="photo"' in food  # the connector's /app/foods/<id>#photo link lands here
    assert f'href="/app/foods/{oats}/menu?back=' in food and "&amp;gone=%2Fapp%2Ffoods" in food
    menu = http.get(f"/app/foods/{oats}/menu", params={"part": "1"}).text
    assert not menu.startswith("<!doctype") and "Log it" in menu and "Add a photo" in menu
    assert http.get("/app/foods/999/menu").status_code == 404
    assert http.get("/app/foods/999/log").status_code == 404


def test_food_log_sheet_starts_at_one_serving_and_logs(http, conn):
    oats = add_food(conn, "Rolled oats", 380, 13, 68, 7, verified=1)
    conn.execute("UPDATE foods SET serving_g = 40 WHERE id = ?", (oats,))
    sign_in(http)
    sheet = http.get(f"/app/foods/{oats}/log", params={"part": "1"}).text
    assert '<h2 id="food-log-title">Log Rolled oats</h2>' in sheet
    assert 'name="by" value="servings" checked' in sheet and 'value="1"' in sheet
    assert 'action="/app/log"' in sheet and "A new meal" in sheet
    assert 'type="date" name="date"' in sheet and "To today" not in sheet


def test_food_log_sheet_picks_another_day(http, conn):
    oats = add_food(conn, "Rolled oats", 380, 13, 68, 7, verified=1)
    sign_in(http)
    sheet = http.get(f"/app/foods/{oats}/log", params={"part": "1", "date": "2026-10-01"}).text
    assert 'type="date" name="date" value="2026-10-01"' in sheet and 'value="12:00"' in sheet
    sheet = http.get(f"/app/foods/{oats}/log", params={"part": "1", "date": "2999-01-01"}).text
    assert 'name="date" value="2999-01-01"' not in sheet  # a future date falls back to today


def test_swiping_a_saved_meal_right_logs_it_now(http, conn):
    oats = add_food(conn, "Rolled oats", 380, 13, 68, 7)
    template_id, _ = store.save_template(conn, "oatmeal", [(oats, 80)])
    sign_in(http)
    r = http.post(
        "/app/log-template",
        data={"template_id": template_id, "to": "new", "now": "1"},
        headers=ORIGIN,
    )
    assert r.status_code == 303 and "done=" in r.headers["location"]
    assert "Logged Oatmeal" in follow(http, r)
    assert conn.execute("SELECT count(*) FROM meals WHERE label = 'oatmeal'").fetchone()[0] == 1


def test_old_scan_link_and_flows(http):
    sign_in(http)
    chooser = http.get("/app/foods/new").text
    assert "data-chooser" in chooser  # the script sends an old #scan link on to ?how=scan
    typed = http.get("/app/foods/new", params={"how": "type", "name": "Mom's chili"}).text
    assert "<h1>Type it in</h1>" in typed and 'value="Mom&#x27;s chili"' in typed
    bad = http.get("/app/foods/new", params={"barcode": "abc"})
    assert bad.status_code == 400 and "<h1>Scan a barcode</h1>" in bad.text
