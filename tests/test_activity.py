"""Activity from Apple Health (#94): steps, energy, distance, exercise minutes and flights
posted to /ingest/health, the get_activity tool, the Activity page and the export."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import test_web
from mcp import Client
from starlette.testclient import TestClient
from test_web import sign_in

from chomp import export, health, logic, store, users
from chomp.server import build_server

TZ = "America/New_York"
public_settings = test_web.public_settings
http = test_web.http


def hae(*metrics):
    """A Health Auto Export payload from (name, units, [(date, qty), ...])."""
    return {
        "data": {
            "metrics": [
                {"name": n, "units": u, "data": [{"date": d, "qty": q} for d, q in readings]}
                for n, u, readings in metrics
            ]
        }
    }


def local_day(n: int = 0) -> date:
    return logic.parse_local_date(None, TZ) - timedelta(days=n)


def midnight(n: int = 0) -> str:
    """Local midnight n days ago in UTC storage format (a day bucket's start)."""
    return logic.local_day_bounds(local_day(n), TZ)[0]


# --- logic ----------------------------------------------------------------------------------


def test_units_convert_to_stored_units():
    assert logic.to_activity("steps", 8123, "count") == 8123
    assert logic.to_activity("steps", 8123, None) == 8123
    assert logic.to_activity("active_kcal", 418.4, "kJ") == pytest.approx(100)
    assert logic.to_activity("active_kcal", 400, "Cal") == 400
    assert logic.to_activity("distance_m", 2, "mi") == pytest.approx(3218.688)
    assert logic.to_activity("exercise_min", 1.5, "hr") == 90
    for metric, value, unit in (
        ("steps", 10, "kcal"),
        ("distance_m", 3, None),
        ("steps", -1, "count"),
        ("steps", 300_000, "count"),
        ("exercise_min", float("inf"), "min"),
    ):
        with pytest.raises(ValueError):
            logic.to_activity(metric, value, unit)


def test_days_sum_their_buckets_and_average_over_days_with_data():
    def row(metric, period, start_at, qty, sent="2026-10-14 12:00:00"):
        return {
            "metric": metric,
            "period": period,
            "start_at": start_at,
            "qty": qty,
            "updated_at": sent,
        }

    rows = [
        row("steps", "part", "2026-10-13T04:00:00Z", 1000),  # Oct 13 local
        row("steps", "part", "2026-10-13T15:00:00Z", 500),
        row("steps", "part", "2026-10-14T03:30:00Z", 200),  # still Oct 13
        row("steps", "day", "2026-10-14T04:00:00Z", 3000),
        row("active_kcal", "day", "2026-10-14T04:00:00Z", 300),
        row("resting_kcal", "day", "2026-10-14T04:00:00Z", 1700),
    ]
    days = logic.activity_by_day(rows, TZ)
    assert days == {
        date(2026, 10, 13): {"steps": 1700},
        date(2026, 10, 14): {"steps": 3000, "active_kcal": 300, "resting_kcal": 1700},
    }
    assert logic.activity_average(days) == {
        "steps": 2350,
        "active_kcal": 300,
        "resting_kcal": 1700,
    }
    assert logic.burned_kcal(days[date(2026, 10, 14)]) == 2000
    assert logic.burned_kcal(days[date(2026, 10, 13)]) is None


def test_a_days_whole_total_and_its_hours_never_add_up():
    """Switching between a whole-day sender (the Shortcut) and an hourly one (Health Auto
    Export by hour): the kind sent last counts for that day, not both."""
    hours = [
        {
            "metric": "steps",
            "period": "part",
            "start_at": f"2026-10-13T{h}:00:00Z",
            "qty": 1000,
            "updated_at": "2026-10-13 20:00:00",
        }
        for h in (4, 12, 18)
    ]
    whole = {
        "metric": "steps",
        "period": "day",
        "start_at": "2026-10-13T04:00:00Z",
        "qty": 2500,
        "updated_at": "2026-10-13 21:00:00",
    }
    assert logic.activity_by_day([*hours, whole], TZ) == {date(2026, 10, 13): {"steps": 2500}}
    later = {**hours[0], "updated_at": "2026-10-13 22:00:00"}
    assert logic.activity_by_day([later, *hours[1:], whole], TZ) == {
        date(2026, 10, 13): {"steps": 3000}
    }
    tie = {**whole, "updated_at": "2026-10-13 20:00:00"}
    assert logic.activity_by_day([*hours, tie], TZ) == {date(2026, 10, 13): {"steps": 2500}}


# --- payloads -------------------------------------------------------------------------------


def test_parses_health_auto_export_activity():
    parsed = health.parse(
        hae(
            ("step_count", "count", [("2026-10-13 00:00:00 -0400", 8123)]),
            ("active_energy", "kJ", [("2026-10-13 00:00:00 -0400", 1673.6)]),
            ("Walking + Running Distance", "mi", [("2026-10-13 00:00:00 -0400", 3.8)]),
            ("apple_exercise_time", "min", [("2026-10-13 00:00:00 -0400", 34)]),
            ("flights_climbed", "count", [("2026-10-13 00:00:00 -0400", 12)]),
            ("basal_energy_burned", "kcal", [("2026-10-13 00:00:00 -0400", 1750)]),
            ("heart_rate", "count/min", [("2026-10-13 00:00:00 -0400", 60)]),
        ),
        TZ,
    )
    day = "2026-10-13T04:00:00Z"
    assert parsed.activity == {
        ("steps", "day", day): 8123,
        ("active_kcal", "day", day): pytest.approx(400),
        ("distance_m", "day", day): pytest.approx(3.8 * logic.M_PER_MILE),
        ("exercise_min", "day", day): 34,
        ("flights", "day", day): 12,
        ("resting_kcal", "day", day): 1750,
    }
    assert parsed.ignored == {"heart_rate": 1} and parsed.rejected == 0


def test_hourly_readings_are_parts_of_a_day():
    parsed = health.parse(
        hae(
            (
                "step_count",
                "count",
                [("2026-10-13 00:00:00 -0400", 40), ("2026-10-13 07:00:00 -0400", 900)],
            ),
            ("flights_climbed", "count", [("2026-10-13 00:00:00 -0400", 2)]),
        ),
        TZ,
    )
    assert parsed.activity == {
        ("steps", "part", "2026-10-13T04:00:00Z"): 40,
        ("steps", "part", "2026-10-13T11:00:00Z"): 900,
        ("flights", "day", "2026-10-13T04:00:00Z"): 2,
    }


def test_simple_lists_take_a_bare_date_as_that_days_total():
    parsed = health.parse(
        {
            "steps": [{"date": "2026-10-13", "value": 8123}],
            "distance": {"date": "2026-10-13", "value": 6.1, "unit": "km"},
            "weight": [{"date": "2026-10-13T07:00", "value": 180, "unit": "lb"}],
        },
        TZ,
    )
    assert parsed.activity == {
        ("steps", "day", "2026-10-13T04:00:00Z"): 8123,
        ("distance_m", "day", "2026-10-13T04:00:00Z"): pytest.approx(6100),
    }
    assert list(parsed.weights) == ["2026-10-13T11:00:00Z"]


def test_parses_a_shortcuts_flat_fields():
    parsed = health.parse(
        {
            "date": "2026-10-13",
            "steps": "8,123",
            "active_kcal": 412.6,
            "resting_kcal": "",  # Health had none
            "exercise_min": "34",
            "distance_mi": 3.8,
            "flights": None,
            "weight_lb": "182.4",
            "weight_date": "2026-10-13T07:02:11-04:00",
        },
        TZ,
    )
    day = "2026-10-13T04:00:00Z"
    assert parsed.activity == {
        ("steps", "day", day): 8123,
        ("active_kcal", "day", day): 412.6,
        ("exercise_min", "day", day): 34,
        ("distance_m", "day", day): pytest.approx(3.8 * logic.M_PER_MILE),
    }
    assert parsed.weights == {"2026-10-13T11:02:11Z": pytest.approx(82.736, abs=1e-3)}
    assert parsed.rejected == 0
    # Nothing in Health today is still a valid send
    assert health.parse({"date": "2026-10-13", "steps": ""}, TZ).activity == {}


def test_flat_fields_read_health_samples_inserted_as_text():
    # A Shortcut field given the Health samples themselves, not their Value: one per line,
    # and grouping by day adds an empty bucket for tomorrow
    parsed = health.parse(
        {
            "date": "2026-10-13",
            "steps": "9,898 count\n0 count",
            "active_kcal": "412.5 Cal\n0 Cal",
            "distance_mi": "5 km",  # the unit Health names wins over the field's
            "flights": [7, 0],  # a Number field given the list
            "weight_lb": "182.4 lb\n183 lb",  # latest first
            "weight_date": "2026-10-13T07:02:11-04:00",
        },
        TZ,
    )
    day = "2026-10-13T04:00:00Z"
    assert parsed.activity == {
        ("steps", "day", day): 9898,
        ("active_kcal", "day", day): 412.5,
        ("distance_m", "day", day): 5000,
        ("flights", "day", day): 7,
    }
    assert parsed.weights == {"2026-10-13T11:02:11Z": pytest.approx(82.736, abs=1e-3)}
    # What arrived is echoed back, so a field pointing at the wrong thing shows
    assert parsed.received["steps"] == "9,898 count\n0 count"
    assert parsed.received["flights"] == "7\n0" and parsed.received["date"] == "2026-10-13"
    # The phone's region decides how numbers are written
    regions = {"9.898": 9898, "9\u202f898": 9898, "1,234.5": 1234.5, "1.234,5": 1234.5}
    regions |= {"412,5 Cal": 412.5, "0.250": 0.25, "12,345": 12345}
    for text, amount in regions.items():
        parsed = health.parse({"date": "2026-10-13", "active_kcal": text}, TZ)
        assert parsed.activity == {("active_kcal", "day", day): amount}, text
    bad = health.parse(
        {"date": "2026-10-13", "steps": "Today, 12:04 AM", "active_kcal": "1 kcal\n2 kJ"}, TZ
    )
    assert bad.activity == {} and bad.problems == [
        "steps: not a number: 'Today, 12:04 AM'",
        "active_kcal: mixed units: kJ, kcal",
    ]


def test_bad_activity_readings_are_rejected_not_fatal():
    parsed = health.parse(
        {
            "steps": [
                {"date": "2026-10-13", "value": 8123},
                {"date": "2026-13-45", "value": 1},
                {"date": "2026-10-14", "value": "lots"},
                {"date": "2026-10-15", "value": 900_000},
            ],
            "distance": [{"date": "2026-10-13", "value": 3}],  # no unit
        },
        TZ,
    )
    assert len(parsed.activity) == 1 and parsed.rejected == 4
    flat = health.parse({"steps": 100, "weight_lb": 180}, TZ)  # no date, no weight_date
    assert flat.rejected == 2 and "steps: missing date" in flat.problems[0]


# --- route ----------------------------------------------------------------------------------


@pytest.fixture
def server(settings, conn):
    return build_server(settings)


@pytest.fixture
def ingest(server):
    with TestClient(server.streamable_http_app()) as client:
        yield client


def test_ingest_saves_activity_and_replaces_a_filling_bucket(ingest, conn):
    token = health.new_token(conn, users.OWNER_ID)
    auth = {"Authorization": f"Bearer {token}"}
    payload = hae(
        ("step_count", "count", [("2026-10-12 00:00:00 -0400", 9000)]),
        ("step_count", "count", [("2026-10-13 00:00:00 -0400", 4000)]),
    )
    r = ingest.post(health.PATH, json=payload, headers=auth)
    assert r.status_code == 200
    assert r.json()["activity"] == {"added": 2, "updated": 0, "unchanged": 0}
    assert r.json()["weights"] == {"added": 0, "updated": 0, "unchanged": 0}
    # Later that day the phone resends both days; today's total has grown
    payload = hae(
        ("step_count", "count", [("2026-10-12 00:00:00 -0400", 9000)]),
        ("step_count", "count", [("2026-10-13 00:00:00 -0400", 7500)]),
    )
    r = ingest.post(health.PATH, json=payload, headers=auth)
    assert r.json()["activity"] == {"added": 0, "updated": 1, "unchanged": 1}
    rows = store.activity_between(conn, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
    assert [(r["start_at"], r["qty"]) for r in rows] == [
        ("2026-10-12T04:00:00Z", 9000),
        ("2026-10-13T04:00:00Z", 7500),
    ]
    # An unchanged resend still counts as the phone's latest send
    conn.execute("UPDATE activity SET updated_at = '2026-01-01 00:00:00'")
    assert store.last_activity_sync(conn) == "2026-01-01T00:00:00Z"
    r = ingest.post(health.PATH, json=payload, headers=auth)
    assert r.json()["activity"] == {"added": 0, "updated": 0, "unchanged": 2}
    assert store.last_activity_sync(conn) > "2026-01-02"
    r = ingest.post(health.PATH, json={"date": "2026-10-13", "flights": 3}, headers=auth)
    assert r.json()["activity"]["added"] == 1
    assert r.json()["received"] == {"date": "2026-10-13", "flights": 3}


# --- tool -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_activity(server, conn):
    store.add_activity(
        conn,
        [
            ("steps", "day", midnight(2), 10000),
            ("active_kcal", "day", midnight(2), 500),
            ("resting_kcal", "day", midnight(2), 1700),
            ("distance_m", "day", midnight(2), 8046.72),
            ("steps", "day", midnight(1), 6000),
            ("steps", "day", midnight(0), 1234),
        ],
    )
    async with Client(server) as client:
        result = await client.call_tool("get_activity", {})
        assert not result.is_error, result.content
        out = result.structured_content
    assert out["start"] == local_day(6).isoformat() and out["end"] == local_day(0).isoformat()
    first, second, today = out["per_day"]
    assert first["date"] == local_day(2).isoformat()
    assert first["steps"] == 10000 and first["burned_kcal"] == 2200
    assert first["distance_mi"] == 5.0 and first["distance_km"] == 8.05
    assert second["active_kcal"] is None and second["burned_kcal"] is None
    assert today["steps"] == 1234
    assert out["average_per_day"]["steps"] == 8000  # today isn't over, so it's left out
    assert out["last_sync"] is not None


@pytest.mark.anyio
async def test_get_activity_with_nothing_sent(server):
    async with Client(server) as client:
        out = (await client.call_tool("get_activity", {"start": "2026-10-01"})).structured_content
    assert out["per_day"] == [] and out["average_per_day"] is None and out["last_sync"] is None


# --- page -----------------------------------------------------------------------------------


def test_empty_activity_page_says_where_it_comes_from(http):
    sign_in(http)
    page = http.get("/app/progress/activity").text
    assert 'href="/app/progress" aria-current=page>' in page
    assert 'href="/app/progress/activity" aria-current=page>Activity<' in page
    assert "No activity yet." in page and "Apple Health" in page


def test_activity_today_is_today_even_before_the_phone_sends_it(http, conn):
    store.add_activity(
        conn,
        [
            ("steps", "day", midnight(1), 9963),
            ("active_kcal", "day", midnight(1), 1078),
            ("resting_kcal", "day", midnight(1), 1793),
        ],
    )
    sign_in(http)
    page = http.get("/app/progress/activity").text.replace("\xa0", " ")
    # Nothing sent for today yet: today at 0, not yesterday's numbers
    assert '<div class="big weight">0</div>' in page and "steps today" in page
    assert "9,963" not in page and "yesterday" not in page.lower()
    assert "0 kcal burned so far" in page
    store.add_activity(conn, [("steps", "day", midnight(0), 12)])
    page = http.get("/app/progress/activity").text.replace("\xa0", " ")
    assert '<div class="big weight">12</div>' in page and "steps today" in page
    assert "Yesterday" in http.get("/app/progress/activity?days=7").text  # still in a range


def test_activity_page_shows_today_chart_average_and_days(http, conn):
    store.add_activity(
        conn,
        [
            ("steps", "day", midnight(3), 12000),
            ("steps", "day", midnight(1), 8000),
            ("active_kcal", "day", midnight(1), 450),
            ("resting_kcal", "day", midnight(1), 1750),
            ("exercise_min", "day", midnight(1), 30),
            ("distance_m", "day", midnight(1), 1609.344 * 3.5),
            ("flights", "day", midnight(1), 1),
            ("steps", "day", midnight(0), 2345),
            ("resting_kcal", "day", midnight(0), 900),
            ("active_kcal", "day", midnight(0), 100),
            ("steps", "day", midnight(20), 5000),  # only in the 30-day view
        ],
    )
    sign_in(http)
    # Today, the default: just today's numbers
    page = http.get("/app/progress/activity").text.replace("\xa0", " ")
    assert 'aria-label="today" aria-current=page>Today<' in page
    assert '<div class="big weight">2,345</div>' in page and "steps today" in page
    assert "900</div>" in page and "kcal resting" in page
    assert "1,000 kcal burned so far" in page and "<b>Apple Health</b> ·" in page
    assert 'class="bar steps"' not in page and "Yesterday" not in page
    # A range: the tiles are daily averages over its full days, then the chart and the days
    page = http.get("/app/progress/activity?days=7").text.replace("\xa0", " ")
    assert '<div class="big weight">10,000</div>' in page and "steps a day over 7 days" in page
    assert "Daily averages over 2 full days with data." in page and "so far" not in page
    assert "450</div>" in page and "1,750</div>" in page  # active and resting, from yesterday
    assert page.count('class="bar steps"') == 3 and '<a href="/app?date=' not in page
    assert "Yesterday" in page and "8,000 steps" in page
    assert "450 kcal active · 2,200 kcal burned · 30 min exercise · 3.5 mi<" in page
    assert "flight" not in page  # kept, but not shown
    # A week's bars show their numbers
    assert 'text-anchor="middle">8,000</text>' in page and ">avg " not in page
    page = http.get("/app/progress/activity?days=30").text.replace("\xa0", " ")
    assert page.count('class="bar steps"') == 4 and "over 3 full days with data." in page
    assert '<div class="big weight">8,333</div>' in page  # the tiles follow the range
    # longer ranges mark the daily average instead
    assert "avg 8,333</text>" in page and 'text-anchor="middle">8,000</text>' not in page
    page = http.get("/app/progress/activity?days=12345").text  # falls back to Today
    assert "aria-current=page>Today<" in page


def test_activity_page_with_nothing_in_range(http, conn):
    store.add_activity(conn, [("steps", "day", midnight(200), 5000)])
    sign_in(http)
    page = http.get("/app/progress/activity?days=7").text
    assert "No activity in this period." in page and 'class="chart"' not in page
    assert "No full days with data yet." in page


# --- export ---------------------------------------------------------------------------------


def test_json_export_includes_activity_in_range(conn):
    store.add_activity(
        conn,
        [
            ("steps", "day", "2026-10-05T04:00:00Z", 9000),
            ("steps", "day", "2026-10-07T04:00:00Z", 1),
        ],
    )
    data = export.to_json(conn, TZ, end=date(2026, 10, 6))
    assert data["activity"] == [
        {"metric": "steps", "period": "day", "start_at": "2026-10-05T00:00:00-04:00", "qty": 9000}
    ]
