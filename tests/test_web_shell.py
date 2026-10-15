"""The app shell (#105): tabs, the Log sheet, Progress and its ranges, You, Integrations,
and the old addresses that moved."""

from __future__ import annotations

import test_web
from test_web import sign_in

public_settings = test_web.public_settings
http = test_web.http
local = test_web.local


def test_tabs_and_the_log_button(http):
    sign_in(http)
    page = http.get("/app").text
    for href, label in (
        ("/app", "Today"),
        ("/app/progress", "Progress"),
        ("/app/foods", "Foods"),
        ("/app/you", "You"),
    ):
        assert f'href="{href}"' in page and f"<span>{label}</span>" in page
    assert 'class="log" href="/app/actions" data-sheet' in page
    past = http.get("/app", params={"date": "2026-10-01"}).text
    assert 'class="log" href="/app/actions?date=2026-10-01" data-sheet' in past  # logs there
    assert '<script src="/app/static/theme.js' in page  # sets the theme before paint
    assert http.get("/app/static/theme.js").status_code == 200
    assert '<svg class="icon" viewBox="0 0 24 24"' in page


def test_icons_render_on_every_new_page(http):
    sign_in(http)
    for path in ("/app/you", "/app/you/integrations", "/app/actions", "/app/progress/training"):
        page = http.get(path).text
        assert "<svg" in page and "coroutine" not in page, path


def test_log_sheet_and_its_page(http):
    sign_in(http)
    part = http.get("/app/actions", params={"part": "1"}).text
    assert not part.startswith("<!doctype") and "Log food" in part
    for target in ("/app/foods/new#scan", "/app/saved-meals", "/app/progress/weight#weigh-in"):
        assert f'href="{target}"' in part
    whole = http.get("/app/actions").text
    assert whole.startswith("<!doctype") and "Log food" in whole
    day = http.get("/app/actions", params={"part": "1", "date": "2026-10-01"}).text
    assert 'href="/app/add?date=2026-10-01"' in day


def test_progress_opens_nutrition_and_old_addresses_move(http):
    sign_in(http)
    assert http.get("/app/progress").headers["location"] == "/app/progress/nutrition"
    for old, new in (
        ("/app/history?days=30", "/app/progress/nutrition?days=30"),
        ("/app/activity?days=7", "/app/progress/activity?days=7"),
        ("/app/workouts", "/app/progress/training"),
        ("/app/weight?days=60", "/app/progress/weight?days=60"),
    ):
        r = http.get(old)
        assert (r.status_code, r.headers["location"]) == (301, new), old


def test_every_progress_area_has_the_same_ranges(http):
    sign_in(http)
    for area in ("nutrition", "weight", "activity", "training"):
        page = http.get(f"/app/progress/{area}", params={"days": "0"}).text
        assert 'aria-label="Time range"' in page, area
        for n, short in ((7, "7D"), (30, "30D"), (60, "60D"), (365, "1Y")):
            assert f'href="/app/progress/{area}?days={n}"' in page and f">{short}<" in page
        assert "?days=90" not in page
        assert ('?days=1"' in page) == (area == "activity"), area  # only Activity has Today
        assert 'aria-label="all time" aria-current=page>All<' in page, area
        assert f'href="/app/progress/{area}" aria-current=page>' in page  # its pill


def test_you_page(http):
    sign_in(http)
    page = http.get("/app/you").text
    assert 'href="/app/targets?back=%2Fapp%2Fyou"' in page
    assert 'href="/app/you/integrations#health"' in page
    assert 'href="/app/you/integrations#hevy"' in page
    assert 'href="/app/you/integrations#claude"' in page  # public: there's a connector URL
    assert "data-theme-pick" in page
    assert 'action="/app/logout"' in page and "Sign out" in page


def test_you_page_locally(local):
    page = local.get("/app/you").text
    assert "Sign out" not in page and "#claude" not in page


def test_integrations_page(http):
    sign_in(http)
    page = http.get("/app/you/integrations").text
    assert 'id="health"' in page and 'id="hevy"' in page and 'id="claude"' in page
    assert "/mcp" in page and "No workouts yet." in page


def test_log_button_keeps_the_day_on_breakdown_and_add(http):
    sign_in(http)
    for path in ("/app/breakdown", "/app/add"):
        page = http.get(path, params={"date": "2026-10-01"}).text
        assert 'class="log" href="/app/actions?date=2026-10-01" data-sheet' in page, path


def test_saved_meal_from_a_past_day_logs_to_that_day(http, conn):
    from test_tools import add_food

    from chomp import store

    oats = add_food(conn, "Rolled oats", 380, 13, 68, 7)
    template_id, _ = store.save_template(conn, "oatmeal", [(oats, 80)])
    sign_in(http)
    sheet = http.get("/app/actions", params={"part": "1", "date": "2026-10-01"}).text
    assert 'href="/app/saved-meals?date=2026-10-01"' in sheet
    meals = http.get("/app/saved-meals", params={"date": "2026-10-01"}).text
    assert f'href="/app/saved-meals/{template_id}?date=2026-10-01"' in meals
    assert '<input type="hidden" name="date" value="2026-10-01">' in meals  # kept by search
    page = http.get(f"/app/saved-meals/{template_id}", params={"date": "2026-10-01"}).text
    assert 'name="date" value="2026-10-01"' in page and "Today after this" not in page
    page = http.get(f"/app/saved-meals/{template_id}", params={"date": "bad"}).text
    assert "Today after this" in page  # a bad date falls back to today
