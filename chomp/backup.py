"""Nightly SQLite backups with retention, and restore (#33).

Backups use SQLite's online backup API (what `sqlite3 .backup` uses), so they are consistent
while the server is running. Each copy is integrity-checked before it gets its final name.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time as _time
from contextlib import closing
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from chomp import db

log = logging.getLogger("chomp.backup")

NAME_RE = re.compile(r"^chomp-\d{8}-\d{6}\.db$")


class BackupError(RuntimeError):
    pass


def _check(path: Path) -> int:
    """Integrity-check a database file; returns its schema version."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError(f"{path.name} failed integrity check: {result}")
        return db.schema_version(conn)


def backups(dest_dir: Path) -> list[Path]:
    """Chomp backups in dest_dir, oldest first (names sort by time)."""
    if not dest_dir.is_dir():
        return []
    return sorted(p for p in dest_dir.iterdir() if NAME_RE.match(p.name))


def backup(
    db_path: str | Path, dest_dir: str | Path, keep: int, now: datetime | None = None
) -> Path:
    """Copy the live database to dest_dir/chomp-YYYYMMDD-HHMMSS.db and keep the newest `keep`."""
    if keep < 1:
        raise BackupError("keep must be at least 1")
    src_path = Path(db_path)
    if not src_path.exists():
        raise BackupError(f"database {src_path} does not exist")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    final = dest_dir / f"chomp-{stamp}.db"
    partial = dest_dir / f".{final.name}.partial"
    partial.unlink(missing_ok=True)
    try:
        with closing(db.connect(src_path)) as src, closing(sqlite3.connect(partial)) as dst:
            src.backup(dst)
        _check(partial)
        partial.replace(final)
    finally:
        partial.unlink(missing_ok=True)
    for old in backups(dest_dir)[:-keep]:
        old.unlink()
        log.info("pruned %s", old.name)
    log.info("backed up to %s", final)
    return final


def restore(backup_file: str | Path, db_path: str | Path, migrations_dir: Path) -> Path | None:
    """Replace the database at db_path with backup_file. Stop the server first.

    The current database is first saved next to it as <name>.pre-restore-<time>; that path
    is returned (None if there was no database)."""
    src_path = Path(backup_file)
    if not src_path.is_file():
        raise BackupError(f"{src_path} not found")
    version = _check(src_path)
    latest = db.latest_version(migrations_dir)
    if not 1 <= version <= latest:
        raise BackupError(
            f"{src_path.name} is schema version {version}; this Chomp knows 1-{latest}"
        )
    target = Path(db_path)
    saved = None
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        saved = target.with_name(f"{target.name}.pre-restore-{stamp}")
        with closing(db.connect(target)) as cur, closing(sqlite3.connect(saved)) as out:
            cur.backup(out)
    with closing(sqlite3.connect(src_path)) as src, closing(db.connect(target)) as dst:
        src.backup(dst)
    log.info("restored %s into %s (schema version %d)", src_path.name, target, version)
    return saved


def next_run(at: time, tz: str, now: datetime) -> datetime:
    """The next local wall-clock `at` strictly after `now`."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    candidate = datetime.combine(local.date(), at, tzinfo=zone)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), at, tzinfo=zone)
    return candidate


def run_daily(db_path: str, dest_dir: str, keep: int, at: time, tz: str) -> None:
    """Back up once a day at local time `at`, forever. Failures are logged and retried
    the next day, so one bad night never stops the schedule."""
    zone = ZoneInfo(tz)
    log.info("nightly backups at %s %s to %s, keeping %d", at.strftime("%H:%M"), tz, dest_dir, keep)
    while True:
        target = next_run(at, tz, datetime.now(zone))
        while (wait := (target - datetime.now(zone)).total_seconds()) > 0:
            _time.sleep(min(wait, 300))  # wake periodically so clock changes can't strand us
        try:
            backup(db_path, dest_dir, keep, now=datetime.now(zone))
        except Exception:
            log.exception("backup failed; will retry at the next scheduled time")
