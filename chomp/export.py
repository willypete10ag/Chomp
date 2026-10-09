"""CSV and JSON export of the meal log (#34).

CSV is one row per logged entry, for spreadsheets. JSON is everything a person would want to
keep: foods, meals with their entries, portions, meal templates and targets. Times are local
ISO 8601 with offset; nutrient values are the stored snapshots, unrounded. An export covers
the user the connection was opened for (#46); foods are shared and always exported in full.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import UTC, date, datetime
from typing import TextIO
from zoneinfo import ZoneInfo

from chomp import __version__, db, logic
from chomp.store import user_of

NUTRIENTS = logic.NUTRIENTS
CSV_COLUMNS = (
    "date",
    "time",
    "meal_id",
    "meal_label",
    "entry_id",
    "food_id",
    "food",
    "brand",
    "grams",
    "estimated",
    *NUTRIENTS,
)


def _bounds(start: date | None, end: date | None, tz: str) -> tuple[str, str]:
    lo = logic.local_day_bounds(start, tz)[0] if start else ""
    hi = logic.local_day_bounds(end, tz)[1] if end else "￿"
    return lo, hi


def _entries(conn: sqlite3.Connection, lo: str, hi: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id AS meal_id, m.eaten_at, m.label AS meal_label, m.note AS meal_note,"
        " e.id AS entry_id, e.food_id, f.name AS food, f.brand, e.grams, e.estimated,"
        f" {', '.join(f'e.{k}' for k in NUTRIENTS)}"
        " FROM entries e JOIN meals m ON m.id = e.meal_id JOIN foods f ON f.id = e.food_id"
        " WHERE m.user_id = ? AND m.eaten_at >= ? AND m.eaten_at < ?"
        " ORDER BY m.eaten_at, m.id, e.id",
        (user_of(conn), lo, hi),
    ).fetchall()


def write_csv(
    conn: sqlite3.Connection,
    out: TextIO,
    tz: str,
    start: date | None = None,
    end: date | None = None,
) -> int:
    """Write one CSV row per entry; returns the number of rows."""
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    rows = _entries(conn, *_bounds(start, end, tz))
    for r in rows:
        local = logic.parse_utc(r["eaten_at"]).astimezone(ZoneInfo(tz))
        writer.writerow(
            [
                local.date().isoformat(),
                local.strftime("%H:%M"),
                r["meal_id"],
                r["meal_label"] or "",
                r["entry_id"],
                r["food_id"],
                r["food"],
                r["brand"] or "",
                r["grams"],
                r["estimated"],
                *("" if r[k] is None else r[k] for k in NUTRIENTS),
            ]
        )
    return len(rows)


def _dicts(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def to_json(
    conn: sqlite3.Connection, tz: str, start: date | None = None, end: date | None = None
) -> dict:
    """Everything in the log as plain data. A date range limits meals only; foods,
    portions, templates and targets are always included in full."""
    lo, hi = _bounds(start, end, tz)
    meals: dict[int, dict] = {}
    for r in _entries(conn, lo, hi):
        meal = meals.setdefault(
            r["meal_id"],
            {
                "meal_id": r["meal_id"],
                "eaten_at": logic.local_iso(r["eaten_at"], tz),
                "label": r["meal_label"],
                "note": r["meal_note"],
                "entries": [],
            },
        )
        meal["entries"].append(
            {
                "entry_id": r["entry_id"],
                "food_id": r["food_id"],
                "food": r["food"],
                "brand": r["brand"],
                "grams": r["grams"],
                "estimated": bool(r["estimated"]),
                **{k: r[k] for k in NUTRIENTS},
            }
        )
    user_id = user_of(conn)
    templates = []
    for t in _dicts(
        conn, "SELECT id, name FROM meal_templates WHERE user_id = ? ORDER BY name", (user_id,)
    ):
        items = _dicts(
            conn,
            "SELECT food_id, grams FROM meal_template_items WHERE template_id = ? ORDER BY id",
            (t["id"],),
        )
        templates.append({"name": t["name"], "items": items})
    targets = conn.execute(
        "SELECT kcal, protein_g, carbs_g, fat_g FROM targets WHERE user_id = ?", (user_id,)
    ).fetchone()
    return {
        "chomp_version": __version__,
        "schema_version": db.schema_version(conn),
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "user": conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()[0],
        "tz": tz,
        "range": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
        "foods": _dicts(
            conn,
            "SELECT id AS food_id, name, brand, gtin, source, source_id, verified, saved,"
            f" {', '.join(NUTRIENTS)}, serving_g, serving_desc, created_at, updated_at"
            " FROM foods ORDER BY id",
        ),
        "meals": list(meals.values()),
        "portions": [
            {**p, "estimated": bool(p["estimated"])}
            for p in _dicts(
                conn,
                "SELECT food_id, name, grams, estimated FROM portions WHERE user_id = ?"
                " ORDER BY food_id, name",
                (user_id,),
            )
        ],
        "meal_templates": templates,
        "targets": dict(targets) if targets else None,
    }


def write_json(
    conn: sqlite3.Connection,
    out: TextIO,
    tz: str,
    start: date | None = None,
    end: date | None = None,
) -> int:
    """Write the JSON export; returns the number of meals."""
    data = to_json(conn, tz, start, end)
    json.dump(data, out, indent=2, ensure_ascii=False)
    out.write("\n")
    return len(data["meals"])
