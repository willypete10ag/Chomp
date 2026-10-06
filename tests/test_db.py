from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chomp import db
from tests.conftest import MIGRATIONS, SCHEMA

TABLES = {
    "foods",
    "aliases",
    "meals",
    "entries",
    "meal_templates",
    "meal_template_items",
    "targets",
}


def add_food(conn, name="Egg, whole, raw", brand=None, source="fdc", source_id="171287"):
    return conn.execute(
        "INSERT INTO foods (name, brand, source, source_id, kcal, protein_g, carbs_g, fat_g)"
        " VALUES (?, ?, ?, ?, 143, 12.6, 0.7, 9.5)",
        (name, brand, source, source_id),
    ).lastrowid


def test_baseline_creates_schema_at_v1(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names >= TABLES | {"foods_fts"}
    assert db.schema_version(conn) == 1


def test_connection_pragmas(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn, SCHEMA, MIGRATIONS) == 1
    assert db.migrate(conn, SCHEMA, MIGRATIONS) == 1


def test_applies_numbered_migrations_in_order(tmp_path: Path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0003_b.sql").write_text("ALTER TABLE t ADD COLUMN b TEXT;")
    (mig / "0002_a.sql").write_text("CREATE TABLE t (a TEXT);")
    (mig / "README.md").write_text("ignored")
    conn = db.connect(tmp_path / "x.db")
    assert db.migrate(conn, SCHEMA, mig) == 3
    cols = [r[1] for r in conn.execute("PRAGMA table_info(t)")]
    assert cols == ["a", "b"]


def test_failed_migration_rolls_back(tmp_path: Path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0002_bad.sql").write_text("CREATE TABLE half (a TEXT); SELECT nope FROM missing;")
    conn = db.connect(tmp_path / "x.db")
    with pytest.raises(db.MigrationError, match=r"0002_bad\.sql"):
        db.migrate(conn, SCHEMA, mig)
    assert db.schema_version(conn) == 1
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'half'").fetchone() is None


@pytest.mark.parametrize(
    ("files", "match"),
    [
        (["0001_x.sql"], "start at 0002"),
        (["0002_a.sql", "0004_c.sql"], "contiguous"),
    ],
)
def test_rejects_bad_migration_sets(tmp_path: Path, files, match):
    for f in files:
        (tmp_path / f).write_text("SELECT 1;")
    with pytest.raises(db.MigrationError, match=match):
        db.list_migrations(tmp_path)


def test_refuses_unversioned_db_with_tables(tmp_path: Path):
    conn = db.connect(tmp_path / "x.db")
    conn.execute("CREATE TABLE stray (a)")
    with pytest.raises(db.MigrationError, match="no schema version"):
        db.migrate(conn, SCHEMA, MIGRATIONS)


def test_refuses_db_newer_than_code(conn):
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(db.MigrationError, match="version 99"):
        db.migrate(conn, SCHEMA, MIGRATIONS)


def test_fts_tracks_inserts_updates_deletes(conn):
    fid = add_food(conn, name="Liquid egg whites", brand="Kirkland", source_id="1")

    def hits(q):
        sql = "SELECT rowid FROM foods_fts WHERE foods_fts MATCH ?"
        return [r[0] for r in conn.execute(sql, (q,))]

    assert hits("egg") == [fid]
    assert hits("kirkland") == [fid]
    conn.execute("UPDATE foods SET name = 'Greek yogurt' WHERE id = ?", (fid,))
    assert hits("egg") == []
    assert hits("yogurt") == [fid]
    conn.execute("DELETE FROM foods WHERE id = ?", (fid,))
    assert hits("yogurt") == []


def test_source_record_is_unique(conn):
    add_food(conn, source_id="171287")
    with pytest.raises(sqlite3.IntegrityError):
        add_food(conn, source_id="171287")
    # Foods without a source id (manual entries) are not constrained.
    add_food(conn, source="manual", source_id=None)
    add_food(conn, source="manual", source_id=None)


def test_template_can_hold_same_food_twice(conn):
    fid = add_food(conn)
    tid = conn.execute("INSERT INTO meal_templates (name) VALUES ('usual breakfast')").lastrowid
    for grams in (100, 50):
        conn.execute(
            "INSERT INTO meal_template_items (template_id, food_id, grams) VALUES (?, ?, ?)",
            (tid, fid, grams),
        )
    n = conn.execute("SELECT COUNT(*) FROM meal_template_items WHERE template_id = ?", (tid,))
    assert n.fetchone()[0] == 2


def test_entries_snapshot_all_nutrients(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    assert {"kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg"} <= cols


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO aliases (alias, food_id) VALUES ('ghost', 999)")
