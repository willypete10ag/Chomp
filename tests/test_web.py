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
    """Breakfast an hour ago (today, local time), an alias, a template and a protein target."""
    eggs = add_food(conn, "Egg, whole", *EGGS)
    whites = add_food(conn, "Egg white", *WHITES, brand="Kirkland", verified=1)
    when = logic.format_utc(datetime.now(UTC) - timedelta(hours=1))
    if logic.local_date(when, "America/New_York") != logic.parse_local_date(
        None, "America/New_York"
    ):  # just after local midnight: keep the meal on today
        when = logic.format_utc(datetime.now(UTC))
    meal = store.log_meal(conn, [(eggs, 200), (whites, 300)], when, "breakfast")
    store.save_alias(conn, "my whites", whites, 300)
    store.save_template(conn, "usual breakfast", [(eggs, 100)])
    store.set_targets(conn, {"protein_g": 180})
    entries = store.meal_entries(conn, meal)
    return eggs, whites, entries


# --- sign-in --------------------------------------------------------------------------------


def test_pages_redirect_to_sign_in(http):
    for path in ("/app", "/app/history", "/app/foods?q=egg"):
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
    assert "<script" not in r.text


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
    text = r.text
    assert "<h1>Today</h1>" in text
    assert "Breakfast" in text and "Egg, whole" in text and "Kirkland" in text
    # 200 g eggs (286 kcal) + 300 g whites (156 kcal), from the stored snapshots
    assert "442 kcal" in text
    # protein 25.2 + 32.7 = 57.9 g of 180 g
    assert "57.9g <span class='muted'>/ 180g</span>" in text
    assert "122.1g left" in text
    assert 'aria-label="Next day"' not in text  # no stepping into the future


def test_other_days_and_bad_dates(http, logged):
    sign_in(http)
    r = http.get("/app?date=2020-01-01")
    assert r.status_code == 200
    assert "Nothing logged on this day" in r.text
    assert "Jump to today" in r.text
    assert http.get("/app?date=yesterday").status_code == 200
    assert http.get("/app?date=13/2").status_code == 400


def test_edit_grams_resnapshots_from_the_food(http, conn, logged):
    _, _, entries = logged
    eggs_entry = entries[0]
    sign_in(http)
    r = http.post(f"/app/entries/{eggs_entry.id}", data={"grams": "100"}, headers=ORIGIN)
    assert r.status_code == 303
    assert r.headers["location"].endswith(f"#entry-{eggs_entry.id}")
    updated = store.get_entry(conn, eggs_entry.id)
    assert updated.grams == 100
    assert updated.nutrients["kcal"] == pytest.approx(143)


@pytest.mark.parametrize("grams", ["0", "-5", "abc", "nan", "inf", "20000", ""])
def test_edit_rejects_bad_grams(http, conn, logged, grams):
    _, _, entries = logged
    sign_in(http)
    r = http.post(f"/app/entries/{entries[0].id}", data={"grams": grams}, headers=ORIGIN)
    assert r.status_code == 400
    assert store.get_entry(conn, entries[0].id).grams == 200


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
    r = http.get("/app/history")
    assert r.status_code == 200
    assert 'class="chart"' in r.text
    assert "Over the 1 of 7 days with entries." in r.text
    assert "target 180" in r.text  # protein target line
    r = http.get("/app/history?days=30")
    assert "Over the 1 of 30 days" in r.text
    assert http.get("/app/history?days=9999").status_code == 200  # falls back to 7


def test_history_when_empty(http):
    sign_in(http)
    assert "Nothing logged in this period" in http.get("/app/history").text


def test_foods_aliases_and_templates(http, conn, logged):
    add_food(conn, "Cached FDC hit", 100, 1, 1, 1, source="fdc")  # never used: hidden
    sign_in(http)
    r = http.get("/app/foods")
    text = r.text
    assert "Egg white" in text and "Verified" in text
    assert "Cached FDC hit" not in text
    assert "“my whites”" in text and "300 g" in text
    assert "usual breakfast" in text and "143 kcal" in text
    r = http.get("/app/foods", params={"q": "whites"})
    assert "Egg white" in r.text and "Egg, whole" not in r.text
    assert "Aliases" not in r.text
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
