"""The Weight page (#94): latest weigh-in and change in lb, the line chart, adding one and
deleting one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import test_web
from test_web import ORIGIN, sign_in
from test_web_edits import follow, today

from chomp import logic, store

public_settings = test_web.public_settings
http = test_web.http


def days_ago(n: int, hour: int = 12) -> str:
    when = datetime.now(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    return logic.format_utc(when - timedelta(days=n))


def test_empty_weight_page_explains_where_weights_come_from(http):
    sign_in(http)
    page = http.get("/app/progress/weight").text
    assert 'href="/app/progress/weight" aria-current=page>Weight<' in page
    assert "No weigh-ins yet." in page and "Apple Health" in page
    assert 'class="chart weight"' not in page
    assert "Add a weigh-in" in page


def test_weight_page_shows_latest_change_chart_and_list(http, conn):
    store.add_weight(conn, days_ago(40), logic.to_kg(186, "lb"), "health")
    store.add_weight(conn, days_ago(10), logic.to_kg(184, "lb"), "health")
    store.add_weight(conn, days_ago(1), logic.to_kg(182.4, "lb"), "chat")
    sign_in(http)
    page = http.get("/app/progress/weight").text.replace("\xa0", " ")
    assert '<div class="big weight">182.4 lb</div>' in page
    assert "&minus;1.6 lb" in page and "over 30 days" in page  # 186 is outside 30 days
    assert page.count('<circle class="dot"') == 2 and '<polyline class="line"' in page
    assert page.count('class="swipe"') == 2 and "Apple Health" in page and "Claude" in page
    assert 'data-confirm="Delete the weigh-in from' in page
    page = http.get("/app/progress/weight?days=60").text.replace("\xa0", " ")
    assert page.count('<circle class="dot"') == 3 and "&minus;3.6 lb" in page
    assert http.get("/app/progress/weight?days=12345").status_code == 200  # falls back to 30
    # All: back to the first weigh-in, however long ago, labeled by month and year
    store.add_weight(conn, days_ago(800), logic.to_kg(200, "lb"), "health")
    page = http.get("/app/progress/weight?days=0").text.replace("\xa0", " ")
    first = logic.local_date(days_ago(800), "America/New_York")
    assert 'href="/app/progress/weight?days=0" aria-label="all time" aria-current=page>All<' in page
    assert page.count('<circle class="dot"') == 4 and "&minus;17.6 lb" in page
    assert f"since {first.strftime('%b')} {first.year}" in page
    assert f">{first.strftime('%b')} {first.year}</text>" in page


def test_add_and_delete_a_weigh_in(http, conn):
    sign_in(http)
    r = http.post(
        "/app/weight",
        data={"lb": "181.2", "date": today(), "time": "07:15", "days": "60"},
        headers=ORIGIN,
    )
    assert r.headers["location"].startswith("/app/progress/weight?days=60")
    page = follow(http, r)
    assert "Weigh-in saved." in page and "181.2 lb" in page
    row = store.latest_weight(conn)
    assert row["source"] == "app" and logic.weight_out(row["kg"])["lb"] == 181.2
    page = follow(http, http.post(f"/app/weight/{row['id']}/delete", headers=ORIGIN))
    assert "Weigh-in deleted." in page and store.latest_weight(conn) is None


def test_bad_weigh_ins_are_refused(http, conn):
    sign_in(http)
    for data, problem in (
        ({"lb": "abc", "date": today(), "time": "07:00"}, "Weight must be a number"),
        ({"lb": "9000", "date": today(), "time": "07:00"}, "Weight must be a number"),
        ({"lb": "180", "date": "2999-01-01", "time": "07:00"}, "Pick a date up to today"),
    ):
        page = follow(http, http.post("/app/weight", data=data, headers=ORIGIN))
        assert problem in page
    assert store.latest_weight(conn) is None
    no_origin = http.post("/app/weight", data={"lb": "180", "date": today(), "time": "07:00"})
    assert no_origin.status_code == 403
    assert http.post("/app/weight/999/delete", headers=ORIGIN).status_code == 303
