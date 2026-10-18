"""The weekly gym-day goal: the week's progress, the streak, setting it on You, Training's
card, Today's tile and the connector."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
import test_web
from mcp import Client
from test_hevy import call
from test_web import ORIGIN, sign_in

from chomp import db, logic, store, users
from chomp.server import build_server

public_settings = test_web.public_settings
http = test_web.http

MON = date(2026, 10, 12)


def week(monday: date, *weekdays: int) -> list[date]:
    return [monday + timedelta(days=i) for i in weekdays]


def test_this_week_left_and_reachable():
    # Mon, Tue, Wed trained; on Thursday 3 more are needed with Thu-Sun (4 days) left
    g = logic.gym_goal(week(MON, 0, 1, 2), 6, MON + timedelta(days=3))
    assert (g.this_week, g.left, g.days_left, g.reachable) == (3, 3, 4, True)
    # Trained Thursday too: today is counted, Fri-Sun are left
    g = logic.gym_goal(week(MON, 0, 1, 2, 3), 6, MON + timedelta(days=3))
    assert (g.this_week, g.left, g.days_left, g.reachable) == (4, 2, 3, True)
    # Only Monday by Friday: 5 more in 3 days can't happen
    g = logic.gym_goal(week(MON, 0), 6, MON + timedelta(days=4))
    assert (g.left, g.days_left, g.reachable) == (5, 3, False)
    # Two workouts on one day count once; days after `day` don't count
    g = logic.gym_goal([MON, MON, MON + timedelta(days=5)], 2, MON + timedelta(days=1))
    assert (g.this_week, g.left) == (1, 1)


def test_streak_and_history():
    prev = MON - timedelta(days=7)
    older = MON - timedelta(days=14)
    oldest = MON - timedelta(days=21)
    trained = week(oldest, 0) + week(older, 0, 1, 2) + week(prev, 0, 2, 4, 6) + week(MON, 0)
    g = logic.gym_goal(trained, 3, MON + timedelta(days=2))
    # This week isn't over: it neither breaks nor adds to the streak of the two before
    assert g.streak == 2
    assert [(w.monday, w.days, w.met) for w in g.weeks] == [
        (MON, 1, False),
        (prev, 4, True),
        (older, 3, True),
        (oldest, 1, False),
    ]
    assert (g.met_weeks, g.counted_weeks) == (2, 3)
    # Met this week: it joins the streak and the count
    g = logic.gym_goal(trained + week(MON, 1, 2), 3, MON + timedelta(days=2))
    assert g.streak == 3 and (g.met_weeks, g.counted_weeks) == (3, 4)
    # No workouts at all: just this week, nothing done
    g = logic.gym_goal([], 6, MON)
    assert len(g.weeks) == 1 and g.streak == 0 and g.left == 6 and g.counted_weeks == 0
    with pytest.raises(ValueError):
        logic.gym_goal([], 8, MON)


def test_goal_is_per_person(public_settings, conn):
    sam = users.add_user(conn, "Sam", "sam's own password", "pw")
    store.set_gym_goal(conn, 6)
    with closing(db.connect(public_settings.db_path, sam)) as hers:
        assert store.get_gym_goal(hers) is None
        store.set_gym_goal(hers, 3)
    assert store.get_gym_goal(conn) == 6
    store.set_targets(conn, {"kcal": 2200})
    assert store.get_gym_goal(conn) == 6  # the other goals don't touch it
    with pytest.raises(ValueError):
        store.set_gym_goal(conn, 0)


def add_workouts(conn, days_ago: list[int]) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    for n in days_ago:
        started = (now - timedelta(days=n)).isoformat().replace("+00:00", "Z")
        conn.execute(
            "INSERT INTO workouts (user_id, started_at, title, source) VALUES (1, ?, ?, 'hevy')",
            (started, f"Workout {n}"),
        )
    conn.commit()


def test_set_on_you_and_tracked_on_training_and_today(http, conn):
    sign_in(http)
    page = http.get("/app/you").text
    assert 'href="/app/you/gym-goal"' in page and "Optional: how many days a week" in page
    assert 'name="days"' in http.get("/app/you/gym-goal").text
    r = http.post("/app/you/gym-goal", data={"days": "6"}, headers=ORIGIN)
    assert r.headers["location"] == "/app/you?saved=gym"
    assert store.get_gym_goal(conn) == 6
    page = http.get("/app/you?saved=gym").text
    assert "Gym-day goal saved." in page and "6 days a week" in page
    r = http.post("/app/you/gym-goal", data={"days": "8"}, headers=ORIGIN)
    assert r.status_code == 400 and "1 to 7" in r.text
    r = http.post("/app/you/gym-goal", data={"days": "two"}, headers=ORIGIN)
    assert r.status_code == 400
    assert http.post("/app/you/gym-goal", data={"days": "5"}).status_code == 403

    add_workouts(conn, [0])
    page = http.get("/app/progress/training").text
    assert "Gym days" in page and "<b>1</b> of 6 this week" in page and "This week" in page
    assert "1 of 6 days this week" in http.get("/app").text

    http.post("/app/you/gym-goal", data={"days": ""}, headers=ORIGIN)
    assert store.get_gym_goal(conn) is None
    page = http.get("/app/progress/training").text
    assert 'href="/app/you/gym-goal">Set a weekly gym-day goal' in page
    assert "days this week" not in http.get("/app").text


@pytest.mark.anyio
async def test_connector_sets_and_reports_it(settings, conn):
    add_workouts(conn, [0])
    async with Client(build_server(settings)) as client:
        out = await call(client, "get_workouts")
        assert out["gym_goal"] is None
        out = (await call(client, "set_gym_goal", days_per_week=6))["gym_goal"]
        assert out["goal_days_per_week"] == 6 and out["this_week"] == 1
        assert out["days_needed"] == 5 and out["weeks"][0]["gym_days"] == 1
        out = await call(client, "get_workouts")
        assert out["gym_goal"]["goal_days_per_week"] == 6
        result = await client.call_tool("set_gym_goal", {"days_per_week": 9})
        assert result.is_error
        assert await call(client, "set_gym_goal", days_per_week=None) == {"gym_goal": None}
    assert store.get_gym_goal(conn) is None
