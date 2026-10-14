"""Hevy workouts (#94): the CSV export parser, importing (repeats skipped), the
get_workouts tool, the command and the export."""

from __future__ import annotations

import io
import json
from contextlib import closing

import pytest
from conftest import FIXTURES
from mcp import Client

from chomp import cli, db, export, hevy, logic, store, users
from chomp.server import build_server

TZ = "America/New_York"
CSV = (FIXTURES / "hevy" / "workouts.csv").read_bytes()
HEADER = (
    "title,start_time,end_time,description,exercise_title,superset_id,exercise_notes,"
    "set_index,set_type,weight_kg,reps,distance_km,duration_seconds,rpe\n"
)


def test_parses_the_export():
    parsed = hevy.parse(CSV, TZ)
    assert parsed.rejected == 0
    push, legs = parsed.workouts
    assert (push.title, push.started_at, push.ended_at) == (
        "Push Day",
        "2026-10-12T22:30:00Z",
        "2026-10-12T23:42:00Z",
    )
    assert push.description == "Felt strong" and len(push.sets) == 6
    warmup, top = push.sets[0], push.sets[1]
    assert warmup.set_type == "warmup" and warmup.weight_kg == pytest.approx(43.09, abs=0.01)
    assert (top.reps, top.rpe, top.set_index) == (6, 8.5, 1)
    assert push.sets[3].superset_id == 0 and push.sets[3].exercise_notes == "Rope"
    pushup = push.sets[5]
    assert pushup.weight_kg is None and pushup.reps == 20
    walk = legs.sets[1]
    assert walk.distance_m == pytest.approx(2011.68) and walk.duration_s == 1215
    assert walk.reps is None


def test_metric_exports_and_bad_rows():
    text = (
        HEADER
        + '"Pull","1 Oct 2026, 18:00","1 Oct 2026, 19:00","","Row",,,0,"normal",80,8,,,\n'
        + '"Pull","1 Oct 2026, 18:00","1 Oct 2026, 19:00","","Run",,,0,"normal",,,5,1500,\n'
        + '"Pull","yesterday","","","Row",,,1,"normal",80,8,,,\n'
        + '"Pull","1 Oct 2026, 18:00","","","Row",,,2,"normal",-5,8,,,\n'
        + '"Pull","1 Oct 2026, 18:00","","","",,,3,"normal",80,8,,,\n'
    )
    parsed = hevy.parse(text, TZ)
    (pull,) = parsed.workouts
    assert pull.sets[0].weight_kg == 80 and pull.sets[1].distance_m == 5000
    assert parsed.rejected == 3 and parsed.problems[0].startswith("row 4:")


def test_rejects_files_that_are_not_hevy_exports():
    for bad in (b"", b"date,weight\n2026-10-01,180\n", HEADER.encode()):
        with pytest.raises(hevy.HevyError):
            hevy.parse(bad, TZ)


def test_import_skips_repeats_and_updates_changes(settings, conn):
    parsed = hevy.parse(CSV, TZ)
    counts = store.import_workouts(conn, parsed.workouts, "hevy")
    assert (counts.added, counts.updated, counts.unchanged) == (2, 0, 0)
    counts = store.import_workouts(conn, hevy.parse(CSV, TZ).workouts, "hevy")
    assert (counts.added, counts.updated, counts.unchanged) == (0, 0, 2)
    changed = hevy.parse(CSV.replace(b'"normal",225,5', b'"normal",230,5'), TZ)
    counts = store.import_workouts(conn, changed.workouts, "hevy")
    assert (counts.added, counts.updated, counts.unchanged) == (0, 1, 1)
    rows = store.workouts_between(conn, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
    assert [r["title"] for r in rows] == ["Legs", "Push Day"]  # newest first
    sets = store.workout_sets(conn, [rows[0]["id"]])[rows[0]["id"]]
    assert logic.weight_out(sets[0]["weight_kg"])["lb"] == 230.0
    assert conn.execute("SELECT count(*) FROM workout_sets").fetchone()[0] == 8
    # Another person's log doesn't see them
    her = users.add_user(conn, "Sam", "her own long password", "")
    with closing(db.connect(settings.db_path, her)) as hers:
        assert store.workouts_between(hers, "2026", "2027") == []
        with pytest.raises(store.NotFound):
            store.delete_workout(hers, rows[0]["id"])
    store.delete_workout(conn, rows[0]["id"])
    assert conn.execute("SELECT count(*) FROM workout_sets").fetchone()[0] == 6


def test_volume_and_best_set():
    sets = [
        {"set_type": "warmup", "weight_kg": 40, "reps": 10},
        {"set_type": "normal", "weight_kg": 80, "reps": 6},
        {"set_type": "failure", "weight_kg": 80, "reps": 8},
        {"set_type": "normal", "weight_kg": None, "reps": 20},
    ]
    assert logic.workout_volume_kg(sets) == 80 * 14
    assert logic.best_set(sets)["reps"] == 8
    assert logic.best_set(sets[3:]) is None


async def call(client, tool, **args):
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    return result.structured_content


@pytest.mark.anyio
async def test_get_workouts_tool(settings, conn):
    store.import_workouts(conn, hevy.parse(CSV, TZ).workouts, "hevy")
    async with Client(build_server(settings)) as client:
        out = await call(client, "get_workouts", start="2026-10-01", end="2026-10-31")
        assert out["total_workouts"] == 2
        _legs, push = out["workouts"]
        assert push["title"] == "Push Day" and push["duration_min"] == 72
        assert push["started_at"] == "2026-10-12T18:30:00-04:00"
        bench = push["exercises"][0]
        assert bench["name"] == "Bench Press (Barbell)" and len(bench["sets"]) == 3
        assert bench["sets"][1] == {
            "set_type": "normal",
            "weight_lb": 185.0,
            "weight_kg": 83.91,
            "reps": 6,
            "distance_m": None,
            "duration_s": None,
            "rpe": 8.5,
        }
        # 185x6 + 185x5 + 60x12 + 40x10, warm-up left out
        assert push["volume_lb"] == pytest.approx(185 * 11 + 60 * 12 + 40 * 10, abs=0.2)
        bench_only = await call(
            client, "get_workouts", start="2026-10-01", end="2026-10-31", exercise="bench"
        )
        assert [w["title"] for w in bench_only["workouts"]] == ["Push Day"]
        assert [x["name"] for x in bench_only["workouts"][0]["exercises"]] == [
            "Bench Press (Barbell)"
        ]


def test_cli_import(settings, conn, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("chomp.config.Settings.from_env", lambda: settings)
    path = tmp_path / "workout_data.csv"
    path.write_bytes(CSV)
    assert cli.main(["import-hevy", str(path)]) == 0
    assert "2 workouts added, 0 updated, 0 already there; 0 rows skipped" in capsys.readouterr().out
    assert cli.main(["import-hevy", str(path)]) == 0
    assert "0 workouts added, 0 updated, 2 already there" in capsys.readouterr().out
    stdin = io.TextIOWrapper(io.BytesIO(CSV))
    monkeypatch.setattr("sys.stdin", stdin)
    assert cli.main(["import-hevy", "-"]) == 0
    assert "2 already there" in capsys.readouterr().out
    assert cli.main(["import-hevy", str(tmp_path / "missing.csv")]) == 1
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n")
    assert cli.main(["import-hevy", str(bad)]) == 1


def test_export_holds_workouts(conn):
    store.import_workouts(conn, hevy.parse(CSV, TZ).workouts, "hevy")
    out = io.StringIO()
    export.write_json(conn, out, TZ)
    workouts = json.loads(out.getvalue())["workouts"]
    assert [w["title"] for w in workouts] == ["Push Day", "Legs"]
    assert workouts[0]["started_at"] == "2026-10-12T18:30:00-04:00"
    assert "id" not in workouts[0] and len(workouts[0]["sets"]) == 6
