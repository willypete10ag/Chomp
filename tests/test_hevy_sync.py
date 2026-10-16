"""Hevy API sync (#113): the API's workout objects, the client's paging and errors, the sync
(first full pull adopting CSV imports, then events: edits and deletes), Sync now and the
command."""

from __future__ import annotations

import dataclasses
from contextlib import closing
from datetime import UTC, datetime

import httpx
import pytest
import test_web
from conftest import FIXTURES
from test_web import ORIGIN, client_for, sign_in
from test_web_edits import follow

from chomp import cli, db, hevy, hevy_sync, store
from chomp.sources import SourceError
from chomp.sources.hevy import HevyClient, HevyKeyError

TZ = "America/New_York"
CSV = (FIXTURES / "hevy" / "workouts.csv").read_bytes()
NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
public_settings = test_web.public_settings


def api_workout(wid="w-push", start="2026-10-12T22:30:27Z", bench=(185, 6), **extra):
    """Push Day from the CSV fixture as Hevy's API sends it, with seconds on the times."""
    return {
        "id": wid,
        "title": "Push Day",
        "description": "Felt strong",
        "start_time": start,
        "end_time": "2026-10-12T23:42:10+00:00",
        "updated_at": "2026-10-12T23:43:00Z",
        "exercises": [
            {
                "index": 1,
                "title": "Triceps Pushdown",
                "notes": "Rope",
                "superset_id": 0,
                "sets": [
                    {"index": 1, "type": "dropset", "weight_kg": 18.14, "reps": 10},
                    {"index": 0, "type": "normal", "weight_kg": 27.22, "reps": 12},
                ],
            },
            {
                "index": 0,
                "title": "Bench Press (Barbell)",
                "notes": None,
                "supersets_id": None,
                "sets": [
                    {"index": 0, "type": "warmup", "weight_kg": 43.09, "reps": 10, "rpe": None},
                    {
                        "index": 1,
                        "type": "normal",
                        "weight_kg": bench[0] * 0.45359237,
                        "reps": bench[1],
                        "rpe": 8.5,
                    },
                ],
            },
        ],
        **extra,
    }


class FakeHevy:
    """Hevy's API over httpx.MockTransport: a list of workouts and a list of events, served
    10 to a page; records each request's path and params."""

    def __init__(self) -> None:
        self.workouts: list[dict] = []
        self.events: list[dict] = []
        self.status = 200
        self.requests: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append((request.url.path, params))
        assert request.headers["api-key"] == "test-key"
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        items, key = (
            (self.workouts, "workouts")
            if request.url.path == "/v1/workouts"
            else (self.events, "events")
        )
        size = int(params.get("pageSize", 5))
        page = int(params.get("page", 1))
        count = -(-len(items) // size)
        if page > count:
            return httpx.Response(404, json={"error": "Page not found"})
        chunk = items[(page - 1) * size : page * size]
        return httpx.Response(200, json={"page": page, "page_count": count, key: chunk})

    def client(self) -> HevyClient:
        transport = httpx.MockTransport(self.handler)
        return HevyClient("test-key", httpx.Client(transport=transport), backoff_s=0)


@pytest.fixture
def fake():
    return FakeHevy()


def rows(conn):
    return conn.execute(
        "SELECT id, started_at, title, hevy_id FROM workouts ORDER BY started_at"
    ).fetchall()


# --- Parsing ---------------------------------------------------------------------------------


def test_reads_an_api_workout():
    w = hevy.from_api(api_workout())
    assert (w.hevy_id, w.title, w.started_at, w.ended_at) == (
        "w-push",
        "Push Day",
        "2026-10-12T22:30:27Z",
        "2026-10-12T23:42:10Z",
    )
    # Exercises and sets in Hevy's order, whatever order the JSON lists them in
    assert [(s.exercise, s.set_index, s.set_type) for s in w.sets] == [
        ("Bench Press (Barbell)", 0, "warmup"),
        ("Bench Press (Barbell)", 1, "normal"),
        ("Triceps Pushdown", 0, "normal"),
        ("Triceps Pushdown", 1, "dropset"),
    ]
    assert w.sets[1].rpe == 8.5 and w.sets[0].rpe is None and w.sets[0].superset_id is None
    assert w.sets[2].superset_id == 0 and w.sets[2].exercise_notes == "Rope"
    assert w.sets[1].weight_kg == pytest.approx(83.91, abs=0.01)


def test_api_values_that_cant_be_used():
    data = api_workout()
    data["exercises"][0]["sets"][0].update(weight_kg=-5, reps="x", type="mystery")
    data["exercises"][0]["sets"][1].update(distance_meters=400, duration_seconds=95.5)
    w = hevy.from_api(data)
    pushdown = w.sets[2:]
    assert pushdown[1].weight_kg is None and pushdown[1].reps is None
    assert pushdown[1].set_type == "normal"
    assert (pushdown[0].distance_m, pushdown[0].duration_s) == (400, 95.5)
    for bad in ({"id": ""}, {"id": "x"}, {"id": "x", "start_time": "soon"}):
        with pytest.raises(hevy.HevyError):
            hevy.from_api(bad)


# --- Client ----------------------------------------------------------------------------------


def test_client_pages_through_and_treats_404_as_empty(fake):
    fake.workouts = [api_workout(f"w{i}") for i in range(23)]
    with fake.client() as client:
        assert [w["id"] for w in client.workouts()] == [f"w{i}" for i in range(23)]
        assert list(client.events("2026-10-01T00:00:00Z")) == []
    assert [p["page"] for path, p in fake.requests if path == "/v1/workouts"] == ["1", "2", "3"]
    assert fake.requests[0][1]["pageSize"] == "10"
    assert fake.requests[-1] == (
        "/v1/workouts/events",
        {"since": "2026-10-01T00:00:00Z", "page": "1", "pageSize": "10"},
    )


def test_client_reads_replies_that_differ_from_the_spec():
    replies = iter(
        [
            {"page": 1, "page_count": 0},  # an empty events feed, as the live server sends it
            {"page": 1, "page_count": 1, "events": None},
            {"page": 1, "page_count": 1, "workout_events": [{"type": "deleted", "id": "w1"}]},
            {"page": 1, "page_count": 1, "a": [], "b": []},
        ]
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=next(replies)))
    with HevyClient("test-key", httpx.Client(transport=transport), backoff_s=0) as client:
        assert list(client.events("2026-10-16T00:00:00Z")) == []
        assert list(client.events("2026-10-16T00:00:00Z")) == []
        assert list(client.events("2026-10-16T00:00:00Z")) == [{"type": "deleted", "id": "w1"}]
        with pytest.raises(SourceError, match="keys: a, b, page, page_count"):
            list(client.events("2026-10-16T00:00:00Z"))


def test_client_errors(fake):
    fake.status = 401
    with fake.client() as client, pytest.raises(HevyKeyError, match="HEVY_API_KEY"):
        list(client.workouts())
    fake.status = 503
    with fake.client() as client, pytest.raises(SourceError, match="unavailable"):
        list(client.workouts())
    assert len(fake.requests) == 1 + 3  # 5xx is retried


# --- Sync ------------------------------------------------------------------------------------


def test_first_sync_adopts_csv_imports_and_adds_the_rest(conn, fake):
    store.import_workouts(conn, hevy.parse(CSV, TZ).workouts, "hevy")
    fake.workouts = [
        api_workout(),
        api_workout("w-new", start="2026-10-15T22:00:00Z"),
    ]
    with fake.client() as client:
        result = hevy_sync.sync(conn, client, now=NOW)
    assert (result.full, result.added, result.updated, result.error) == (True, 1, 1, None)
    got = rows(conn)
    assert [(r["title"], r["hevy_id"]) for r in got] == [
        ("Push Day", "w-push"),
        ("Legs", None),  # not in Hevy any more, so left as it was
        ("Push Day", "w-new"),
    ]
    assert got[0]["started_at"] == "2026-10-12T22:30:27Z"  # the API's time, with seconds
    sets = store.workout_sets(conn, [got[0]["id"]])[got[0]["id"]]
    assert [s["exercise"] for s in sets] == ["Bench Press (Barbell)"] * 2 + ["Triceps Pushdown"] * 2
    state = store.hevy_sync_state(conn)
    assert state["since"] == "2026-10-16T13:50:00Z" and state["last_error"] is None
    assert (state["added"], state["updated"], state["deleted"]) == (1, 1, 0)

    # Running the full pull again changes nothing
    with fake.client() as client:
        again = hevy_sync.sync(conn, client, full=True, now=NOW)
    assert (again.added, again.updated, again.unchanged) == (0, 0, 2)

    # A later CSV import leaves synced workouts alone and doesn't double them
    counts = store.import_workouts(conn, hevy.parse(CSV, TZ).workouts, "hevy")
    assert (counts.added, counts.updated, counts.unchanged) == (0, 0, 2)
    assert len(rows(conn)) == 3


def test_events_bring_edits_and_deletes(conn, fake):
    fake.workouts = [api_workout(), api_workout("w-gone", start="2026-10-14T11:05:00Z")]
    with fake.client() as client:
        hevy_sync.sync(conn, client, now=NOW)
    edited = api_workout(bench=(190, 5), updated_at="2026-10-16T15:00:00Z", title="Push A")
    fake.events = [
        {"type": "updated", "workout": api_workout(updated_at="2026-10-16T14:30:00Z")},
        {"type": "updated", "workout": edited},
        {"type": "deleted", "id": "w-gone", "deleted_at": "2026-10-16T14:10:00Z"},
        {"type": "deleted", "id": "never-synced", "deleted_at": "2026-10-16T14:10:00Z"},
        {"type": "updated", "workout": api_workout("w-late", start="2026-10-16T13:00:00Z")},
    ]
    later = datetime(2026, 10, 16, 16, 0, tzinfo=UTC)
    with fake.client() as client:
        result = hevy_sync.sync(conn, client, now=later)
    assert fake.requests[-1][1]["since"] == "2026-10-16T13:50:00Z"
    assert (result.full, result.added, result.updated, result.deleted, result.error) == (
        False,
        1,
        1,
        1,
        None,
    )
    got = rows(conn)
    assert [(r["title"], r["hevy_id"]) for r in got] == [
        ("Push A", "w-push"),
        ("Push Day", "w-late"),
    ]
    sets = store.workout_sets(conn, [got[0]["id"]])[got[0]["id"]]
    assert (round(sets[1]["weight_kg"] / 0.45359237), sets[1]["reps"]) == (190, 5)
    assert store.hevy_sync_state(conn)["since"] == "2026-10-16T15:50:00Z"

    # Deleted after an edit, in the same run: gone
    fake.events = [
        {"type": "updated", "workout": api_workout("w-late", updated_at="2026-10-16T16:30:00Z")},
        {"type": "deleted", "id": "w-late", "deleted_at": "2026-10-16T16:40:00Z"},
    ]
    with fake.client() as client:
        result = hevy_sync.sync(conn, client, now=later)
    assert result.deleted == 1 and [r["hevy_id"] for r in rows(conn)] == ["w-push"]


def test_a_workout_that_cant_be_saved_keeps_the_cursor(conn, fake):
    fake.workouts = [api_workout()]
    with fake.client() as client:
        hevy_sync.sync(conn, client, now=NOW)
    broken = {"id": "w-broken", "updated_at": "2026-10-16T14:00:00Z"}  # no start time
    fake.events = [
        {"type": "updated", "workout": api_workout("w-new", start="2026-10-16T12:00:00Z")},
        {"type": "updated", "workout": broken},
    ]
    later = datetime(2026, 10, 16, 16, 0, tzinfo=UTC)
    with fake.client() as client:
        result = hevy_sync.sync(conn, client, now=later)
    assert (result.added, result.skipped) == (1, 1)
    assert (
        result.error
        == "1 workout from Hevy couldn't be saved (see the server log); the next sync tries again"
    )
    state = store.hevy_sync_state(conn)
    assert state["since"] == "2026-10-16T13:50:00Z" and state["last_error"] == result.error
    # Once Hevy sends it whole, the cursor moves on
    broken.update(api_workout("w-broken", start="2026-10-16T13:00:00Z"))
    with fake.client() as client:
        result = hevy_sync.sync(conn, client, now=later)
    assert fake.requests[-1][1]["since"] == "2026-10-16T13:50:00Z"
    assert (result.added, result.unchanged, result.error) == (1, 1, None)
    assert store.hevy_sync_state(conn)["since"] == "2026-10-16T15:50:00Z"


def test_a_failed_run_keeps_the_cursor(conn, fake):
    fake.workouts = [api_workout()]
    with fake.client() as client:
        hevy_sync.sync(conn, client, now=NOW)
    fake.status = 401
    with fake.client() as client:
        result = hevy_sync.sync(conn, client, now=datetime(2026, 10, 17, tzinfo=UTC))
    assert result.error and "HEVY_API_KEY" in result.error
    state = store.hevy_sync_state(conn)
    assert state["since"] == "2026-10-16T13:50:00Z" and "HEVY_API_KEY" in state["last_error"]
    fake.status = 200
    with fake.client() as client:
        hevy_sync.sync(conn, client, now=datetime(2026, 10, 17, tzinfo=UTC))
    assert fake.requests[-1][1]["since"] == "2026-10-16T13:50:00Z"
    assert store.hevy_sync_state(conn)["last_error"] is None


def test_run_opens_the_hevy_users_log(settings, conn, fake):
    fake.workouts = [api_workout()]
    with_key = dataclasses.replace(settings, hevy_api_key="test-key")
    assert hevy_sync.run(with_key, client=fake.client()).added == 1
    assert len(rows(conn)) == 1
    nobody = dataclasses.replace(with_key, hevy_user="nobody")
    with pytest.raises(Exception, match="no user named"):
        hevy_sync.run(nobody, client=fake.client())
    assert hevy_sync.keyed_users(conn, nobody) == []  # an unknown HEVY_USER syncs no one
    assert hevy_sync.run(settings, 1).error == "no Hevy API key for this person"


def test_cli_sync_needs_a_key(settings, conn, monkeypatch, capsys):
    monkeypatch.setenv("DB_PATH", settings.db_path)
    monkeypatch.delenv("HEVY_API_KEY", raising=False)
    assert cli.main(["sync-hevy"]) == 1
    monkeypatch.setenv("HEVY_API_KEY", "test-key")
    calls = []

    def fake_run(s, user_id, full=False):
        calls.append((s.hevy_api_key, user_id, full))
        return hevy_sync.SyncResult(full=full, added=2)

    monkeypatch.setattr(hevy_sync, "run", fake_run)
    assert cli.main(["sync-hevy", "--full"]) == 0
    assert calls == [("test-key", 1, True)]
    assert "owner: 2 workouts added" in capsys.readouterr().out
    assert cli.main(["sync-hevy", "--user", "owner"]) == 0
    assert calls[-1] == ("test-key", 1, False)


# --- Dashboard -------------------------------------------------------------------------------


def test_integrations_sync_now(public_settings, conn, monkeypatch):
    with client_for(public_settings) as http:  # no key: only the CSV import
        sign_in(http)
        page = http.get("/app/you/integrations").text
        assert "Sync now" not in page and 'action="/app/workouts/import"' in page
    on = dataclasses.replace(public_settings, hevy_api_key="test-key", hevy_sync_minutes=15)
    results = [
        hevy_sync.SyncResult(added=2, updated=1),
        hevy_sync.SyncResult(error="Hevy refused the API key (HTTP 401)."),
    ]

    def fake_run(s, user_id):
        assert user_id == 1
        result = results.pop(0)
        with closing(db.connect(s.db_path, 1)) as mine:  # runs in a worker thread
            store.record_hevy_sync(
                mine, "2026-10-16T14:00:00Z", since="2026-10-16T13:50:00Z", error=result.error
            )
        return result

    monkeypatch.setattr(hevy_sync, "run", fake_run)
    with client_for(on) as http:
        sign_in(http)
        page = http.get("/app/you/integrations").text
        assert "The first sync runs shortly." in page
        assert 'action="/app/workouts/sync"' in page and "Import a CSV export instead" in page
        r = http.post("/app/workouts/sync", headers=ORIGIN)
        assert r.headers["location"] == "/app/you/integrations?hevysync=2-1-0#hevy"
        page = follow(http, r)
        assert "Synced with Hevy: 2 new workouts, 1 updated, 0 deleted." in page
        assert "every 15 min; last checked" in page
        page = follow(http, http.post("/app/workouts/sync", headers=ORIGIN))
        assert "Syncing with Hevy didn&#x27;t work" in page or "Syncing with Hevy didn" in page
        assert "That sync failed: Hevy refused the API key (HTTP 401)." in page
        assert http.post("/app/workouts/sync").status_code == 403  # not from the dashboard
