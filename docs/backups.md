# Backups, restore and export

The `backup` container copies the database every night at `BACKUP_AT` (local `TZ`) using
SQLite's online backup, integrity-checks the copy, and keeps the newest `BACKUP_KEEP`.
Food photos (kept in the data volume at `/data/photos`) go to a `photos/` folder beside the
backups. A restore keeps photos only for foods in the restored database (taking any missing
ones from the backup) and moves the old photo folder aside as `photos.pre-restore-<time>`.
Back up right now with `docker compose exec backup chomp backup`.

To restore (this replaces the whole database; the current one is kept as
`chomp.db.pre-restore-<time>` in the data volume):

```bash
ls /mnt/backup/chomp                    # pick a file
docker compose stop chomp backup
docker compose run --rm --no-deps backup chomp restore /backups/chomp-YYYYMMDD-HHMMSS.db --yes
docker compose up -d
```

## Export

CSV is one row per logged entry (for spreadsheets); JSON is everything (foods, meals and
entries, portions, templates, targets). `--start` and `--end` are inclusive local dates. An
export is the owner's log unless you add `--user NAME`.

```bash
docker compose exec -T chomp chomp export --format csv > chomp.csv
docker compose exec -T chomp chomp export --format json --start 2026-10-01 > october.json
```

Locally, `uv run --env-file .env chomp export --format csv` does the same.
