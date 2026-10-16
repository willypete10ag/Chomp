"""Web dashboard at /app: sign-in, the Today / History / Foods pages, and entry edits."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest
from starlette.testclient import TestClient
from test_auth import BEARER, CLIENT_ID, CLIENT_SECRET, PASSWORD, PUBLIC
from test_tools import EGGS, WHITES, add_food

from chomp import auth, logic, store, web
from chomp.server import build_server

ORIGIN = {"Origin": PUBLIC}


def client_for(s) -> TestClient:
    app = build_server(s).streamable_http_app(
        host=s.host, transport_security=auth.transport_security(s)
    )
    return TestClient(app, base_url=PUBLIC, follow_redirects=False)


@pytest.fixture
def public_settings(settings, conn):
    return dataclasses.replace(
        settings,
        host="0.0.0.0",
        public_url=PUBLIC,
        auth_password=PASSWORD,
        oauth_client_id=CLIENT_ID,
        oauth_client_secret=CLIENT_SECRET,
        bearer_token=BEARER,
    )


@pytest.fixture
def http(public_settings):
    with client_for(public_settings) as client:
        yield client


@pytest.fixture
def local(settings, conn):
    app = build_server(settings).streamable_http_app()
    with TestClient(app, follow_redirects=False) as client:
        yield client


def sign_in(http) -> None:
    r = http.post("/app/login", data={"password": PASSWORD, "next": "/app"}, headers=ORIGIN)
    assert r.status_code == 303


@pytest.fixture
def logged(conn):
    """Breakfast an hour ago (today, local time), a portion, a template and a protein target."""
    eggs = add_food(conn, "Egg, whole", *EGGS)
    whites = add_food(conn, "Egg white", *WHITES, brand="Kirkland", verified=1)
    when = logic.format_utc(datetime.now(UTC) - timedelta(hours=1))
    if logic.local_date(when, "America/New_York") != logic.parse_local_date(
        None, "America/New_York"
    ):  # just after local midnight: keep the meal on today
        when = logic.format_utc(datetime.now(UTC))
    meal = store.log_meal(conn, [(eggs, 200), (whites, 300)], when, "breakfast")
    store.save_portion(conn, whites, "my whites", 300)
    store.save_template(conn, "usual breakfast", [(eggs, 100)])
    store.set_targets(conn, {"protein_g": 180})
    entries = store.meal_entries(conn, meal)
    return eggs, whites, entries


# --- sign-in --------------------------------------------------------------------------------


def test_pages_redirect_to_sign_in(http):
    for path in ("/app", "/app/progress/nutrition", "/app/foods?q=egg", "/app/you"):
        r = http.get(path)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/app/login?next=")
    r = http.post("/app/entries/1", data={"grams": "5"}, headers=ORIGIN)
    assert r.status_code == 303


def test_sign_in_sets_a_hashed_session_cookie(http, conn):
    r = http.get("/app/login")
    assert r.status_code == 200 and 'name="password"' in r.text
    r = http.post(
        "/app/login", data={"password": PASSWORD, "next": "/app/history?days=30"}, headers=ORIGIN
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/history?days=30"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/app" in cookie
    assert "SameSite=lax" in cookie
    token = http.cookies[web.COOKIE]
    stored = [row[0] for row in conn.execute("SELECT token_hash FROM web_sessions")]
    assert stored == [web._hash(token)]
    assert http.get("/app").status_code == 200


def test_wrong_password_and_shared_lockout(http):
    for _ in range(auth.MAX_FAILURES - 1):
        r = http.post("/app/login", data={"password": "nope"}, headers=ORIGIN)
        assert r.status_code == 401 and "Wrong password" in r.text
    r = http.post("/app/login", data={"password": "nope"}, headers=ORIGIN)
    assert r.status_code == 429
    # Locked out even with the right password, like the connector's consent page.
    r = http.post("/app/login", data={"password": PASSWORD}, headers=ORIGIN)
    assert r.status_code == 429
    assert web.COOKIE not in http.cookies


def test_cross_site_posts_are_refused(http):
    r = http.post("/app/login", data={"password": PASSWORD})
    assert r.status_code == 403
    r = http.post(
        "/app/login", data={"password": PASSWORD}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    r = http.post(
        "/app/login", data={"password": PASSWORD}, headers={"Referer": f"{PUBLIC}/app/login"}
    )
    assert r.status_code == 303


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/app/history?days=30", "/app/history?days=30"),
        ("/app?date=2026-10-05", "/app?date=2026-10-05"),
        ("https://evil.example/app", "/app"),
        ("//evil.example/app", "/app"),
        ("/mcp", "/app"),
        ("/application", "/app"),
    ],
)
def test_sign_in_only_returns_into_the_dashboard(value, expected):
    assert web.safe_next(value) == expected


def test_sign_out_ends_the_session(http, conn):
    sign_in(http)
    token = http.cookies[web.COOKIE]
    r = http.post("/app/logout", headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == "/app/login"
    assert conn.execute("SELECT count(*) FROM web_sessions").fetchone()[0] == 0
    http.cookies.set(web.COOKIE, token, domain="chomp.example.com", path="/app")
    assert http.get("/app").status_code == 303


def test_expired_session_is_refused(http, conn):
    sign_in(http)
    conn.execute("UPDATE web_sessions SET expires_at = 0")
    assert http.get("/app").status_code == 303


def test_mcp_still_needs_its_own_token(http):
    sign_in(http)
    r = http.post("/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401


def test_local_mode_needs_no_sign_in(local):
    r = local.get("/app")
    assert r.status_code == 200
    assert "Nothing logged on this day" in r.text
    assert "Sign out" not in r.text
    assert local.get("/app/login").headers["location"] == "/app"


def test_pages_send_security_headers(http):
    sign_in(http)
    r = http.get("/app")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "'unsafe-inline'" not in r.headers["content-security-policy"]
    assert f'<script src="/app/static/app.js?v={web.JS_VERSION}" defer></script>' in r.text
    assert "<script>" not in r.text  # no inline scripts


# --- static ---------------------------------------------------------------------------------


def test_manifest_and_icons_need_no_sign_in(http):
    r = http.get("/app/manifest.webmanifest")
    assert r.status_code == 200
    manifest = json.loads(r.text)
    assert manifest["start_url"] == "/app" and manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        assert http.get(icon["src"]).headers["content-type"] == "image/png"
    assert http.get("/app/static/app.css").headers["content-type"].startswith("text/css")
    assert http.get("/app/static/../server.py").status_code == 404
    assert http.get("/app/static/nope.png").status_code == 404


# --- pages ----------------------------------------------------------------------------------


def test_today_shows_meals_totals_and_target_progress(http, logged):
    sign_in(http)
    r = http.get("/app")
    assert r.status_code == 200
    assert "\xa0g" in r.text  # numbers never wrap away from their units
    text = r.text.replace("\xa0", " ")
    assert 'data-calendar href="/app/calendar?month=' in text  # the title opens the month
    assert 'Today <span class="caret"' in text
    assert "Egg, whole" in text and "Kirkland" in text
    # the meal header leads with the time, then the label
    assert '<span class="label">· Breakfast</span></h2>' in text
    # 200 g eggs (286 kcal) + 300 g whites (156 kcal), from the stored snapshots
    # calories left against the default 2,000 kcal goal (only protein is set)
    assert '<b class="kcal ">1,558</b><span>left</span>' in text
    assert '<span class="key kcal-ate">Eaten</span><b>442</b>' in text
    assert "Goal 2,000 kcal" in text and "kcal logged" not in text
    assert "Meal total" in text  # two items: keep the combined row
    # protein 25.2 + 32.7 = 57.9 g of 180 g; carbs and fat against a third of the calories
    assert '<strong>57.9</strong> <span class="muted target">/ 180 g</span>' in text
    assert "122.1 g left" in text
    assert '<div class="small c">Carbs</div>' in text
    assert '<span class="muted target">/ 166 g</span>' in text
    assert '<span class="sr">Protein</span>' in text  # full names for screen readers
    assert 'aria-label="Next day"' not in text  # no stepping into the future
    assert 'class="week"' in text and 'aria-current="date"' in text
    assert "data-refresh" in text  # reloads when it comes back after a while (#105)
    assert '<h2 class="section-title" id="yourday-title">Your day</h2>' in text
    assert 'aria-label="Pick a date"' not in text and ">Go</button>" not in text  # #71
    assert f"/app/static/app.css?v={web.CSS_VERSION}" in text  # phones fetch new CSS


def test_today_over_the_calorie_goal_turns_amber_then_red(http, conn, logged):
    sign_in(http)
    store.set_targets(conn, {"kcal": 400})  # 442 logged: 42 over
    text = http.get("/app").text
    assert '<b class="kcal caution">42</b><span>over</span>' in text
    assert 'class="arc caution"' in text and "Goal 400 kcal" in text
    store.set_targets(conn, {"kcal": 342})  # exactly 100 over is still amber
    assert '<b class="kcal caution">100</b><span>over</span>' in http.get("/app").text
    store.set_targets(conn, {"kcal": 341})  # 101 over: red
    text = http.get("/app").text
    assert '<b class="kcal over">101</b><span>over</span>' in text
    assert 'class="arc over"' in text


def test_fiber_sugar_sodium_fold_away(http, conn, logged):
    eggs, _, _ = logged
    conn.execute("UPDATE entries SET sodium_mg = 140 WHERE food_id = ?", (eggs,))
    sign_in(http)
    text = http.get("/app").text.replace("\xa0", " ")
    assert '<details class="extras morenut">' in text and "Sodium 140 mg" in text


def test_one_item_meal_shows_its_numbers_once(http, conn, logged):
    eggs, _, _ = logged
    when = logic.format_utc(datetime.now(UTC))
    meal = store.log_meal(conn, [(eggs, 100)], when, None)
    sign_in(http)
    text = http.get("/app").text
    card = text[text.index(f'id="meal-{meal}"') :]
    card = card[: card.index("</section>")]
    assert "Meal total" not in card
    row = card[card.index('<a class="entry"') : card.index("</a>")]
    assert "kcal" not in row  # the header carries the calories
    assert 'class="label"' not in card  # no guessed label


def test_other_days_and_bad_dates(http, logged):
    sign_in(http)
    r = http.get("/app?date=2020-01-01")
    assert r.status_code == 200
    assert "Nothing logged on this day" in r.text
    assert "/app/calendar?month=2020-01&amp;date=2020-01-01" in r.text.replace("&", "&amp;")
    assert http.get("/app?date=yesterday").status_code == 200
    assert http.get("/app?date=13/2").status_code == 400
    for edge in ("9999-12-31", "0001-01-01"):
        assert http.get(f"/app?date={edge}").status_code == 400


def test_month_view_marks_logged_and_near_goal_days(http, conn, logged):
    eggs, _, _ = logged
    now = logic.parse_local_date(None, "America/New_York")
    month = now.replace(day=1)
    sign_in(http)
    r = http.get(f"/app/calendar?month={month:%Y-%m}")
    assert r.status_code == 200
    text = r.text
    assert f"<h2>{month:%B}</h2>" in text and 'class="years"' in text
    today = text[text.index('class="is-today"') :]
    today = today[: today.index("</a>")]
    assert 'aria-current="date"' in today and 'class="dot"' in today  # logged, not near goals
    assert 'aria-label="Next month"' not in text  # no months past this one
    assert 'aria-label="Previous month"' not in text  # nothing logged before this month
    assert ">Today</a>" in text and 'href="/app"' in text

    # With the goals set to the day's own totals, it is within 10% of each: a star
    totals = logic.sum_nutrients(
        x.nutrients
        for m in store.meals_between(conn, *logic.local_day_bounds(now, "America/New_York"))
        for x in store.meal_entries(conn, m["id"])
    )
    store.set_targets(conn, {k: round(totals[k]) for k in logic.CORE})
    text = http.get(f"/app/calendar?month={month:%Y-%m}&part=1").text
    assert text.startswith('<div class="calendar"')  # just the calendar, for the sheet
    today = text[text.index('class="is-today"') :]
    today = today[: today.index("</a>")]
    assert 'class="star"' in today and "near your goals" in today

    # An older meal opens earlier months; the year box lists each year back to it
    old = logic.format_utc(datetime(2024, 3, 5, 17, tzinfo=UTC))
    store.log_meal(conn, [(eggs, 100)], old, None)
    text = http.get(f"/app/calendar?month={month:%Y-%m}").text
    assert 'aria-label="Previous month"' in text
    for y in range(2024, now.year + 1):
        assert f">{y}</a>" in text
    text = http.get("/app/calendar?month=2024-03&date=2024-03-05").text
    assert 'aria-label="Previous month"' not in text  # the first logged month
    assert '<a href="/app?date=2024-03-05" aria-current="date"' in text
    assert "Tuesday, March 5, 2024, logged" in text
    # Out of range months clamp; bad ones are refused; future days can't be picked
    assert "<h2>March</h2>" in http.get("/app/calendar?month=1999-01").text
    assert http.get("/app/calendar?month=nope").status_code == 400
    future = http.get(f"/app/calendar?month={now.year + 3}-01").text
    assert f"<h2>{month:%B}</h2>" in future


def test_near_goals():
    goals = {"kcal": 2000, "protein_g": 150, "carbs_g": 200, "fat_g": 70}
    day = {"kcal": 1850, "protein_g": 160, "carbs_g": 185, "fat_g": 75}
    assert logic.near_goals(day, goals)
    assert not logic.near_goals({**day, "fat_g": 78}, goals)  # 11% over
    assert not logic.near_goals({**day, "kcal": 1790}, goals)  # 10.5% under
    # No goals set: measured against 2,000 kcal and thirds, like Today
    thirds = logic.with_default_targets({})
    assert logic.near_goals(thirds, {}) and not logic.near_goals({"kcal": 2000}, {})


def test_edit_grams_resnapshots_from_the_food(http, conn, logged):
    _, _, entries = logged
    eggs_entry = entries[0]
    sign_in(http)
    r = http.post(f"/app/entries/{eggs_entry.id}", data={"grams": "100"}, headers=ORIGIN)
    assert r.status_code == 303
    assert r.headers["location"].endswith(f"#entry-{eggs_entry.id}")
    page = http.get(r.headers["location"]).text
    assert "Changed Egg, whole from 200 g to 100 g." in page
    assert 'class="undo"' in page
    updated = store.get_entry(conn, eggs_entry.id)
    assert updated.grams == 100
    assert updated.nutrients["kcal"] == pytest.approx(143)


@pytest.mark.parametrize("grams", ["0", "-5", "abc", "nan", "inf", "20000", ""])
def test_edit_rejects_bad_grams(http, conn, logged, grams):
    _, _, entries = logged
    sign_in(http)
    r = http.post(f"/app/entries/{entries[0].id}", data={"grams": grams}, headers=ORIGIN)
    assert r.status_code == 303
    assert store.get_entry(conn, entries[0].id).grams == 200
    # back on the entry's sheet with the typed value and the reason
    assert r.headers["location"].startswith(f"/app/entries/{entries[0].id}?error=1")
    page = http.get(r.headers["location"]).text
    assert 'role="alert"' in page and (f'value="{grams}"' in page or not grams)


def test_delete_is_on_the_entry_sheet_and_undoable(http, logged):
    _, _, entries = logged
    sign_in(http)
    sheet = http.get(f"/app/entries/{entries[0].id}").text
    assert f'action="/app/entries/{entries[0].id}/delete"' in sheet and "data-confirm" not in sheet
    r = http.post(f"/app/entries/{entries[0].id}/delete", headers=ORIGIN)
    page = http.get(r.headers["location"]).text
    assert "Deleted Egg, whole." in page and 'class="undo"' in page


def test_edit_and_delete_refuse_cross_site_posts(http, conn, logged):
    _, _, entries = logged
    sign_in(http)
    evil = {"Origin": "https://evil.example"}
    assert (
        http.post(f"/app/entries/{entries[0].id}", data={"grams": "1"}, headers=evil).status_code
        == 403
    )
    assert http.post(f"/app/entries/{entries[0].id}/delete", headers=evil).status_code == 403
    assert store.get_entry(conn, entries[0].id).grams == 200


def test_delete_entry_and_then_its_meal(http, conn, logged):
    _, _, entries = logged
    sign_in(http)
    for entry in entries:
        r = http.post(f"/app/entries/{entry.id}/delete", headers=ORIGIN)
        assert r.status_code == 303
    assert conn.execute("SELECT count(*) FROM meals").fetchone()[0] == 0
    r = http.post(f"/app/entries/{entries[0].id}/delete", headers=ORIGIN)
    assert r.status_code == 404


def test_history_charts_and_averages(http, logged):
    sign_in(http)
    r = http.get("/app/progress/nutrition")
    assert r.status_code == 200
    page = r.text.replace("\xa0", " ")
    assert page.count("data-chart") == 2 and 'class="chart bars kcal"' in page
    # Only today is logged, so the tiles say so and its bars are hatched as "so far"
    assert "Days logged" in page and "of 7 days · today so far" in page
    assert "goal 180 g" in page  # protein goal line
    assert "goal 2,000" not in page  # no calorie goal set: no default line here
    assert 'class="bar protein_g partial"' in page and 'fill="url(#hatch-protein_g' in page
    assert "so far" in page and "Show as table" in page
    assert '"href":"/app?date=' in page.replace("&quot;", '"')  # a pinned day opens it
    r = http.get("/app/progress/nutrition?days=30")
    assert "of 30 days" in r.text and "7-day average line" in r.text
    assert 'aria-label="30 days" aria-current=page>30D<' in r.text
    assert "Compare" in r.text  # 30D has a previous 30 days to show as ghosts
    assert http.get("/app/progress/nutrition?days=9999").status_code == 200  # falls back to 7
    # All runs from the first meal, with no previous period; the old address moves you on
    page = http.get("/app/progress/nutrition?days=0").text
    assert "Days logged" in page and "vs prev" not in page and "data-compare" not in page
    r = http.get("/app/history?days=30")
    assert r.status_code == 301 and r.headers["location"] == "/app/progress/nutrition?days=30"


def test_history_when_empty(http):
    sign_in(http)
    assert "Nothing logged in this period" in http.get("/app/progress/nutrition").text


def test_foods_and_templates(http, conn, logged):
    add_food(conn, "Cached FDC hit", 100, 1, 1, 1, source="fdc")  # never used: hidden
    sign_in(http)
    r = http.get("/app/foods")
    text = r.text.replace("\xa0", " ")
    assert "Egg white" in text and "Confirmed" in text
    assert "logged 1&times;" not in text  # on the food's page now (#72)
    assert "Cached FDC hit" not in text
    assert "Usual breakfast" not in text  # on the Meals side now (#60)
    assert 'href="/app/foods" aria-current=page>Foods<' in text and ">Meals</a>" in text
    assert text.count('class="swipe unconfirmed"') == 1 and "Unconfirmed" in text  # eggs only
    assert "143 <small>kcal</small>" in text  # eggs per 100 g, with its unit
    r = http.get("/app/foods", params={"q": "whites"})
    assert "Egg white" in r.text and "Egg, whole" not in r.text
    assert "Nicknames" not in r.text
    assert "Clear" in r.text
    r = http.get("/app/foods", params={"q": "breakfast"})
    assert "Usual breakfast" not in r.text and "Nothing in your foods matches" in r.text
    r = http.get("/app/foods", params={"q": "zzz"})
    assert "Nothing in your foods matches" in r.text
    r = http.get("/app/foods", params={"q": "<script>"})
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text


# --- formatting -----------------------------------------------------------------------------


def test_number_formatting():
    assert web.num(1234.0) == "1,234"
    assert web.num(12.0, 1) == "12"
    assert web.num(12.25, 1) == "12.2"
    assert web.num(None) == "&ndash;"
    assert web.clock("2026-10-05T08:05:00-04:00") == "8:05 AM"
    assert web.clock("2026-10-05T00:30:00-04:00") == "12:30 AM"
    assert web.clock("2026-10-05T13:00:00-04:00") == "1:00 PM"


def test_food_details_show_serving_and_extra_nutrients(http, conn, logged):
    _, whites, _ = logged
    conn.execute(
        "UPDATE foods SET serving_g = 46, serving_desc = '3 tbsp', sodium_mg = 166 WHERE id = ?",
        (whites,),
    )
    sign_in(http)
    text = http.get("/app/foods").text
    link = f'href="/app/foods/{whites}?back=%2Fapp%2Ffoods"'
    assert link in text  # a tap opens the food's page (#72)
    text = http.get(f"/app/foods/{whites}?back=/app/foods").text.replace("\xa0", " ")
    # 52 kcal per 100 g -> 23.92 kcal in a 46 g serving
    assert '<span class="big kcal">24</span> kcal' in text
    assert "per serving · 3 tbsp (46 g)" in text
    assert '<th scope="row">Sodium</th><td>76 mg</td><td>166 mg</td>' in text
    assert "Mark unconfirmed" in text and "Logged 1 time, last" in text
    assert "Source: " in text and 'href="/app/foods"' in text
    assert http.get("/app/foods/9999").status_code == 404
