"""The `chomp` command.

    chomp                      run the MCP server (same as `chomp serve`)
    chomp backup [--daily]     back up the database now, or nightly at BACKUP_AT
    chomp restore FILE --yes   replace the database with a backup (stop the server first)
    chomp export --format csv|json [--start D] [--end D] [-o FILE]

Settings come from the environment (see .env.example).
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import closing
from datetime import date, time
from pathlib import Path

from chomp import auth, backup, db, export
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
    return p


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
                backup.run_daily(settings.db_path, args.dest, args.keep, args.at, settings.tz)
            else:
                print(backup.backup(settings.db_path, args.dest, args.keep))
        elif args.command == "restore":
            if not args.yes:
                print(
                    "Restoring replaces the whole database. Stop the server first, then rerun"
                    " with --yes.",
                    file=sys.stderr,
                )
                return 2
            saved = backup.restore(args.file, settings.db_path, settings.migrations_dir)
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
                if args.output:
                    with open(args.output, "w", encoding="utf-8", newline="") as out:
                        n = write(conn, out, settings.tz, args.start, args.end)
                else:
                    n = write(conn, sys.stdout, settings.tz, args.start, args.end)
            log.info("exported %d %s", n, "entries" if args.format == "csv" else "meals")
    except (auth.AuthConfigError, backup.BackupError, db.MigrationError) as err:
        log.error("%s", err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
