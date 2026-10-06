"""SQLite connection and migrations.

SCHEMA.sql is baseline version 1. Files in migrations/ named NNNN_<name>.sql (NNNN >= 2)
are applied in order, each in its own transaction, and tracked with PRAGMA user_version.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

BASELINE_VERSION = 1
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    pass


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign keys on, WAL for file DBs, and dict-like rows."""
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block as one write transaction (connections are in autocommit mode)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def list_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    """Numbered migration files above the baseline, sorted by version."""
    if not migrations_dir.is_dir():
        return []
    found: dict[int, Path] = {}
    for p in migrations_dir.iterdir():
        m = _MIGRATION_RE.match(p.name)
        if not m:
            if p.suffix.lower() == ".sql":
                raise MigrationError(f"{p.name}: migration files must be named NNNN_<name>.sql")
            continue
        version = int(m.group(1))
        if version <= BASELINE_VERSION:
            raise MigrationError(f"{p.name}: versions start at {BASELINE_VERSION + 1:04d}")
        if version in found:
            raise MigrationError(f"duplicate migration version {version:04d}")
        found[version] = p
    versions = sorted(found)
    expected = list(range(BASELINE_VERSION + 1, BASELINE_VERSION + 1 + len(versions)))
    if versions != expected:
        raise MigrationError(f"migration versions must be contiguous, got {versions}")
    return [(v, found[v]) for v in versions]


def _apply(conn: sqlite3.Connection, sql: str, version: int, label: str) -> None:
    # executescript() runs outside Python's transaction handling, so the script carries
    # its own BEGIN/COMMIT and a failure rolls the whole step back.
    try:
        conn.executescript(f"BEGIN;\n{sql}\n;PRAGMA user_version = {version};\nCOMMIT;")
    except sqlite3.Error as e:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(f"{label} failed: {e}") from e


def latest_version(migrations_dir: Path) -> int:
    """The schema version this code expects once all migrations have run."""
    migrations = list_migrations(migrations_dir)
    return migrations[-1][0] if migrations else BASELINE_VERSION


def migrate(conn: sqlite3.Connection, schema_path: Path, migrations_dir: Path) -> int:
    """Bring the database up to the latest version. Returns the resulting version."""
    migrations = list_migrations(migrations_dir)
    version = schema_version(conn)
    if version == 0:
        has_tables = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
        ).fetchone()
        if has_tables:
            raise MigrationError("database has tables but no schema version; refusing to guess")
        _apply(conn, schema_path.read_text(), BASELINE_VERSION, schema_path.name)
        version = BASELINE_VERSION
    latest = migrations[-1][0] if migrations else BASELINE_VERSION
    if version > latest:
        raise MigrationError(f"database is at version {version}, code only knows {latest}")
    for v, path in migrations:
        if v > version:
            _apply(conn, path.read_text(), v, path.name)
            version = v
    return version


def open_db(path: str | Path, schema_path: Path, migrations_dir: Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn, schema_path, migrations_dir)
    return conn
