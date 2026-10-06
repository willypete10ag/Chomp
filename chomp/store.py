"""Database reads and writes behind the MCP tools. Every function takes an open connection.

Macro math is delegated to logic.py; this module only moves rows around.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from chomp import logic, text
from chomp.db import transaction
from chomp.sources import FoodRecord

FOOD_COLS = (
    "id, name, brand, gtin, source, source_id, verified, kcal, protein_g, carbs_g, fat_g, "
    "fiber_g, sugar_g, sodium_mg, serving_g, serving_desc"
)


class NotFound(LookupError):
    pass


def nutrients_of(row: Mapping) -> logic.Nutrients:
    return {k: row[k] for k in logic.NUTRIENTS}


# --- Foods ----------------------------------------------------------------------------------


def get_food(conn: sqlite3.Connection, food_id: int) -> sqlite3.Row:
    row = conn.execute(f"SELECT {FOOD_COLS} FROM foods WHERE id = ?", (food_id,)).fetchone()
    if row is None:
        raise NotFound(f"no food with id {food_id}")
    return row


def get_foods(conn: sqlite3.Connection, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
    ids = sorted(set(ids))
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT {FOOD_COLS} FROM foods WHERE id IN ({marks})", ids).fetchall()
    found = {r["id"]: r for r in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise NotFound(f"no food with id {', '.join(map(str, missing))}")
    return found


def food_by_gtin(conn: sqlite3.Connection, gtin14: str) -> sqlite3.Row | None:
    return conn.execute(f"SELECT {FOOD_COLS} FROM foods WHERE gtin = ?", (gtin14,)).fetchone()


def cache_record(conn: sqlite3.Connection, rec: FoodRecord) -> tuple[sqlite3.Row, bool]:
    """Store an upstream food unless it is already cached. Returns (row, created).

    An existing row for the same upstream record, or for the same barcode, wins: it may
    have been confirmed or corrected by the user, and its id may already be in the log.
    """
    with transaction(conn):
        row = conn.execute(
            f"SELECT {FOOD_COLS} FROM foods WHERE source = ? AND source_id = ?",
            (rec.source, rec.source_id),
        ).fetchone()
        if row is None and rec.gtin:
            row = food_by_gtin(conn, rec.gtin)
        if row is not None:
            return row, False
        n = logic.round_nutrients(rec.nutrients)
        cur = conn.execute(
            "INSERT INTO foods (name, brand, gtin, source, source_id, verified, kcal, protein_g,"
            " carbs_g, fat_g, fiber_g, sugar_g, sodium_mg, serving_g, serving_desc)"
            " VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.name,
                rec.brand,
                rec.gtin,
                rec.source,
                rec.source_id,
                *(n[k] for k in logic.NUTRIENTS),
                rec.serving_g,
                rec.serving_desc,
            ),
        )
        return get_food(conn, cur.lastrowid), True


def save_label_food(
    conn: sqlite3.Connection,
    *,
    name: str,
    brand: str | None,
    gtin: str | None,
    per_100g: logic.Nutrients,
    serving_g: float,
    serving_desc: str | None,
) -> tuple[sqlite3.Row, int | None]:
    """Insert a food read from a package label (verified: it is the user's own label).

    If another food already holds the barcode, the barcode moves to the new food so future
    scans find the label values; the old food keeps its id for past entries. Returns the
    new row and the id of the food the barcode was taken from, if any.
    """
    n = logic.round_nutrients(per_100g)
    with transaction(conn):
        previous = None
        if gtin and (old := food_by_gtin(conn, gtin)) is not None:
            previous = old["id"]
            conn.execute("UPDATE foods SET gtin = NULL WHERE id = ?", (previous,))
        cur = conn.execute(
            "INSERT INTO foods (name, brand, gtin, source, verified, kcal, protein_g, carbs_g,"
            " fat_g, fiber_g, sugar_g, sodium_mg, serving_g, serving_desc)"
            " VALUES (?, ?, ?, 'label', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, brand, gtin, *(n[k] for k in logic.NUTRIENTS), serving_g, serving_desc),
        )
        return get_food(conn, cur.lastrowid), previous


def confirm_food(
    conn: sqlite3.Connection, food_id: int, corrections: logic.Nutrients
) -> tuple[sqlite3.Row, list[str]]:
    """Mark a food verified, applying per-100 g corrections for the keys given.

    Returns the updated row and the names of nutrients that changed. Past entries keep
    their snapshots.
    """
    current = nutrients_of(get_food(conn, food_id))
    merged = logic.round_nutrients({**current, **corrections})
    changed = [k for k in logic.NUTRIENTS if merged[k] != current[k]]
    sets = ", ".join(f"{k} = ?" for k in logic.NUTRIENTS)
    with transaction(conn):
        conn.execute(
            f"UPDATE foods SET verified = 1, updated_at = datetime('now'), {sets} WHERE id = ?",
            (*(merged[k] for k in logic.NUTRIENTS), food_id),
        )
    return get_food(conn, food_id), changed


_TOKEN = re.compile(r"\w+", re.UNICODE)


def tokens(query: str) -> list[str]:
    return _TOKEN.findall(query.lower())


def fts_query(query: str) -> str | None:
    """FTS5 MATCH expression: every word must match as a prefix of its singular and plural
    forms ('eggs' finds 'egg', 'berries' finds 'berry'). Callers filter out prefix-only hits
    such as 'eggplant' with text.match_score."""
    words = tokens(query)
    prefixes = (os.path.commonprefix([w, text.stem(w)]) for w in words)
    return " ".join(f'"{p}"*' for p in prefixes) if words else None


def search_aliases(conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    """Aliases matching the query, exact match first, then aliases containing every word."""
    words = tokens(query)
    if not words:
        return []
    where = " AND ".join("a.alias LIKE ?" for _ in words)
    return conn.execute(
        f"SELECT a.alias, a.default_grams, {', '.join('f.' + c for c in FOOD_COLS.split(', '))}"
        f" FROM aliases a JOIN foods f ON f.id = a.food_id WHERE {where}"
        " ORDER BY (a.alias = ? COLLATE NOCASE) DESC, length(a.alias) LIMIT ?",
        (*(f"%{w}%" for w in words), query.strip(), limit),
    ).fetchall()


# Foods the user has chosen: verified, added from a label or barcode, logged, aliased, or in
# a template. FDC search hits are cached in foods too, but only as a lookup cache.
USED_FOOD = (
    "(f.verified = 1 OR f.source != 'fdc'"
    " OR EXISTS (SELECT 1 FROM entries e WHERE e.food_id = f.id)"
    " OR EXISTS (SELECT 1 FROM aliases a WHERE a.food_id = f.id)"
    " OR EXISTS (SELECT 1 FROM meal_template_items t WHERE t.food_id = f.id))"
)


def search_foods(conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    """Foods the user has used that match every word of the query (singular or plural):
    verified first, then by relevance."""
    match = fts_query(query)
    if match is None:
        return []
    cols = ", ".join("f." + c for c in FOOD_COLS.split(", "))
    rows = conn.execute(
        f"SELECT {cols} FROM foods_fts JOIN foods f ON f.id = foods_fts.rowid"
        f" WHERE foods_fts MATCH ? AND {USED_FOOD}"
        " ORDER BY f.verified DESC, bm25(foods_fts) LIMIT ?",
        (match, limit * 5),
    ).fetchall()
    full = [r for r in rows if text.match_score(query, f"{r['name']} {r['brand'] or ''}") == 1]
    return full[:limit]


# --- Aliases and templates ------------------------------------------------------------------


def save_alias(
    conn: sqlite3.Connection, alias: str, food_id: int, default_grams: float | None
) -> None:
    get_food(conn, food_id)
    with transaction(conn):
        conn.execute(
            "INSERT INTO aliases (alias, food_id, default_grams) VALUES (?, ?, ?)"
            " ON CONFLICT(alias) DO UPDATE SET food_id = excluded.food_id,"
            " default_grams = excluded.default_grams",
            (alias, food_id, default_grams),
        )


def save_template(
    conn: sqlite3.Connection, name: str, items: list[tuple[int, float]]
) -> tuple[int, bool]:
    """Create or replace a meal template. Returns (template_id, replaced)."""
    get_foods(conn, (f for f, _ in items))
    with transaction(conn):
        row = conn.execute("SELECT id FROM meal_templates WHERE name = ?", (name,)).fetchone()
        if row is not None:
            template_id = row["id"]
            conn.execute("UPDATE meal_templates SET name = ? WHERE id = ?", (name, template_id))
            conn.execute("DELETE FROM meal_template_items WHERE template_id = ?", (template_id,))
        else:
            template_id = conn.execute(
                "INSERT INTO meal_templates (name) VALUES (?)", (name,)
            ).lastrowid
        conn.executemany(
            "INSERT INTO meal_template_items (template_id, food_id, grams) VALUES (?, ?, ?)",
            [(template_id, f, g) for f, g in items],
        )
    return template_id, row is not None


def get_template(conn: sqlite3.Connection, name: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    """A template by name (any case). If no name matches exactly, a single template whose
    name contains the phrase or is contained in it ('my usual breakfast') is used."""
    row = conn.execute("SELECT id, name FROM meal_templates WHERE name = ?", (name,)).fetchone()
    if row is None:
        wanted = name.strip().lower()
        close = [
            r
            for r in conn.execute("SELECT id, name FROM meal_templates")
            if r["name"].lower() in wanted or wanted in r["name"].lower()
        ]
        if len(close) == 1:
            row = close[0]
    if row is None:
        names = [r["name"] for r in conn.execute("SELECT name FROM meal_templates ORDER BY name")]
        known = ", ".join(repr(n) for n in names) if names else "none saved yet"
        raise NotFound(f"no meal template named {name!r} (templates: {known})")
    items = conn.execute(
        "SELECT food_id, grams FROM meal_template_items WHERE template_id = ? ORDER BY id",
        (row["id"],),
    ).fetchall()
    return row, items


# --- Meals and entries ----------------------------------------------------------------------


@dataclass
class EntryRow:
    id: int
    meal_id: int
    food_id: int
    name: str
    brand: str | None
    grams: float
    nutrients: logic.Nutrients


ENTRY_SELECT = (
    "SELECT e.id, e.meal_id, e.food_id, f.name, f.brand, e.grams, e.kcal, e.protein_g,"
    " e.carbs_g, e.fat_g, e.fiber_g, e.sugar_g, e.sodium_mg"
    " FROM entries e JOIN foods f ON f.id = e.food_id"
)


def _entry(row: sqlite3.Row) -> EntryRow:
    return EntryRow(
        id=row["id"],
        meal_id=row["meal_id"],
        food_id=row["food_id"],
        name=row["name"],
        brand=row["brand"],
        grams=row["grams"],
        nutrients=nutrients_of(row),
    )


def log_meal(
    conn: sqlite3.Connection,
    items: list[tuple[int, float]],
    eaten_at: str,
    label: str | None,
    note: str | None = None,
) -> int:
    """Create a meal with one entry per (food_id, grams), snapshotting nutrients."""
    if not items:
        raise ValueError("a meal needs at least one item")
    foods = get_foods(conn, (f for f, _ in items))
    snaps = [(f, g, logic.snapshot(nutrients_of(foods[f]), g)) for f, g in items]
    with transaction(conn):
        meal_id = conn.execute(
            "INSERT INTO meals (eaten_at, label, note) VALUES (?, ?, ?)", (eaten_at, label, note)
        ).lastrowid
        conn.executemany(
            "INSERT INTO entries (meal_id, food_id, grams, kcal, protein_g, carbs_g, fat_g,"
            " fiber_g, sugar_g, sodium_mg) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(meal_id, f, g, *(s[k] for k in logic.NUTRIENTS)) for f, g, s in snaps],
        )
    return meal_id


def get_meal(conn: sqlite3.Connection, meal_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, eaten_at, label, note FROM meals WHERE id = ?", (meal_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"no meal with id {meal_id}")
    return row


def meal_entries(conn: sqlite3.Connection, meal_id: int) -> list[EntryRow]:
    rows = conn.execute(f"{ENTRY_SELECT} WHERE e.meal_id = ? ORDER BY e.id", (meal_id,))
    return [_entry(r) for r in rows]


def get_entry(conn: sqlite3.Connection, entry_id: int) -> EntryRow:
    row = conn.execute(f"{ENTRY_SELECT} WHERE e.id = ?", (entry_id,)).fetchone()
    if row is None:
        raise NotFound(f"no entry with id {entry_id}")
    return _entry(row)


def update_entry(
    conn: sqlite3.Connection, entry_id: int, grams: float | None, food_id: int | None
) -> EntryRow:
    """Change grams and/or food, re-snapshotting from the food's current per-100 g values."""
    current = get_entry(conn, entry_id)
    new_food = food_id if food_id is not None else current.food_id
    new_grams = grams if grams is not None else current.grams
    snap = logic.snapshot(nutrients_of(get_food(conn, new_food)), new_grams)
    with transaction(conn):
        conn.execute(
            "UPDATE entries SET food_id = ?, grams = ?, kcal = ?, protein_g = ?, carbs_g = ?,"
            " fat_g = ?, fiber_g = ?, sugar_g = ?, sodium_mg = ? WHERE id = ?",
            (new_food, new_grams, *(snap[k] for k in logic.NUTRIENTS), entry_id),
        )
    return get_entry(conn, entry_id)


def delete_entry(conn: sqlite3.Connection, entry_id: int) -> tuple[EntryRow, bool]:
    """Delete an entry, and its meal if that was the last entry. Returns (entry, meal_deleted)."""
    entry = get_entry(conn, entry_id)
    with transaction(conn):
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        left = conn.execute(
            "SELECT count(*) FROM entries WHERE meal_id = ?", (entry.meal_id,)
        ).fetchone()[0]
        if left == 0:
            conn.execute("DELETE FROM meals WHERE id = ?", (entry.meal_id,))
    return entry, left == 0


def meals_between(conn: sqlite3.Connection, start_utc: str, end_utc: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, eaten_at, label, note FROM meals WHERE eaten_at >= ? AND eaten_at < ?"
        " ORDER BY eaten_at, id",
        (start_utc, end_utc),
    ).fetchall()


def entries_between(
    conn: sqlite3.Connection, start_utc: str, end_utc: str
) -> list[tuple[str, logic.Nutrients]]:
    """(meal eaten_at, entry nutrients) for every entry in [start, end)."""
    rows = conn.execute(
        "SELECT m.eaten_at, e.kcal, e.protein_g, e.carbs_g, e.fat_g, e.fiber_g, e.sugar_g,"
        " e.sodium_mg FROM entries e JOIN meals m ON m.id = e.meal_id"
        " WHERE m.eaten_at >= ? AND m.eaten_at < ?",
        (start_utc, end_utc),
    ).fetchall()
    return [(r["eaten_at"], nutrients_of(r)) for r in rows]


# --- Targets --------------------------------------------------------------------------------

TARGET_KEYS = ("kcal", "protein_g", "carbs_g", "fat_g")


def get_targets(conn: sqlite3.Connection) -> dict[str, float | None]:
    row = conn.execute(
        "SELECT kcal, protein_g, carbs_g, fat_g FROM targets WHERE id = 1"
    ).fetchone()
    return {k: (row[k] if row else None) for k in TARGET_KEYS}


def set_targets(conn: sqlite3.Connection, changes: Mapping[str, float | None]) -> dict:
    """Update the given targets (None clears one); others keep their value."""
    merged = {**get_targets(conn), **changes}
    with transaction(conn):
        conn.execute(
            "INSERT INTO targets (id, kcal, protein_g, carbs_g, fat_g) VALUES (1, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET kcal = excluded.kcal, protein_g = excluded.protein_g,"
            " carbs_g = excluded.carbs_g, fat_g = excluded.fat_g",
            tuple(merged[k] for k in TARGET_KEYS),
        )
    return get_targets(conn)
