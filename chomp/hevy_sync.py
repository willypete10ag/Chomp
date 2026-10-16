"""Hevy API sync (#113): workouts come in from Hevy by themselves, with edits and deletes.

The first run pulls every workout on the account; it adopts workouts the CSV import brought in
earlier (matched by start minute) so nothing is doubled. Later runs read Hevy's workout events
feed since the last run: an `updated` event saves the workout over the copy with the same Hevy
id, a `deleted` one removes it. The cursor only moves after a run without errors, and each run
asks again from a few minutes before the last one started, so a missed event is picked up on
the next run; saving the same workout twice changes nothing.

The server runs this every HEVY_SYNC_MINUTES in a background thread when HEVY_API_KEY is set;
the dashboard's Sync now and `chomp sync-hevy` run it on demand.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from chomp import db, hevy, logic, store, users
from chomp.config import Settings
from chomp.sources import SourceError
from chomp.sources.hevy import HevyClient

log = logging.getLogger("chomp.hevy_sync")

OVERLAP = timedelta(minutes=10)  # each run re-asks for this much before the last one began
FIRST_RUN_DELAY_S = 15.0
_lock = threading.Lock()  # one run at a time: the background loop or Sync now


@dataclass
class SyncResult:
    full: bool = False  # pulled every workout (the first run, or --full)
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped: int = 0  # workouts Hevy sent that couldn't be read or saved
    error: str | None = None

    def summary(self) -> str:
        if self.error:
            return f"Hevy sync failed: {self.error}"
        return (
            f"{self.added} workouts added, {self.updated} updated, {self.deleted} deleted,"
            f" {self.unchanged} unchanged" + (f", {self.skipped} skipped" if self.skipped else "")
        )


def _stamp(event: dict) -> str:
    """When an event happened, for picking the latest event per workout."""
    if event.get("type") == "deleted":
        return str(event.get("deleted_at") or "")
    workout = event.get("workout") or {}
    return str(workout.get("updated_at") or "")


def latest_events(events) -> list[dict]:
    """The last event for each workout id: a workout edited twice, or edited then deleted,
    since the last run only counts as its final state."""
    last: dict[str, dict] = {}
    for event in events:
        kind = event.get("type")
        if kind == "deleted":
            key = str(event.get("id") or "").strip()
        elif kind == "updated" and isinstance(event.get("workout"), dict):
            key = str(event["workout"].get("id") or "").strip()
        else:
            log.warning("hevy: skipping an event of unknown type %r", kind)
            continue
        if not key:
            continue
        seen = last.get(key)
        # A delete is final, so it wins a tie
        if (
            seen is None
            or _stamp(event) > _stamp(seen)
            or (_stamp(event) == _stamp(seen) and kind == "deleted")
        ):
            last[key] = event
    return list(last.values())


def sync(
    conn: sqlite3.Connection,
    client: HevyClient,
    *,
    full: bool = False,
    now: datetime | None = None,
) -> SyncResult:
    """One sync run for the connection's user. Records how it went in `hevy_sync`."""
    started = now or datetime.now(UTC)
    state = store.hevy_sync_state(conn)
    since = None if full or state is None else state["since"]
    result = SyncResult(full=since is None)
    try:
        if since is None:
            events = [{"type": "updated", "workout": w} for w in client.workouts()]
        else:
            events = latest_events(client.events(since))
        for event in events:
            if event["type"] == "deleted":
                if store.delete_hevy_workout(conn, str(event["id"]).strip()):
                    result.deleted += 1
                continue
            raw = event["workout"]
            try:
                workout = hevy.from_api(raw)
                done = store.save_hevy_workout(conn, workout)
            except (hevy.HevyError, sqlite3.IntegrityError) as e:
                result.skipped += 1
                log.warning("hevy: skipped workout %s: %s", raw.get("id"), e)
                continue
            setattr(result, done, getattr(result, done) + 1)
    except SourceError as e:
        result.error = str(e)
        store.record_hevy_sync(conn, logic.format_utc(datetime.now(UTC)), error=result.error)
        log.warning("hevy: sync failed: %s", e)
        return result
    if result.skipped:
        # Keep the cursor, so the next run asks for those workouts again rather than moving
        # past them for good; the ones saved this time come back unchanged
        n = result.skipped
        result.error = (
            f"{n} workout{'' if n == 1 else 's'} from Hevy couldn't be saved (see the server"
            " log); the next sync tries again"
        )
        store.record_hevy_sync(conn, logic.format_utc(datetime.now(UTC)), error=result.error)
        log.warning("hevy: %s", result.summary())
        return result
    store.record_hevy_sync(
        conn,
        logic.format_utc(datetime.now(UTC)),
        since=logic.format_utc(started - OVERLAP),
        added=result.added,
        updated=result.updated,
        deleted=result.deleted,
    )
    if result.added or result.updated or result.deleted or result.full:
        log.info("hevy: %s%s", "full pull, " if result.full else "", result.summary())
    return result


def user_id(conn: sqlite3.Connection, settings: Settings) -> int:
    """Whose workouts the key brings in: HEVY_USER, or the owner."""
    return (
        users.get_by_name(conn, settings.hevy_user)["id"]
        if settings.hevy_user
        else (users.OWNER_ID)
    )


def run(settings: Settings, *, full: bool = False, client: HevyClient | None = None) -> SyncResult:
    """One run against the database in settings, waiting for any run already going."""
    if not settings.hevy_api_key and client is None:
        return SyncResult(error="HEVY_API_KEY is not set")
    with _lock, closing(db.connect(settings.db_path)) as conn:
        conn.user_id = user_id(conn, settings)
        if client is not None:
            return sync(conn, client, full=full)
        with HevyClient(settings.hevy_api_key) as own:
            return sync(conn, own, full=full)


def start_background(
    settings: Settings, stop: threading.Event | None = None
) -> threading.Thread | None:
    """Run the sync every HEVY_SYNC_MINUTES in a daemon thread; None when no key is set."""
    if not settings.hevy_api_key:
        return None
    stop = stop or threading.Event()
    every = settings.hevy_sync_minutes * 60

    def loop() -> None:
        wait = FIRST_RUN_DELAY_S
        while not stop.wait(wait):
            try:
                run(settings)
            except Exception:  # keep the loop alive; the next run tries again
                log.exception("hevy: sync run crashed")
            wait = every

    thread = threading.Thread(target=loop, name="hevy-sync", daemon=True)
    thread.start()
    log.info("hevy sync on: every %d min", settings.hevy_sync_minutes)
    return thread
