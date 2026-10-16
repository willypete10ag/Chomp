"""Hevy API sync (#113): workouts come in from Hevy by themselves, with edits and deletes.

The first run pulls every workout on the account; it adopts workouts the CSV import brought in
earlier (matched by start minute) so nothing is doubled. Later runs read Hevy's workout events
feed since the last run: an `updated` event saves the workout over the copy with the same Hevy
id, a `deleted` one removes it. The cursor only moves after a run without errors, and each run
asks again from a few minutes before the last one started, so a missed event is picked up on
the next run; saving the same workout twice changes nothing.

Each person syncs with their own key (#109), pasted on the dashboard's Integrations page;
HEVY_API_KEY in .env is the owner's key (or HEVY_USER's) unless they paste one there. The server
runs this for everyone with a key every HEVY_SYNC_MINUTES in a background thread; the
dashboard's Sync now and `chomp sync-hevy` run it on demand.
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
from chomp.sources.hevy import HevyClient, HevyKeyError

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
    key_refused: bool = False  # Hevy said the key is wrong (or Pro has lapsed)

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
        result.key_refused = isinstance(e, HevyKeyError)
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


def env_user_id(conn: sqlite3.Connection, settings: Settings) -> int:
    """Whose workouts HEVY_API_KEY from .env brings in: HEVY_USER, or the owner."""
    return (
        users.get_by_name(conn, settings.hevy_user)["id"] if settings.hevy_user else users.OWNER_ID
    )


def saved_key(conn: sqlite3.Connection, user_id: int) -> str:
    """The key someone pasted on Integrations, or ""."""
    row = conn.execute("SELECT api_key FROM hevy_keys WHERE user_id = ?", (user_id,)).fetchone()
    return row["api_key"] if row else ""


def key_for(conn: sqlite3.Connection, settings: Settings, user_id: int) -> str:
    """The Hevy key someone syncs with: their own, else HEVY_API_KEY when it is theirs."""
    if key := saved_key(conn, user_id):
        return key
    if settings.hevy_api_key:
        try:
            if env_user_id(conn, settings) == user_id:
                return settings.hevy_api_key
        except users.UserError:
            pass
    return ""


def save_key(conn: sqlite3.Connection, user_id: int, api_key: str) -> None:
    """Keep someone's key and start their sync over: a new key may be another Hevy account,
    so the next run pulls every workout (adopting the ones already here). Waits for a run
    already going, so it can't write back the old key's cursor afterwards."""
    with _lock, db.transaction(conn):
        conn.execute(
            "INSERT INTO hevy_keys (user_id, api_key) VALUES (?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET api_key = excluded.api_key,"
            " created_at = datetime('now')",
            (user_id, api_key),
        )
        conn.execute("DELETE FROM hevy_sync WHERE user_id = ?", (user_id,))


def remove_key(conn: sqlite3.Connection, user_id: int) -> None:
    with _lock, db.transaction(conn):
        conn.execute("DELETE FROM hevy_keys WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM hevy_sync WHERE user_id = ?", (user_id,))


def keyed_users(conn: sqlite3.Connection, settings: Settings) -> list[int]:
    """Everyone who has a key to sync with."""
    ids = {r["user_id"] for r in conn.execute("SELECT user_id FROM hevy_keys")}
    if settings.hevy_api_key:
        try:
            ids.add(env_user_id(conn, settings))
        except users.UserError as e:
            log.warning("hevy: HEVY_API_KEY is set but %s", e)
    return sorted(ids)


def run(
    settings: Settings,
    user_id: int | None = None,
    *,
    full: bool = False,
    client: HevyClient | None = None,
) -> SyncResult:
    """One run for one person (by default HEVY_API_KEY's), waiting for any run already
    going."""
    with _lock, closing(db.connect(settings.db_path)) as conn:
        conn.user_id = env_user_id(conn, settings) if user_id is None else user_id
        if client is not None:
            return sync(conn, client, full=full)
        key = key_for(conn, settings, conn.user_id)
        if not key:
            return SyncResult(error="no Hevy API key for this person")
        with HevyClient(key) as own:
            return sync(conn, own, full=full)


def run_all(settings: Settings, *, full: bool = False) -> dict[int, SyncResult]:
    """One run for everyone with a key, one after another."""
    with closing(db.connect(settings.db_path)) as conn:
        ids = keyed_users(conn, settings)
    return {user_id: run(settings, user_id, full=full) for user_id in ids}


def start_background(settings: Settings, stop: threading.Event | None = None) -> threading.Thread:
    """Run the sync for everyone with a key every HEVY_SYNC_MINUTES in a daemon thread. It
    always runs, since a key may be pasted on the dashboard at any time; with no keys a run
    does nothing."""
    stop = stop or threading.Event()
    every = settings.hevy_sync_minutes * 60

    def loop() -> None:
        wait = FIRST_RUN_DELAY_S
        while not stop.wait(wait):
            try:
                run_all(settings)
            except Exception:  # keep the loop alive; the next run tries again
                log.exception("hevy: sync run crashed")
            wait = every

    thread = threading.Thread(target=loop, name="hevy-sync", daemon=True)
    thread.start()
    log.info("hevy sync: every %d min for everyone with a key", settings.hevy_sync_minutes)
    return thread
