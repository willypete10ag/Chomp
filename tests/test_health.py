"""Health data (#94): weigh-ins from the phone's ingest token, and the weight tools."""

from __future__ import annotations

import io
from contextlib import closing

import pytest
from mcp import Client
from starlette.testclient import TestClient

from chomp import cli, db, health, logic, store, users
from chomp.server import build_server

TZ = "America/New_York"
HER_PASSWORD = "her own long password"


def add_her(conn) -> int:
    return users.add_user(conn, "Sam", HER_PASSWORD, "")


def hae(*readings, units="lb", name="weight_&_body_mass", extra=None):
    """A Health Auto Export payload."""
    metrics = [{"name": name, "units": units, "data": [{"date": d, "qty": q} for d, q in readings]}]
    return {"data": {"metrics": metrics + (extra or []), "workouts": []}}


@pytest.fixture
def server(settings, conn):
    return build_server(settings)


@pytest.fixture
def http(server):
    with TestClient(server.streamable_http_app()) as client:
        yield client


@pytest.fixture
def token(conn):
    return health.new_token(conn, users.OWNER_ID)


def post(http, payload, token, **kw):
    return http.post(health.PATH, json=payload, headers={"Authorization": f"Bearer {token}"}, **kw)


# --- logic ----------------------------------------------------------------------------------


def test_to_kg_and_back():
    assert logic.to_kg(182.4, "lb") == pytest.approx(82.7352, abs=1e-4)
    assert logic.to_kg(80, "KG") == 80
    assert logic.weight_out(logic.to_kg(182.4, "lbs")) == {"kg": 82.74, "lb": 182.4}
    for value, unit in ((180, "stone"), (0, "kg"), (-1, "lb"), (2000, "kg"), (float("nan"), "kg")):
        with pytest.raises(ValueError):
            logic.to_kg(value, unit)


def test_parse_time_names_the_field():
    with pytest.raises(ValueError, match="measured_at needs a time"):
        logic.parse_time("2026-10-13", TZ, field="measured_at")
    assert logic.parse_time("2026-10-13T07:00", TZ) == "2026-10-13T11:00:00Z"


# --- payloads -------------------------------------------------------------------------------


def test_parses_health_auto_export():
    steps = {"name": "heart_rate", "units": "count/min", "data": [{"date": "x", "qty": 1}] * 3}
    parsed = health.parse(
        hae(
            ("2026-10-13 07:02:11 -0400", 182.4),
            ("2026-10-14 06:58:00 -0400", 181.9),
            extra=[steps],
        ),
        TZ,
    )
    assert parsed.weights == {
        "2026-10-13T11:02:11Z": pytest.approx(82.736, abs=1e-3),
        "2026-10-14T10:58:00Z": pytest.approx(82.509, abs=1e-3),
    }
    assert parsed.ignored == {"heart_rate": 3} and parsed.rejected == 0


def test_weight_metric_names_match_loosely():
    for name in ("weight_&_body_mass", "weight_body_mass", "Weight & Body Mass", "body_mass"):
        parsed = health.parse(hae(("2026-10-13 07:00:00 -0400", 180), name=name), TZ)
        assert list(parsed.weights) == ["2026-10-13T11:00:00Z"], name
    assert health.parse(hae(("2026-10-13 07:00:00 -0400", 22), name="body_mass_index"), TZ).ignored


def test_parses_simple_shape_with_local_and_offset_times():
    parsed = health.parse(
        {
            "weight": [
                {"date": "2026-10-13T07:00", "value": "80.5", "unit": "kg"},
                {"date": "2026-10-14T07:00:00Z", "value": 80.1, "unit": "kg"},
            ]
        },
        TZ,
    )
    assert parsed.weights == {"2026-10-13T11:00:00Z": 80.5, "2026-10-14T07:00:00Z": 80.1}
    one = health.parse({"weight": {"date": "2026-10-13T07:00", "qty": 180, "units": "lb"}}, TZ)
    assert list(one.weights) == ["2026-10-13T11:00:00Z"]


def test_bad_readings_are_rejected_not_fatal():
    parsed = health.parse(
        {
            "weight": [
                {"date": "2026-10-13T07:00", "value": 180, "unit": "lb"},
                {"date": "2026-10-13", "value": 180, "unit": "lb"},
                {"date": "2026-10-13T08:00", "value": 180},
                {"date": "2026-10-13T09:00", "value": True, "unit": "lb"},
                {"date": "2026-10-13T10:00", "value": 9000, "unit": "lb"},
                "junk",
            ]
        },
        TZ,
    )
    assert len(parsed.weights) == 1
    assert parsed.rejected == 5 and len(parsed.problems) == health.MAX_PROBLEMS
    for bad in ([], "x", {"nothing": 1}):
        with pytest.raises(health.PayloadError):
            health.parse(bad, TZ)


# --- tokens ---------------------------------------------------------------------------------


def test_token_is_stored_hashed_and_replaced(conn):
    first = health.new_token(conn, users.OWNER_ID)
    assert first.startswith(health.TOKEN_PREFIX)
    stored = conn.execute("SELECT token_hash FROM ingest_tokens").fetchall()
    assert len(stored) == 1 and first not in stored[0][0]
    assert health.user_of_token(conn, first) == users.OWNER_ID
    second = health.new_token(conn, users.OWNER_ID)
    assert health.user_of_token(conn, first) is None
    assert health.user_of_token(conn, second) == users.OWNER_ID
    assert health.revoke_token(conn, users.OWNER_ID)
    assert health.user_of_token(conn, second) is None
    assert not health.revoke_token(conn, users.OWNER_ID)


def test_cli_makes_and_revokes_tokens(settings, conn, monkeypatch, capsys):
    monkeypatch.setattr("chomp.config.Settings.from_env", lambda: settings)
    monkeypatch.setattr("sys.stdin", io.StringIO(HER_PASSWORD + "\n"))
    assert cli.main(["user", "add", "Sam"]) == 0
    assert cli.main(["user", "ingest-token", "sam"]) == 0
    token = [line for line in capsys.readouterr().out.splitlines() if line.startswith("chomp_")]
    assert health.user_of_token(conn, token[0]) == 2
    assert cli.main(["user", "ingest-token", "Sam", "--revoke"]) == 0
    assert health.user_of_token(conn, token[0]) is None
    assert cli.main(["user", "ingest-token", "nobody"]) == 1


# --- route ----------------------------------------------------------------------------------


def test_ingest_saves_and_dedupes(http, conn, token):
    payload = hae(("2026-10-13 07:02:11 -0400", 182.4), ("2026-10-14 06:58:00 -0400", 181.9))
    r = post(http, payload, token)
    assert r.status_code == 200
    assert r.json()["weights"] == {"added": 2, "updated": 0, "unchanged": 0}
    r = post(http, payload, token)
    assert r.json()["weights"] == {"added": 0, "updated": 0, "unchanged": 2}
    r = post(http, hae(("2026-10-14 06:58:00 -0400", 181.5)), token)
    assert r.json()["weights"] == {"added": 0, "updated": 1, "unchanged": 0}
    rows = store.weights_between(conn, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
    assert [(r["measured_at"], logic.weight_out(r["kg"])["lb"], r["source"]) for r in rows] == [
        ("2026-10-13T11:02:11Z", 182.4, "health"),
        ("2026-10-14T10:58:00Z", 181.5, "health"),
    ]
    used = conn.execute("SELECT last_used_at FROM ingest_tokens").fetchone()[0]
    assert used is not None


def test_ingest_goes_to_the_tokens_owner(settings, http, conn):
    her = add_her(conn)
    r = post(http, hae(("2026-10-13 07:00:00 -0400", 130)), health.new_token(conn, her))
    assert r.json()["weights"]["added"] == 1
    assert store.latest_weight(conn) is None  # not the owner's
    with closing(db.connect(settings.db_path, her)) as hers:
        assert logic.weight_out(store.latest_weight(hers)["kg"])["lb"] == 130.0


def test_ingest_accepts_x_chomp_token_header(http, token):
    r = http.post(
        health.PATH,
        json={"weight": {"date": "2026-10-13T07:00", "value": 80, "unit": "kg"}},
        headers={"X-Chomp-Token": token},
    )
    assert r.status_code == 200


def test_ingest_refuses_missing_and_wrong_tokens(http, token):
    payload = hae(("2026-10-13 07:00:00 -0400", 180))
    assert http.post(health.PATH, json=payload).status_code == 401
    assert post(http, payload, token + "x").status_code == 401
    assert post(http, payload, "not-a-chomp-token").status_code == 401
    assert http.post(f"{health.PATH}?token={token}", json=payload).status_code == 401
    assert http.get(health.PATH).status_code == 405


def test_ingest_rejects_bad_bodies(http, token, monkeypatch):
    auth = {"Authorization": f"Bearer {token}"}
    r = http.post(health.PATH, content=b"{not json", headers=auth)
    assert r.status_code == 400 and r.json() == {"error": "body is not JSON"}
    r = http.post(health.PATH, json={"sleep": 3}, headers=auth)
    assert r.status_code == 400 and "no readings" in r.json()["error"]
    monkeypatch.setattr(health, "MAX_BODY", 100)
    big = {"weight": [{"date": "2026-10-13T07:00", "value": 80, "unit": "kg"}] * 20}
    assert http.post(health.PATH, json=big, headers=auth).status_code == 413


# --- tools ----------------------------------------------------------------------------------


async def call(client, tool, **args):
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    return result.structured_content


@pytest.mark.anyio
async def test_log_get_and_delete_weight(server, conn):
    store.add_weight(conn, "2026-10-12T11:00:00Z", logic.to_kg(183, "lb"), "health")
    async with Client(server) as client:
        out = await call(
            client, "log_weight", weight=182.4, unit="lb", measured_at="2026-10-13T07:00"
        )
        assert out["weight"]["lb"] == 182.4 and out["weight"]["source"] == "chat"
        assert out["weight"]["measured_at"] == "2026-10-13T07:00:00-04:00"
        assert out["previous"]["lb"] == 183.0 and out["change"]["lb"] == -0.6
        hist = await call(client, "get_weight", start="2026-10-01", end="2026-10-31")
        assert [d["lb"] for d in hist["per_day"]] == [183.0, 182.4]
        assert hist["change"]["lb"] == -0.6 and hist["average"]["lb"] == 182.7
        assert hist["latest"]["lb"] == 182.4
        empty = await call(client, "get_weight", start="2026-09-01", end="2026-09-30")
        assert empty["readings"] == [] and empty["change"] is None
        assert empty["latest"]["lb"] == 182.4
        gone = await call(client, "delete_weight", weight_id=out["weight"]["weight_id"])
        assert gone["lb"] == 182.4
        result = await client.call_tool("delete_weight", {"weight_id": 999})
        assert result.is_error and "no weigh-in" in result.content[0].text
        result = await client.call_tool(
            "log_weight", {"weight": 180, "unit": "lb", "measured_at": "2026-10-13"}
        )
        assert result.is_error and "measured_at needs a time" in result.content[0].text


def test_users_cannot_delete_each_others_weights(settings, conn):
    her = add_her(conn)
    with closing(db.connect(settings.db_path, her)) as hers:
        row = store.add_weight(hers, "2026-10-13T11:00:00Z", 60, "app")
    with pytest.raises(store.NotFound):
        store.delete_weight(conn, row["id"])


def test_deleted_weigh_in_stays_deleted_when_the_phone_resends(http, conn, token):
    payload = hae(("2026-10-13 07:00:00 -0400", 250))  # someone else on the scale
    post(http, payload, token)
    row = store.latest_weight(conn)
    store.delete_weight(conn, row["id"])
    r = post(http, payload, token)
    assert r.json()["weights"] == {"added": 0, "updated": 0, "unchanged": 1}
    assert store.latest_weight(conn) is None
    with pytest.raises(store.NotFound):
        store.delete_weight(conn, row["id"])
    # Logged on purpose (chat or the app), the same time comes back
    store.add_weight(conn, "2026-10-13T11:00:00Z", 110, "chat")
    assert store.latest_weight(conn)["kg"] == 110
