"""The `chomp` command.

    chomp                      run the MCP server (same as `chomp serve`)
    chomp backup [--daily]     back up the database now, or nightly at BACKUP_AT
    chomp restore FILE --yes   replace the database with a backup (stop the server first)
    chomp export --format csv|json [--user NAME] [--start D] [--end D] [-o FILE]
    chomp import-hevy FILE [--user NAME]
                               import a Hevy workout CSV export (repeats are skipped)
    chomp sync-hevy [--full] [--user NAME]
                               pull new, edited and deleted workouts from Hevy's API now,
                               for everyone with a key (theirs from Integrations, or
                               HEVY_API_KEY); --full pulls every workout again
    chomp import-health FILE [--user NAME]
                               import past weight and activity from Apple Health's
                               export.zip (days the phone already sent are kept)
    chomp user list            who keeps a log here
    chomp user add NAME        add a person with their own log and password (asks for it)
                               (the dashboard's You > People does it with an invite link)
    chomp user password NAME   change someone's password (signs them out everywhere)
    chomp user rename OLD NEW  change someone's display name
    chomp user ingest-token NAME [--revoke]
                               make (or replace, or remove) the token their phone uses to
                               post Apple Health data to /ingest/health

Settings come from the environment (see .env.example).
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from contextlib import closing
from datetime import date, time
from pathlib import Path

from chomp import (
    auth,
    backup,
    db,
    export,
    health,
    health_export,
    hevy,
    hevy_sync,
    logic,
    store,
    users,
)
from chomp.config import Settings


def _time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected HH:MM, got {value!r}") from None


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None


def parser(settings: Settings) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chomp", description="Chomp meal-log MCP server")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server (default)")

    b = sub.add_parser("backup", help="back up the database")
    b.add_argument("--dest", default=settings.backup_dir, help="backup folder (BACKUP_DIR)")
    b.add_argument("--keep", type=int, default=settings.backup_keep, help="copies to keep")
    b.add_argument(
        "--daily",
        action="store_true",
        help="keep running and back up every day at --at (for the backup container)",
    )
    b.add_argument("--at", type=_time, default=settings.backup_at, help="local HH:MM (BACKUP_AT)")

    r = sub.add_parser("restore", help="replace the database with a backup file")
    r.add_argument("file", help="backup file, e.g. /backups/chomp-20261006-033000.db")
    r.add_argument("--yes", action="store_true", help="confirm; the server must be stopped")

    e = sub.add_parser("export", help="export the log as CSV (entries) or JSON (everything)")
    e.add_argument("--format", choices=("csv", "json"), required=True)
    e.add_argument("--start", type=_date, help="first local date, YYYY-MM-DD")
    e.add_argument("--end", type=_date, help="last local date, inclusive")
    e.add_argument("-o", "--output", help="file to write (default: standard output)")
    e.add_argument("--user", help="whose log to export (default: the owner)")

    h = sub.add_parser("import-hevy", help="import a Hevy workout CSV export")
    h.add_argument("file", help="the CSV from Hevy's Export Workouts, or - for standard input")
    h.add_argument("--user", help="whose workouts these are (default: the owner)")
    hs = sub.add_parser("sync-hevy", help="pull workouts from Hevy's API now")
    hs.add_argument("--full", action="store_true", help="pull every workout again")
    hs.add_argument("--user", help="only this person (default: everyone with a key)")

    hh = sub.add_parser("import-health", help="import Apple Health's export.zip")
    hh.add_argument(
        "file", help="export.zip from Health's Export All Health Data, or - for standard input"
    )
    hh.add_argument("--user", help="whose health data this is (default: the owner)")

    u = sub.add_parser("user", help="manage the people who keep a log here")
    us = u.add_subparsers(dest="action", required=True)
    us.add_parser("list", help="list users")
    a = us.add_parser("add", help="add a user; asks for their password")
    a.add_argument("name")
    pw = us.add_parser("password", help="change a user's password; asks for it")
    pw.add_argument("name")
    rn = us.add_parser("rename", help="change a user's display name")
    rn.add_argument("name")
    rn.add_argument("new_name")
    it = us.add_parser(
        "ingest-token", help="make the token a phone posts health data with (replaces the old)"
    )
    it.add_argument("name")
    it.add_argument("--revoke", action="store_true", help="remove the token instead")
    return p


def _new_password() -> str:
    """Ask twice, without echo. Piped input (no terminal) is read as one line."""
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password (at least 12 characters): ")
    if getpass.getpass("Same password again: ") != first:
        raise users.UserError("the passwords didn't match")
    return first


def user_command(args, settings: Settings) -> None:
    with closing(db.open_db(settings.db_path, settings.schema_path, settings.migrations_dir)) as c:
        if args.action == "list":
            for row in users.list_users(c):
                owner = (
                    " (owner, signs in with AUTH_PASSWORD)" if row["id"] == users.OWNER_ID else ""
                )
                print(f"{row['name']}: {row['meals']} meals{owner}")
        elif args.action == "add":
            users.add_user(c, args.name, _new_password(), settings.auth_password)
            print(f"added {' '.join(args.name.split())}; they can sign in with that password now")
        elif args.action == "password":
            user = users.get_by_name(c, args.name)
            users.set_password(c, user["id"], _new_password(), settings.auth_password)
            print(f"changed {user['name']}'s password and signed them out everywhere")
        elif args.action == "rename":
            user = users.get_by_name(c, args.name)
            users.rename(c, user["id"], args.new_name)
            print(f"renamed {user['name']} to {' '.join(args.new_name.split())}")
        elif args.action == "ingest-token":
            user = users.get_by_name(c, args.name)
            if args.revoke:
                gone = health.revoke_token(c, user["id"])
                print(f"removed {user['name']}'s ingest token" if gone else "there was none")
            else:
                token = health.new_token(c, user["id"])
                print(f"{user['name']}'s ingest token (shown once; any older one stops working):")
                print(token)
                print(f"Post to {health.PATH} with the header: Authorization: Bearer <token>")


def import_health(args, settings: Settings) -> None:
    with closing(
        db.open_db(settings.db_path, settings.schema_path, settings.migrations_dir)
    ) as conn:
        conn.user_id = users.get_by_name(conn, args.user)["id"] if args.user else users.OWNER_ID
        print("reading the export (a few minutes for years of data)...", file=sys.stderr)
        if args.file == "-":
            with health_export.spool(sys.stdin.buffer) as copy:
                parsed = health_export.read(copy, settings.tz)
        else:
            with open(args.file, "rb") as f:
                parsed = health_export.read(f, settings.tz)
        weights = store.add_weights(conn, parsed.weights.items(), "health")
        activity = store.import_activity_days(conn, health_export.day_rows(parsed, settings.tz))
    first = min((start for _, start in parsed.days), default=None)
    print(
        f"{weights.added} weigh-ins added, {weights.updated} updated,"
        f" {weights.unchanged} already there; {activity.added} days of activity added,"
        f" {activity.updated} updated, {activity.unchanged} already there,"
        f" {activity.kept} kept as the phone sent them"
        + (f" (from {logic.local_date(first, settings.tz)})" if first else "")
        + f"; {parsed.rejected} samples skipped"
    )
    for problem in parsed.problems:
        print(f"  {problem}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    settings = Settings.from_env()
    args = parser(settings).parse_args(argv)
    log = logging.getLogger("chomp")
    try:
        if args.command in (None, "serve"):
            from chomp import server

            server.main()
        elif args.command == "backup":
            if args.daily:
                backup.run_daily(
                    settings.db_path,
                    args.dest,
                    args.keep,
                    args.at,
                    settings.tz,
                    settings.photos_path,
                )
            else:
                print(
                    backup.backup(
                        settings.db_path, args.dest, args.keep, photos_dir=settings.photos_path
                    )
                )
        elif args.command == "restore":
            if not args.yes:
                print(
                    "Restoring replaces the whole database. Stop the server first, then rerun"
                    " with --yes.",
                    file=sys.stderr,
                )
                return 2
            saved = backup.restore(
                args.file, settings.db_path, settings.migrations_dir, settings.photos_path
            )
            if saved:
                print(f"previous database saved as {saved}")
            print(f"restored {args.file}; start the server to apply any newer migrations")
        elif args.command == "export":
            if args.start and args.end and args.end < args.start:
                print("--end is before --start", file=sys.stderr)
                return 2
            if not Path(settings.db_path).is_file():
                log.error("no database at %s (check DB_PATH)", settings.db_path)
                return 1
            write = export.write_csv if args.format == "csv" else export.write_json
            with closing(db.connect(settings.db_path)) as conn:
                conn.user_id = (
                    users.get_by_name(conn, args.user)["id"] if args.user else users.OWNER_ID
                )
                if args.output:
                    with open(args.output, "w", encoding="utf-8", newline="") as out:
                        n = write(conn, out, settings.tz, args.start, args.end)
                else:
                    n = write(conn, sys.stdout, settings.tz, args.start, args.end)
            log.info("exported %d %s", n, "entries" if args.format == "csv" else "meals")
        elif args.command == "import-hevy":
            data = sys.stdin.buffer.read() if args.file == "-" else Path(args.file).read_bytes()
            parsed = hevy.parse(data, settings.tz)
            with closing(
                db.open_db(settings.db_path, settings.schema_path, settings.migrations_dir)
            ) as conn:
                conn.user_id = (
                    users.get_by_name(conn, args.user)["id"] if args.user else users.OWNER_ID
                )
                counts = store.import_workouts(conn, parsed.workouts, "hevy")
            print(
                f"{counts.added} workouts added, {counts.updated} updated,"
                f" {counts.unchanged} already there; {parsed.rejected} rows skipped"
                + (f" ({parsed.incomplete} workouts left out)" if parsed.incomplete else "")
            )
            for problem in parsed.problems:
                print(f"  {problem}")
        elif args.command == "sync-hevy":
            with closing(
                db.open_db(settings.db_path, settings.schema_path, settings.migrations_dir)
            ) as conn:  # applies migrations before syncing
                if args.user:
                    user = users.get_by_name(conn, args.user)
                    if not hevy_sync.key_for(conn, settings, user["id"]):
                        log.error("%s has no Hevy API key", user["name"])
                        return 1
                    names = {user["id"]: user["name"]}
                else:
                    names = {r["id"]: r["name"] for r in users.list_users(conn)}
                    if not hevy_sync.keyed_users(conn, settings):
                        log.error(
                            "nobody has a Hevy API key: set HEVY_API_KEY (see .env.example)"
                            " or paste one on the dashboard's Integrations page"
                        )
                        return 1
            if args.user:
                (user_id,) = names
                results = {user_id: hevy_sync.run(settings, user_id, full=args.full)}
            else:
                results = hevy_sync.run_all(settings, full=args.full)
            for user_id, result in results.items():
                print(f"{names.get(user_id, user_id)}: {result.summary()}")
            return 1 if any(r.error for r in results.values()) else 0
        elif args.command == "import-health":
            import_health(args, settings)
        elif args.command == "user":
            user_command(args, settings)
    except (
        auth.AuthConfigError,
        backup.BackupError,
        db.MigrationError,
        health_export.ExportError,
        hevy.HevyError,
        users.UserError,
        OSError,
    ) as err:
        log.error("%s", err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
