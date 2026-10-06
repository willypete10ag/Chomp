"""#33 nightly backup + restore, #34 CSV/JSON export, and the `chomp` command for both."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, time

import pytest
from test_tools import EGGS, WHITES, add_food

from chomp import backup, cli, db, export, store


@pytest.fixture
def logged(conn):
    eggs = add_food(conn, "Egg, whole", *EGGS)
    whites = add_food(conn, "Egg white", *WHITES, brand="Kirkland")
    # 2026-10-05 08:00 and 2026-10-06 00:30 New York time (EDT, UTC-4)
    store.log_meal(conn, [(eggs, 200), (whites, 300)], "2026-10-05T12:00:00Z", "breakfast")
    store.log_meal(conn, [(whites, 150)], "2026-10-06T04:30:00Z", None, note="late snack")
    store.save_alias(conn, "my whites", whites, 300)
    store.save_template(conn, "usual breakfast", [(eggs, 200), (whites, 300)])
    store.set_targets(conn, {"protein_g": 180})
    return eggs, whites


# --- backup ---------------------------------------------------------------------------------


def test_backup_restore_round_trip(settings, conn, logged, tmp_path):
    dest = tmp_path / "backups"
    made = backup.backup(settings.db_path, dest, keep=3)
    assert backup.NAME_RE.match(made.name)
    # Something goes wrong after the backup...
    conn.execute("DELETE FROM entries")
    conn.close()
    saved = backup.restore(made, settings.db_path, settings.migrations_dir)
    with closing(db.connect(settings.db_path)) as fresh:
        assert fresh.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3
        assert db.schema_version(fresh) == db.latest_version(settings.migrations_dir)
    with closing(sqlite3.connect(saved)) as before:
        assert before.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0


def test_backup_keeps_only_the_newest(settings, conn, tmp_path):
    dest = tmp_path / "backups"
    (dest).mkdir()
    (dest / "notes.txt").write_text("not a backup")
    for day in range(1, 6):
        backup.backup(settings.db_path, dest, keep=3, now=datetime(2026, 10, day, 3, 30))
    names = [p.name for p in backup.backups(dest)]
    assert names == [f"chomp-2026100{d}-033000.db" for d in (3, 4, 5)]
    assert (dest / "notes.txt").exists()
    assert not list(dest.glob(".*partial"))


def test_backup_of_a_missing_database_fails(tmp_path):
    with pytest.raises(backup.BackupError, match="does not exist"):
        backup.backup(tmp_path / "nope.db", tmp_path / "b", keep=3)


def test_restore_refuses_corrupt_or_newer_files(settings, conn, tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        backup.restore(junk, settings.db_path, settings.migrations_dir)
    newer = tmp_path / "newer.db"
    with closing(sqlite3.connect(newer)) as c:
        c.execute("PRAGMA user_version = 99")
    with pytest.raises(backup.BackupError, match="schema version 99"):
        backup.restore(newer, settings.db_path, settings.migrations_dir)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 02:00 EDT -> 03:30 the same day
        (datetime(2026, 10, 6, 6, 0, tzinfo=UTC), datetime(2026, 10, 6, 7, 30, tzinfo=UTC)),
        # 04:00 EDT -> 03:30 tomorrow
        (datetime(2026, 10, 6, 8, 0, tzinfo=UTC), datetime(2026, 10, 7, 7, 30, tzinfo=UTC)),
        # The night clocks fall back: 03:30 EST is 08:30 UTC
        (datetime(2026, 11, 1, 6, 0, tzinfo=UTC), datetime(2026, 11, 1, 8, 30, tzinfo=UTC)),
    ],
)
def test_next_run_is_local_wall_clock(now, expected):
    assert backup.next_run(time(3, 30), "America/New_York", now) == expected


# --- export ---------------------------------------------------------------------------------


def test_csv_has_one_row_per_entry_in_local_time(conn, logged):
    out = io.StringIO()
    assert export.write_csv(conn, out, "America/New_York") == 3
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert list(rows[0]) == list(export.CSV_COLUMNS)
    assert [(r["date"], r["time"], r["food"]) for r in rows] == [
        ("2026-10-05", "08:00", "Egg, whole"),
        ("2026-10-05", "08:00", "Egg white"),
        ("2026-10-06", "00:30", "Egg white"),
    ]
    assert rows[1]["brand"] == "Kirkland" and rows[1]["meal_label"] == "breakfast"
    assert float(rows[0]["kcal"]) == pytest.approx(286)
    assert rows[0]["fiber_g"] == ""


def test_csv_date_range_uses_local_days(conn, logged):
    out = io.StringIO()
    day = date(2026, 10, 6)  # 04:30Z on the 6th is 00:30 local, still the 6th
    assert export.write_csv(conn, out, "America/New_York", day, day) == 1
    out = io.StringIO()
    assert export.write_csv(conn, out, "America/New_York", end=date(2026, 10, 5)) == 2


def test_json_holds_everything(conn, logged):
    out = io.StringIO()
    assert export.write_json(conn, out, "America/New_York") == 2
    data = json.loads(out.getvalue())
    assert data["schema_version"] == db.schema_version(conn)
    assert [f["name"] for f in data["foods"]] == ["Egg, whole", "Egg white"]
    first, second = data["meals"]
    assert first["eaten_at"] == "2026-10-05T08:00:00-04:00" and len(first["entries"]) == 2
    assert second["note"] == "late snack"
    assert data["aliases"] == [{"alias": "my whites", "food_id": logged[1], "default_grams": 300}]
    assert data["meal_templates"][0]["name"] == "usual breakfast"
    assert len(data["meal_templates"][0]["items"]) == 2
    assert data["targets"]["protein_g"] == 180


# --- the chomp command ----------------------------------------------------------------------


@pytest.fixture
def env(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", settings.db_path)
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    for name in ("PUBLIC_URL", "HOST"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_cli_export_and_backup(env, conn, logged, capsys):
    assert (
        cli.main(["export", "--format", "csv", "--start", "2026-10-05", "--end", "2026-10-05"]) == 0
    )
    assert capsys.readouterr().out.count("\n") == 3  # header + 2 entries
    target = env / "log.json"
    assert cli.main(["export", "--format", "json", "-o", str(target)]) == 0
    assert len(json.loads(target.read_text())["meals"]) == 2

    assert cli.main(["backup", "--keep", "2"]) == 0
    made = capsys.readouterr().out.strip()
    assert backup.NAME_RE.match(made.rsplit("/", 1)[-1])
    assert cli.main(["restore", made]) == 2  # needs --yes
    conn.close()
    assert cli.main(["restore", made, "--yes"]) == 0


def test_cli_rejects_bad_input(env, conn, capsys):
    assert (
        cli.main(["export", "--format", "csv", "--start", "2026-10-06", "--end", "2026-10-01"]) == 2
    )
    with pytest.raises(SystemExit):
        cli.main(["export", "--format", "csv", "--start", "yesterday"])
    with pytest.raises(SystemExit):
        cli.main(["backup", "--at", "3am"])


def test_cli_export_without_database(env, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(env / "missing.db"))
    assert cli.main(["export", "--format", "json"]) == 1
    assert not (env / "missing.db").exists()


def test_cli_serve_refuses_public_bind_without_auth(env, monkeypatch):
    monkeypatch.setenv("HOST", "0.0.0.0")
    assert cli.main(["serve"]) == 1
