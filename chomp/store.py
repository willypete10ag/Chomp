"""Database reads and writes behind the MCP tools. Every function takes an open connection.

Macro math is delegated to logic.py; this module only moves rows around.

Users (#46): foods are shared, but meals, entries, targets, portions, meal templates and undo
history belong to one user. Functions that touch those need a connection opened for a user
(db.connect(path, user_id)) and only ever see that user's rows; another user's meal or entry
is NotFound.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from chomp import logic, text
from chomp.db import transaction
from chomp.sources import FoodRecord

# Nutrient columns, the same on foods (per 100 g) and entries (snapshot)
NUTRIENT_COLS = ", ".join(logic.NUTRIENTS)
NUTRIENT_MARKS = ", ".join("?" * len(logic.NUTRIENTS))
FOOD_COLS = (
    f"id, name, brand, gtin, source, source_id, verified, {NUTRIENT_COLS}, serving_g, serving_desc"
)


class NotFound(LookupError):
    pass


def user_of(conn: sqlite3.Connection) -> int:
    """The user this connection was opened for."""
    user_id = getattr(conn, "user_id", None)
    if user_id is None:
        raise RuntimeError("this connection was not opened for a user")
    return user_id


def nutrients_of(row: Mapping) -> logic.Nutrients:
    return {k: row[k] for k in logic.NUTRIENTS}


# --- Foods ----------------------------------------------------------------------------------


def get_food(conn: sqlite3.Connection, food_id: int, *, live: bool = False) -> sqlite3.Row:
    """A food by id. Deleted foods are still found (past entries point at them) unless
    `live`, for picking a food to log, swap in, give a portion or put in a saved meal."""
    return get_foods(conn, [food_id], live=live)[food_id]


def get_foods(
    conn: sqlite3.Connection, ids: Iterable[int], *, live: bool = False
) -> dict[int, sqlite3.Row]:
    ids = sorted(set(ids))
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT {FOOD_COLS} FROM foods WHERE id IN ({marks})", ids).fetchall()
    found = {r["id"]: r for r in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise NotFound(f"no food with id {', '.join(map(str, missing))}")
    if live:
        gone = conn.execute(
            f"SELECT id FROM foods WHERE id IN ({marks}) AND deleted = 1", ids
        ).fetchall()
        if gone:
            raise NotFound(f"food {', '.join(str(r[0]) for r in gone)} was deleted; search again")
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
        n = logic.round_nutrients(rec.nutrients)
        if row is not None:
            same = (row["source"], row["source_id"]) == (rec.source, rec.source_id)
            if same and _is_deleted(conn, row["id"]):
                # deleted, then found upstream again: it's wanted after all, barcode too
                free = rec.gtin and food_by_gtin(conn, rec.gtin) is None
                conn.execute(
                    "UPDATE foods SET deleted = 0, gtin = ? WHERE id = ?",
                    (rec.gtin if free else row["gtin"], row["id"]),
                )
                row = get_food(conn, row["id"])
            if same:
                row = fill_serving(conn, _fill_micros(conn, row, n), rec)
            return row, False
        cur = conn.execute(
            f"INSERT INTO foods (name, brand, gtin, source, source_id, verified, {NUTRIENT_COLS},"
            f" serving_g, serving_desc) VALUES (?, ?, ?, ?, ?, 0, {NUTRIENT_MARKS}, ?, ?)",
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


def fill_serving(conn: sqlite3.Connection, row: sqlite3.Row, rec: FoodRecord) -> sqlite3.Row:
    """Give a food with no serving size the one its upstream record now has (USDA household
    measures for generic foods, after they were cached without one)."""
    if row["serving_g"] is not None or rec.serving_g is None:
        return row
    conn.execute(
        "UPDATE foods SET serving_g = ?, serving_desc = ? WHERE id = ? AND serving_g IS NULL",
        (rec.serving_g, rec.serving_desc, row["id"]),
    )
    return get_food(conn, row["id"])


def _fill_micros(conn: sqlite3.Connection, row: sqlite3.Row, n: logic.Nutrients) -> sqlite3.Row:
    """Fill in vitamins and minerals (#53) and label details (#63) a food cached before them
    lacks, from the same upstream record. Only empty columns of unconfirmed foods change;
    macros never do."""
    if row["verified"]:
        return row
    keys = logic.DETAILS + logic.MICROS
    missing = {k: n[k] for k in keys if row[k] is None and n.get(k) is not None}
    if not missing:
        return row
    sets = ", ".join(f"{k} = ?" for k in missing)
    conn.execute(f"UPDATE foods SET {sets} WHERE id = ?", (*missing.values(), row["id"]))
    resync_recipes_using(conn, row["id"])
    return get_food(conn, row["id"])


def _is_deleted(conn: sqlite3.Connection, food_id: int) -> bool:
    row = conn.execute("SELECT deleted FROM foods WHERE id = ?", (food_id,)).fetchone()
    return bool(row and row[0])


def delete_food(conn: sqlite3.Connection, food_id: int) -> tuple[sqlite3.Row, int]:
    """Delete a food for everyone (foods are shared). Logged entries keep their snapshots
    and still point at the row, so it stays, hidden from search and lists, with its barcode
    released for a fresh scan. Everyone's portions of it go, and it leaves every saved
    meal (a meal left empty goes too). Returns the food and how many saved meals it left."""
    food = get_food(conn, food_id)
    _not_a_recipe(conn, food_id)
    with transaction(conn):
        templates = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT template_id FROM meal_template_items WHERE food_id = ?",
                (food_id,),
            )
        ]
        conn.execute("DELETE FROM meal_template_items WHERE food_id = ?", (food_id,))
        conn.execute(
            "DELETE FROM meal_templates WHERE id NOT IN"
            " (SELECT template_id FROM meal_template_items)"
        )
        conn.execute("DELETE FROM portions WHERE food_id = ?", (food_id,))
        conn.execute(
            "UPDATE foods SET deleted = 1, gtin = NULL, saved = 0, updated_at = datetime('now')"
            " WHERE id = ?",
            (food_id,),
        )
    return food, len(templates)


def mark_saved(conn: sqlite3.Connection, food_id: int) -> sqlite3.Row:
    """Record that the user deliberately added this food (e.g. by scanning its barcode)."""
    conn.execute("UPDATE foods SET saved = 1 WHERE id = ?", (food_id,))
    return get_food(conn, food_id)


def save_label_food(
    conn: sqlite3.Connection,
    *,
    name: str,
    brand: str | None,
    gtin: str | None,
    per_100g: logic.Nutrients,
    serving_g: float,
    serving_desc: str | None,
    source: str = "label",
    verified: bool = True,
) -> tuple[sqlite3.Row, int | None]:
    """Insert a food read from a package label (verified: it is the user's own label), or,
    with source 'manual', one typed in by hand. The dashboard (#59) saves label foods
    unverified unless the person ticked every value, and typed-in foods always unverified.

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
            f"INSERT INTO foods (name, brand, gtin, source, verified, {NUTRIENT_COLS}, serving_g,"
            f" serving_desc) VALUES (?, ?, ?, ?, ?, {NUTRIENT_MARKS}, ?, ?)",
            (
                name,
                brand,
                gtin,
                source,
                int(verified),
                *(n[k] for k in logic.NUTRIENTS),
                serving_g,
                serving_desc,
            ),
        )
        return get_food(conn, cur.lastrowid), previous


FOOD_DETAILS = ("name", "brand", "serving_g", "serving_desc")


def confirm_food(
    conn: sqlite3.Connection,
    food_id: int,
    corrections: logic.Nutrients,
    details: Mapping[str, str | float | None] | None = None,
    *,
    confirmed: bool = True,
) -> tuple[sqlite3.Row, list[str]]:
    """Mark a food verified (or, with confirmed=False, back to unconfirmed), applying
    per-100 g corrections for the keys given, and any `details` (name, brand, serving_g,
    serving_desc) given.

    Returns the updated row and the names of nutrients that changed. Past entries keep
    their snapshots.
    """
    details = dict(details or {})
    _not_a_recipe(conn, food_id)
    if unknown := set(details) - set(FOOD_DETAILS):
        raise ValueError(f"unknown food details: {sorted(unknown)}")
    current = nutrients_of(get_food(conn, food_id))
    merged = logic.round_nutrients({**current, **corrections})
    changed = [k for k in logic.NUTRIENTS if merged[k] != current[k]]
    sets = ", ".join(f"{k} = ?" for k in (*logic.NUTRIENTS, *details))
    with transaction(conn):
        conn.execute(
            f"UPDATE foods SET verified = ?, updated_at = datetime('now'), {sets} WHERE id = ?",
            (int(confirmed), *(merged[k] for k in logic.NUTRIENTS), *details.values(), food_id),
        )
        if changed:
            resync_recipes_using(conn, food_id)
    return get_food(conn, food_id), changed


def _not_a_recipe(conn: sqlite3.Connection, food_id: int) -> None:
    """A recipe's food row follows its ingredients; it is changed through the recipe."""
    row = conn.execute("SELECT name FROM recipes WHERE food_id = ?", (food_id,)).fetchone()
    if row is not None:
        raise ValueError(f"food {food_id} is the recipe {row['name']!r}; edit the recipe instead")


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


def search_portions(conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    """The user's foods with a portion whose name contains every word of the query ("birria
    taco"), exact name first. Each row is the food plus its matched `portion`, `portion_grams`
    and `portion_estimated`."""
    words = tokens(query)
    if not words:
        return []
    where = " AND ".join("p.name LIKE ?" for _ in words)
    return conn.execute(
        "SELECT p.name AS portion, p.grams AS portion_grams, p.estimated AS portion_estimated,"
        f" {', '.join('f.' + c for c in FOOD_COLS.split(', '))}"
        " FROM portions p JOIN foods f ON f.id = p.food_id"
        f" WHERE p.user_id = ? AND f.deleted = 0 AND {where}"
        " ORDER BY (p.name = ? COLLATE NOCASE) DESC, length(p.name) LIMIT ?",
        (user_of(conn), *(f"%{w}%" for w in words), query.strip(), limit),
    ).fetchall()


# Foods someone has chosen: verified, added from a label or barcode, logged, with a portion, or in
# a template. FDC and Open Food Facts search hits (#88) are cached in foods too, but only as a
# lookup cache. Foods are shared, so one person's saved foods are found by everyone.
USED_FOOD = (
    "f.deleted = 0 AND (f.verified = 1 OR f.saved = 1 OR f.source NOT IN ('fdc', 'off')"
    " OR EXISTS (SELECT 1 FROM entries e WHERE e.food_id = f.id)"
    " OR EXISTS (SELECT 1 FROM portions p WHERE p.food_id = f.id)"
    " OR EXISTS (SELECT 1 FROM meal_template_items t WHERE t.food_id = f.id))"
)


def used_food_ids(conn: sqlite3.Connection, food_ids: Iterable[int]) -> set[int]:
    """Which of these foods are in Foods (USED_FOOD), not just a cached search hit (#97)."""
    ids = list(dict.fromkeys(food_ids))
    if not ids:
        return set()
    marks = ", ".join("?" * len(ids))
    return {
        r[0]
        for r in conn.execute(
            f"SELECT f.id FROM foods f WHERE f.id IN ({marks}) AND {USED_FOOD}", ids
        )
    }


# How many of this user's entries log the food, as column `uses` (one ? for the user id)
USES = (
    "(SELECT count(*) FROM entries e JOIN meals m ON m.id = e.meal_id"
    " WHERE e.food_id = f.id AND m.user_id = ?) AS uses"
)
# When this user last logged the food (UTC), as column `last_used` (one ? for the user id)
LAST_USED = (
    "(SELECT max(m.eaten_at) FROM entries e JOIN meals m ON m.id = e.meal_id"
    " WHERE e.food_id = f.id AND m.user_id = ?) AS last_used"
)
MOST_USED = "uses DESC, f.verified DESC, f.name COLLATE NOCASE, f.id"
# Ways to order the Foods list and its search (#91): sort -> the column it sorts by. Each
# sorts high to low (most, latest, unconfirmed first) unless asked for low to high; foods with
# no value go last either way, and ties fall back to MOST_USED.
FOOD_SORTS = {
    "most": "uses",
    "recent": "last_used",
    "new": "f.id",  # ids only grow, and same-second foods still order
    "unconfirmed": "(f.verified = 0)",
    "kcal": "f.kcal",
    "protein": "f.protein_g",
    "fat": "f.fat_g",
    "carbs": "f.carbs_g",
}
MACRO_SORTS = ("kcal", "protein", "fat", "carbs")


def food_order(sort: str, desc: bool = True, per_serving: bool = False) -> str:
    """ORDER BY for the Foods list. Macro sorts compare the values shown: per serving when
    `per_serving` (per 100 g for a food with no serving size), else per 100 g."""
    col = FOOD_SORTS.get(sort, "uses")
    if per_serving and sort in MACRO_SORTS:
        col = f"({col} * coalesce(f.serving_g, 100) / 100)"
    return f"{col} IS NULL, {col} {'DESC' if desc else 'ASC'}, {MOST_USED}"


def search_foods(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    sort: str | None = None,
    desc: bool = True,
    per_serving: bool = False,
    typing: bool = False,
) -> list[sqlite3.Row]:
    """Foods the user has used that match every word of the query (singular or plural):
    verified first, then by relevance; a `sort` (FOOD_SORTS, see food_order, as the Foods
    page asks) orders them by it instead. With `typing` (Foods' search as you type, #97)
    the last word may be half typed: "chob" finds Chobani, after the whole-word matches."""
    match = fts_query(query)
    if match is None:
        return []
    order = food_order(sort, desc, per_serving) if sort else None
    cols = ", ".join("f." + c for c in FOOD_COLS.split(", "))
    user = user_of(conn)
    rows = conn.execute(
        f"SELECT {cols}, {USES}, {LAST_USED} FROM foods_fts JOIN foods f"
        f" ON f.id = foods_fts.rowid WHERE foods_fts MATCH ? AND {USED_FOOD}"
        f" ORDER BY {order or 'f.verified DESC, bm25(foods_fts)'}",
        (user, user, match),
    )
    # Prefix-only hits ('eggplant' for 'egg') are dropped here, so read until enough remain
    found: list[sqlite3.Row] = []
    begun: list[sqlite3.Row] = []  # typing: the last word only begins a word ('chob')
    for row in rows:
        name = f"{row['name']} {row['brand'] or ''}"
        if text.match_score(query, name) == 1:
            found.append(row)
            if len(found) == limit:
                break
        elif typing and text.typed_so_far(query, name):
            begun.append(row)
    rows.close()
    return (found + begun)[:limit]


def list_used_foods(
    conn: sqlite3.Connection,
    limit: int,
    sort: str = "most",
    desc: bool = True,
    per_serving: bool = False,
) -> list[sqlite3.Row]:
    """Foods the user has used (see USED_FOOD), most logged first, then verified, then by
    name; another sort (FOOD_SORTS, see food_order) orders them by it instead."""
    cols = ", ".join("f." + c for c in FOOD_COLS.split(", "))
    user = user_of(conn)
    return conn.execute(
        f"SELECT {cols}, {USES}, {LAST_USED} FROM foods f WHERE {USED_FOOD}"
        f" ORDER BY {food_order(sort, desc, per_serving)} LIMIT ?",
        (user, user, limit),
    ).fetchall()


def food_usage(conn: sqlite3.Connection, food_id: int) -> tuple[int, str | None]:
    """How many of this user's entries log the food, and when they last logged it (UTC)."""
    user = user_of(conn)
    row = conn.execute(
        f"SELECT {USES}, {LAST_USED} FROM foods f WHERE f.id = ?", (user, user, food_id)
    ).fetchone()
    return (row["uses"], row["last_used"]) if row else (0, None)


def set_food_confirmed(conn: sqlite3.Connection, food_id: int, confirmed: bool) -> sqlite3.Row:
    """Mark a food confirmed (the person vouches for its numbers) or back to unconfirmed,
    leaving its values alone."""
    get_food(conn, food_id, live=True)
    with transaction(conn):
        conn.execute(
            "UPDATE foods SET verified = ?, updated_at = datetime('now') WHERE id = ?",
            (int(confirmed), food_id),
        )
    return get_food(conn, food_id)


def rename_food(conn: sqlite3.Connection, food_id: int, name: str) -> sqlite3.Row:
    """Give a food a shorter display name. Search finds it by the new name from then on."""
    name = name.strip()
    if not name:
        raise ValueError("a food needs a name")
    get_food(conn, food_id)
    _not_a_recipe(conn, food_id)
    with transaction(conn):
        conn.execute(
            "UPDATE foods SET name = ?, updated_at = datetime('now') WHERE id = ?", (name, food_id)
        )
    return get_food(conn, food_id)


# --- Portions (#75) ------------------------------------------------------------------------


def portion_name(name: str) -> str:
    """A portion's name as stored: single-spaced, without a leading count ("1 handful")."""
    name = " ".join(name.split())
    name = re.sub(r"^(1|one|a|an)\s+(?=\S)", "", name, flags=re.IGNORECASE)
    if not name:
        raise ValueError("a portion needs a name, e.g. 'handful' or 'slice'")
    return name


def portions_of(conn: sqlite3.Connection, food_ids: Iterable[int]) -> dict[int, list[sqlite3.Row]]:
    """This user's portions of each food, by name."""
    ids = sorted(set(food_ids))
    out: dict[int, list[sqlite3.Row]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = conn.execute(
        "SELECT id, food_id, name, grams, estimated FROM portions"
        f" WHERE user_id = ? AND food_id IN ({','.join('?' * len(ids))})"
        " ORDER BY name COLLATE NOCASE",
        (user_of(conn), *ids),
    )
    for r in rows:
        out[r["food_id"]].append(r)
    return out


def get_portion(conn: sqlite3.Connection, food_id: int, name: str) -> sqlite3.Row:
    """This user's portion of a food by name (any case, a leading "1 " ignored)."""
    row = conn.execute(
        "SELECT id, food_id, name, grams, estimated FROM portions"
        " WHERE user_id = ? AND food_id = ? AND name = ?",
        (user_of(conn), food_id, portion_name(name)),
    ).fetchone()
    if row is None:
        known = ", ".join(repr(p["name"]) for p in portions_of(conn, [food_id])[food_id])
        raise NotFound(
            f"no portion named {name!r} for food {food_id} (portions: {known or 'none yet'})"
        )
    return row


def save_portion(
    conn: sqlite3.Connection, food_id: int, name: str, grams: float, estimated: bool = False
) -> tuple[sqlite3.Row, bool]:
    """Create or replace this user's portion of a food. Returns (portion, replaced)."""
    if not grams > 0:
        raise ValueError("a portion weighs more than 0 g")
    name = portion_name(name)
    get_food(conn, food_id, live=True)
    user_id = user_of(conn)
    with transaction(conn):
        existed = conn.execute(
            "SELECT 1 FROM portions WHERE user_id = ? AND food_id = ? AND name = ?",
            (user_id, food_id, name),
        ).fetchone()
        conn.execute(
            "INSERT INTO portions (user_id, food_id, name, grams, estimated) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, food_id, name) DO UPDATE SET name = excluded.name,"
            " grams = excluded.grams, estimated = excluded.estimated,"
            " updated_at = datetime('now')",
            (user_id, food_id, name, grams, int(estimated)),
        )
    return get_portion(conn, food_id, name), existed is not None


def delete_portion(conn: sqlite3.Connection, food_id: int, name: str) -> sqlite3.Row:
    """Forget one of this user's portions. Returns it as it was."""
    row = get_portion(conn, food_id, name)
    with transaction(conn):
        conn.execute("DELETE FROM portions WHERE id = ?", (row["id"],))
    return row


# --- Templates ------------------------------------------------------------------------------


def save_template(conn: sqlite3.Connection, name: str, items: list[Item]) -> tuple[int, bool]:
    """Create or replace a meal template. Returns (template_id, replaced)."""
    get_foods(conn, (i[0] for i in items), live=True)
    user_id = user_of(conn)
    with transaction(conn):
        row = conn.execute(
            "SELECT id FROM meal_templates WHERE user_id = ? AND name = ?", (user_id, name)
        ).fetchone()
        if row is not None:
            template_id = row["id"]
            conn.execute("UPDATE meal_templates SET name = ? WHERE id = ?", (name, template_id))
            conn.execute("DELETE FROM meal_template_items WHERE template_id = ?", (template_id,))
        else:
            template_id = conn.execute(
                "INSERT INTO meal_templates (user_id, name) VALUES (?, ?)", (user_id, name)
            ).lastrowid
        conn.executemany(
            "INSERT INTO meal_template_items (template_id, food_id, grams, estimated)"
            " VALUES (?, ?, ?, ?)",
            [(template_id, f, g, int(any(est))) for f, g, *est in items],
        )
    return template_id, row is not None


def get_template_by_id(
    conn: sqlite3.Connection, template_id: int
) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    row = conn.execute(
        "SELECT id, name FROM meal_templates WHERE id = ? AND user_id = ?",
        (template_id, user_of(conn)),
    ).fetchone()
    if row is None:
        raise NotFound(f"no meal template with id {template_id}")
    items = conn.execute(
        "SELECT food_id, grams, estimated FROM meal_template_items WHERE template_id = ?"
        " ORDER BY id",
        (template_id,),
    ).fetchall()
    return row, items


def get_template(conn: sqlite3.Connection, name: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    """A template by name (any case). If no name matches exactly, a single template whose
    name contains the phrase or is contained in it ('my usual breakfast') is used."""
    user_id = user_of(conn)
    row = conn.execute(
        "SELECT id, name FROM meal_templates WHERE user_id = ? AND name = ?", (user_id, name)
    ).fetchone()
    if row is None:
        wanted = name.strip().lower()
        close = [
            r
            for r in _templates(conn)
            if r["name"].lower() in wanted or wanted in r["name"].lower()
        ]
        if len(close) == 1:
            row = close[0]
    if row is None:
        names = [r["name"] for r in _templates(conn)]
        known = ", ".join(repr(n) for n in names) if names else "none saved yet"
        raise NotFound(f"no meal template named {name!r} (templates: {known})")
    items = conn.execute(
        "SELECT food_id, grams, estimated FROM meal_template_items WHERE template_id = ?"
        " ORDER BY id",
        (row["id"],),
    ).fetchall()
    return row, items


def _templates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """This user's meal templates (id, name) by name."""
    return conn.execute(
        "SELECT id, name FROM meal_templates WHERE user_id = ? ORDER BY name", (user_of(conn),)
    ).fetchall()


def templates_matching(conn: sqlite3.Connection, query: str) -> list[str]:
    """Names of meal templates the phrase names ("protein shake", "my usual breakfast")."""
    return [
        r["name"]
        for r in _templates(conn)
        if text.match_score(query, r["name"]) == 1 or text.match_score(r["name"], query) == 1
    ]


def _template_foods(conn: sqlite3.Connection, template_id: int) -> list[sqlite3.Row]:
    """A template's items in order: grams, estimated and the food's columns."""
    cols = ", ".join("f." + c for c in FOOD_COLS.split(", "))
    return conn.execute(
        f"SELECT i.grams, i.estimated, {cols} FROM meal_template_items i"
        " JOIN foods f ON f.id = i.food_id WHERE i.template_id = ? ORDER BY i.id",
        (template_id,),
    ).fetchall()


def list_templates(conn: sqlite3.Connection) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
    """Every meal template with its items (food name, grams and per-100 g nutrients)."""
    return [(t, _template_foods(conn, t["id"])) for t in _templates(conn)]


def template_with_foods(
    conn: sqlite3.Connection, template_id: int
) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    """One of this user's templates with its items as list_templates gives them."""
    row, _ = get_template_by_id(conn, template_id)
    return row, _template_foods(conn, template_id)


def edit_template(
    conn: sqlite3.Connection, template_id: int | None, name: str, items: list[Item]
) -> int:
    """Save a template from the dashboard editor (#60): a new one when template_id is None,
    else that one renamed and its items replaced. Unlike save_template a name is never
    reused: another template with the same name (any case) is refused with ValueError."""
    get_foods(conn, (i[0] for i in items), live=True)
    if template_id is not None:
        get_template_by_id(conn, template_id)
    user_id = user_of(conn)
    with transaction(conn):
        clash = conn.execute(
            "SELECT 1 FROM meal_templates WHERE user_id = ? AND name = ? COLLATE NOCASE"
            " AND id IS NOT ?",
            (user_id, name, template_id),
        ).fetchone()
        if clash is not None:
            raise ValueError(f"you already have a saved meal named {name!r}")
        if template_id is None:
            template_id = conn.execute(
                "INSERT INTO meal_templates (user_id, name) VALUES (?, ?)", (user_id, name)
            ).lastrowid
        else:
            conn.execute("UPDATE meal_templates SET name = ? WHERE id = ?", (name, template_id))
            conn.execute("DELETE FROM meal_template_items WHERE template_id = ?", (template_id,))
        conn.executemany(
            "INSERT INTO meal_template_items (template_id, food_id, grams, estimated)"
            " VALUES (?, ?, ?, ?)",
            [(template_id, f, g, int(any(est))) for f, g, *est in items],
        )
    return template_id


def delete_template(conn: sqlite3.Connection, template_id: int) -> sqlite3.Row:
    """Delete one of this user's templates (its items go with it). Returns its id and name."""
    row, _ = get_template_by_id(conn, template_id)
    with transaction(conn):
        conn.execute("DELETE FROM meal_template_items WHERE template_id = ?", (template_id,))
        conn.execute("DELETE FROM meal_templates WHERE id = ?", (template_id,))
    return row


# --- Recipes (#82) -------------------------------------------------------------------------

RECIPE_BRAND = "Recipe"


@dataclass
class Recipe:
    id: int
    name: str
    servings: float
    steps: list[str]
    food_id: int | None
    # food_id, name, brand, grams and the food's per-100 g nutrients, in recipe order
    ingredients: list[sqlite3.Row]


def save_recipe(
    conn: sqlite3.Connection,
    name: str,
    servings: float,
    ingredients: list[tuple[int, float]],
    steps: str | list[str] | None = None,
    *,
    recipe_id: int | None = None,
) -> tuple[Recipe, bool]:
    """Create a recipe, or replace one: the recipe `recipe_id` (which may rename it), else
    this user's recipe with the same name. Ingredients are (food_id, grams). Steps are a list
    or text with one step per line. Returns the recipe and whether one was replaced.

    The recipe's food row (what logging it uses) is created or updated to match."""
    name = name.strip()
    if not name:
        raise ValueError("a recipe needs a name")
    if not ingredients:
        raise ValueError("a recipe needs at least one ingredient")
    foods = get_foods(conn, (f for f, _ in ingredients))
    logic.recipe_nutrition(
        [(nutrients_of(foods[f]), g) for f, g in ingredients], servings
    )  # validates grams and servings before anything is written
    user_id = user_of(conn)
    lines = steps if isinstance(steps, list) else (steps or "").splitlines()
    text_steps = "\n".join(s.strip() for s in lines if s.strip()) or None
    with transaction(conn):
        if recipe_id is not None:
            row = _recipe_row(conn, recipe_id)
        else:
            row = conn.execute(
                "SELECT * FROM recipes WHERE user_id = ? AND name = ?", (user_id, name)
            ).fetchone()
        clash = conn.execute(
            "SELECT id FROM recipes WHERE user_id = ? AND name = ? AND id != ?",
            (user_id, name, row["id"] if row else 0),
        ).fetchone()
        if clash:
            raise ValueError(f"you already have a recipe named {name!r}")
        if row is not None and any(f == row["food_id"] for f, _ in ingredients):
            raise ValueError("a recipe can't be an ingredient of itself")
        if row is None:
            rid = conn.execute(
                "INSERT INTO recipes (user_id, name, servings, steps) VALUES (?, ?, ?, ?)",
                (user_id, name, servings, text_steps),
            ).lastrowid
        else:
            rid = row["id"]
            conn.execute(
                "UPDATE recipes SET name = ?, servings = ?, steps = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (name, servings, text_steps, rid),
            )
            conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (rid,))
        conn.executemany(
            "INSERT INTO recipe_ingredients (recipe_id, food_id, grams) VALUES (?, ?, ?)",
            [(rid, f, g) for f, g in ingredients],
        )
        _sync_recipe_food(conn, rid)
    return get_recipe_by_id(conn, rid), row is not None


def _recipe_row(conn: sqlite3.Connection, recipe_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM recipes WHERE id = ? AND user_id = ?", (recipe_id, user_of(conn))
    ).fetchone()
    if row is None:
        raise NotFound(f"no recipe with id {recipe_id}")
    return row


def _sync_recipe_food(conn: sqlite3.Connection, recipe_id: int) -> None:
    """Write a recipe's summed nutrition to its food row (one serving as the serving size),
    creating the row the first time. Any user's recipe: foods are shared, so a change to an
    ingredient updates every recipe that uses it. Past entries keep their snapshots."""
    r = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    items = conn.execute(
        f"SELECT i.grams, {', '.join('f.' + k for k in logic.NUTRIENTS)}"
        " FROM recipe_ingredients i JOIN foods f ON f.id = i.food_id"
        " WHERE i.recipe_id = ? ORDER BY i.id",
        (recipe_id,),
    ).fetchall()
    n = logic.recipe_nutrition([(nutrients_of(i), i["grams"]) for i in items], r["servings"])
    desc = f"1 serving (recipe makes {r['servings']:g})"
    values = (
        r["name"],
        *(n["per_100g"][k] for k in logic.NUTRIENTS),
        round(n["serving_g"], 1),
        desc,
    )
    sets = ", ".join(f"{k} = ?" for k in logic.NUTRIENTS)
    if r["food_id"] is not None:
        conn.execute(
            f"UPDATE foods SET name = ?, {sets}, serving_g = ?, serving_desc = ?, deleted = 0,"
            " verified = 1, updated_at = datetime('now') WHERE id = ?",
            (*values, r["food_id"]),
        )
        return
    food_id = conn.execute(
        f"INSERT INTO foods (name, {NUTRIENT_COLS}, serving_g, serving_desc, brand, source,"
        f" verified, saved) VALUES (?, {NUTRIENT_MARKS}, ?, ?, ?, 'manual', 1, 1)",
        (*values, RECIPE_BRAND),
    ).lastrowid
    conn.execute("UPDATE recipes SET food_id = ? WHERE id = ?", (food_id, recipe_id))


def resync_recipes_using(conn: sqlite3.Connection, food_id: int) -> None:
    """Recompute every recipe with this food as an ingredient (its values changed)."""
    with transaction(conn):
        for (rid,) in conn.execute(
            "SELECT DISTINCT recipe_id FROM recipe_ingredients WHERE food_id = ?", (food_id,)
        ).fetchall():
            _sync_recipe_food(conn, rid)


def _recipe(conn: sqlite3.Connection, row: sqlite3.Row) -> Recipe:
    cols = ", ".join("f." + c for c in FOOD_COLS.split(", "))
    items = conn.execute(
        f"SELECT i.grams, {cols} FROM recipe_ingredients i JOIN foods f ON f.id = i.food_id"
        " WHERE i.recipe_id = ? ORDER BY i.id",
        (row["id"],),
    ).fetchall()
    return Recipe(
        id=row["id"],
        name=row["name"],
        servings=row["servings"],
        steps=(row["steps"] or "").splitlines(),
        food_id=row["food_id"],
        ingredients=items,
    )


def get_recipe_by_id(conn: sqlite3.Connection, recipe_id: int) -> Recipe:
    return _recipe(conn, _recipe_row(conn, recipe_id))


def get_recipe(conn: sqlite3.Connection, name: str) -> Recipe:
    """A recipe by name (any case). If no name matches exactly, a single recipe whose name
    contains the phrase or is contained in it ('my chili') is used."""
    rows = conn.execute(
        "SELECT * FROM recipes WHERE user_id = ? ORDER BY name", (user_of(conn),)
    ).fetchall()
    wanted = name.strip().lower()
    row = next((r for r in rows if r["name"].lower() == wanted), None)
    if row is None:
        close = [r for r in rows if r["name"].lower() in wanted or wanted in r["name"].lower()]
        if len(close) == 1:
            row = close[0]
    if row is None:
        known = ", ".join(repr(r["name"]) for r in rows) if rows else "none saved yet"
        raise NotFound(f"no recipe named {name!r} (recipes: {known})")
    return _recipe(conn, row)


def list_recipes(conn: sqlite3.Connection) -> list[Recipe]:
    """This user's recipes by name, with ingredients."""
    rows = conn.execute(
        "SELECT * FROM recipes WHERE user_id = ? ORDER BY name", (user_of(conn),)
    ).fetchall()
    return [_recipe(conn, r) for r in rows]


def recipe_food_ids(conn: sqlite3.Connection) -> set[int]:
    """Food rows that stand for a recipe (anyone's): the Foods list leaves them out."""
    return {r[0] for r in conn.execute("SELECT food_id FROM recipes WHERE food_id IS NOT NULL")}


def recipe_of_food(conn: sqlite3.Connection, food_id: int) -> sqlite3.Row | None:
    """The recipe (id, user_id, name) a food row stands for, if any."""
    return conn.execute(
        "SELECT id, user_id, name FROM recipes WHERE food_id = ?", (food_id,)
    ).fetchone()


def delete_recipe(conn: sqlite3.Connection, recipe_id: int) -> Recipe:
    """Delete one of this user's recipes. Its food row is deleted like any food (entries
    that logged it keep their snapshots). Returns the recipe as it was."""
    recipe = get_recipe_by_id(conn, recipe_id)
    with transaction(conn):
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
        if recipe.food_id is not None:
            delete_food(conn, recipe.food_id)
    return recipe


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
    estimated: bool = False  # grams estimated from a count, not weighed (#75)


# Callers add "AND ..." conditions and pass the user id first
ENTRY_NUTRIENTS = ", ".join(f"e.{k}" for k in logic.NUTRIENTS)
ENTRY_SELECT = (
    f"SELECT e.id, e.meal_id, e.food_id, f.name, f.brand, e.grams, e.estimated, {ENTRY_NUTRIENTS}"
    " FROM entries e JOIN foods f ON f.id = e.food_id JOIN meals m ON m.id = e.meal_id"
    " WHERE m.user_id = ?"
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
        estimated=bool(row["estimated"]),
    )


# (food_id, grams) or (food_id, grams, estimated)
Item = tuple[int, float] | tuple[int, float, bool]


def log_meal(
    conn: sqlite3.Connection,
    items: list[Item],
    eaten_at: str,
    label: str | None,
    note: str | None = None,
) -> int:
    """Create a meal with one entry per item, snapshotting nutrients."""
    with transaction(conn):
        meal_id = conn.execute(
            "INSERT INTO meals (user_id, eaten_at, label, note) VALUES (?, ?, ?, ?)",
            (user_of(conn), eaten_at, label, note),
        ).lastrowid
        _insert_entries(conn, meal_id, items)
    return meal_id


def add_to_meal(conn: sqlite3.Connection, meal_id: int, items: list[Item]) -> None:
    """Add entries to an existing meal, snapshotting nutrients."""
    get_meal(conn, meal_id)
    with transaction(conn):
        _insert_entries(conn, meal_id, items)


def _insert_entries(conn: sqlite3.Connection, meal_id: int, items: list[Item]) -> None:
    if not items:
        raise ValueError("a meal needs at least one item")
    foods = get_foods(conn, (i[0] for i in items), live=True)
    rows = []
    for f, g, *estimated in items:
        snap = logic.snapshot(nutrients_of(foods[f]), g)
        rows.append((meal_id, f, g, int(any(estimated)), *(snap[k] for k in logic.NUTRIENTS)))
    conn.executemany(
        f"INSERT INTO entries (meal_id, food_id, grams, estimated, {NUTRIENT_COLS})"
        f" VALUES (?, ?, ?, ?, {NUTRIENT_MARKS})",
        rows,
    )


def get_meal(conn: sqlite3.Connection, meal_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, eaten_at, label, note FROM meals WHERE id = ? AND user_id = ?",
        (meal_id, user_of(conn)),
    ).fetchone()
    if row is None:
        raise NotFound(f"no meal with id {meal_id}")
    return row


def meal_entries(conn: sqlite3.Connection, meal_id: int) -> list[EntryRow]:
    rows = conn.execute(f"{ENTRY_SELECT} AND e.meal_id = ? ORDER BY e.id", (user_of(conn), meal_id))
    return [_entry(r) for r in rows]


def get_entry(conn: sqlite3.Connection, entry_id: int) -> EntryRow:
    row = conn.execute(f"{ENTRY_SELECT} AND e.id = ?", (user_of(conn), entry_id)).fetchone()
    if row is None:
        raise NotFound(f"no entry with id {entry_id}")
    return _entry(row)


SNAPSHOT_SETS = ", ".join(f"{k} = ?" for k in logic.NUTRIENTS)


def update_entry(
    conn: sqlite3.Connection,
    entry_id: int,
    grams: float | None,
    food_id: int | None,
    estimated: bool = False,
) -> EntryRow:
    """Change grams and/or food, re-snapshotting from the food's current per-100 g values.
    New grams are taken as exact unless `estimated`; a food swap alone keeps the flag."""
    current = get_entry(conn, entry_id)
    new_food = food_id if food_id is not None else current.food_id
    new_grams = grams if grams is not None else current.grams
    new_estimated = estimated if grams is not None else current.estimated
    swapped = food_id is not None and food_id != current.food_id
    snap = logic.snapshot(nutrients_of(get_food(conn, new_food, live=swapped)), new_grams)
    with transaction(conn):
        conn.execute(
            f"UPDATE entries SET food_id = ?, grams = ?, estimated = ?, {SNAPSHOT_SETS}"
            " WHERE id = ?",
            (
                new_food,
                new_grams,
                int(new_estimated),
                *(snap[k] for k in logic.NUTRIENTS),
                entry_id,
            ),
        )
    return get_entry(conn, entry_id)


def update_meal(
    conn: sqlite3.Connection,
    meal_id: int,
    eaten_at: str | None,
    label: str | None,
    note: str | None,
) -> sqlite3.Row:
    """Change a meal's time, label and/or note. An empty label or note clears it.

    Only the given fields are written, so overlapping updates to different fields don't undo
    each other.
    """
    changes: dict[str, str | None] = {}
    if eaten_at is not None:
        changes["eaten_at"] = eaten_at
    if label is not None:
        changes["label"] = label.strip() or None
    if note is not None:
        changes["note"] = note.strip() or None
    get_meal(conn, meal_id)
    with transaction(conn):
        if changes:
            conn.execute(
                f"UPDATE meals SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                (*changes.values(), meal_id),
            )
    return get_meal(conn, meal_id)


def delete_entry(conn: sqlite3.Connection, entry_id: int) -> tuple[EntryRow, bool]:
    """Delete an entry, and its meal if that was the last entry. Returns (entry, meal_deleted)."""
    entry = get_entry(conn, entry_id)
    with transaction(conn):
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        gone = _drop_if_empty(conn, entry.meal_id)
    return entry, gone


def move_entry(
    conn: sqlite3.Connection, entry_id: int, meal_id: int | None, eaten_at: str | None = None
) -> tuple[int, bool]:
    """Move an entry to another meal, or (meal_id None) to a new meal at eaten_at. Its old
    meal is deleted if this was its last entry. Returns (new meal_id, old meal deleted)."""
    entry = get_entry(conn, entry_id)
    if meal_id is not None:
        get_meal(conn, meal_id)
    elif eaten_at is None:
        raise ValueError("a new meal needs a time")
    with transaction(conn):
        if meal_id is None:
            meal_id = conn.execute(
                "INSERT INTO meals (user_id, eaten_at) VALUES (?, ?)", (user_of(conn), eaten_at)
            ).lastrowid
        conn.execute("UPDATE entries SET meal_id = ? WHERE id = ?", (meal_id, entry_id))
        gone = _drop_if_empty(conn, entry.meal_id)
    return meal_id, gone


def merge_meals(conn: sqlite3.Connection, source_id: int, target_id: int) -> None:
    """Move every entry of one meal into another and delete the emptied meal. The target
    keeps its own time, label and note."""
    if source_id == target_id:
        raise ValueError("a meal can't be combined with itself")
    get_meal(conn, source_id)
    get_meal(conn, target_id)
    with transaction(conn):
        conn.execute("UPDATE entries SET meal_id = ? WHERE meal_id = ?", (target_id, source_id))
        conn.execute("DELETE FROM meals WHERE id = ?", (source_id,))


def delete_meal(conn: sqlite3.Connection, meal_id: int) -> None:
    """Delete a meal and all its entries."""
    get_meal(conn, meal_id)
    with transaction(conn):
        conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))


def _drop_if_empty(conn: sqlite3.Connection, meal_id: int) -> bool:
    left = conn.execute("SELECT count(*) FROM entries WHERE meal_id = ?", (meal_id,)).fetchone()[0]
    if left == 0:
        conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
    return left == 0


def meals_between(conn: sqlite3.Connection, start_utc: str, end_utc: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, eaten_at, label, note FROM meals"
        " WHERE user_id = ? AND eaten_at >= ? AND eaten_at < ? ORDER BY eaten_at, id",
        (user_of(conn), start_utc, end_utc),
    ).fetchall()


def entries_between(
    conn: sqlite3.Connection, start_utc: str, end_utc: str
) -> list[tuple[str, logic.Nutrients]]:
    """(meal eaten_at, entry nutrients) for every entry in [start, end)."""
    rows = conn.execute(
        f"SELECT m.eaten_at, {ENTRY_NUTRIENTS} FROM entries e JOIN meals m ON m.id = e.meal_id"
        " WHERE m.user_id = ? AND m.eaten_at >= ? AND m.eaten_at < ?",
        (user_of(conn), start_utc, end_utc),
    ).fetchall()
    return [(r["eaten_at"], nutrients_of(r)) for r in rows]


def first_meal_at(conn: sqlite3.Connection) -> str | None:
    """When the user's earliest meal was eaten (UTC), or None before anything is logged."""
    return conn.execute(
        "SELECT min(eaten_at) FROM meals WHERE user_id = ?", (user_of(conn),)
    ).fetchone()[0]


# --- Targets --------------------------------------------------------------------------------

TARGET_KEYS = ("kcal", "protein_g", "carbs_g", "fat_g")


def get_targets(conn: sqlite3.Connection) -> dict[str, float | None]:
    row = conn.execute(
        "SELECT kcal, protein_g, carbs_g, fat_g FROM targets WHERE user_id = ?", (user_of(conn),)
    ).fetchone()
    return {k: (row[k] if row else None) for k in TARGET_KEYS}


def set_targets(conn: sqlite3.Connection, changes: Mapping[str, float | None]) -> dict:
    """Update the given targets (None clears one); others keep their value."""
    merged = {**get_targets(conn), **changes}
    with transaction(conn):
        _write_targets(conn, merged)
    return get_targets(conn)


def _write_targets(conn: sqlite3.Connection, values: Mapping[str, float | None]) -> None:
    conn.execute(
        "INSERT INTO targets (user_id, kcal, protein_g, carbs_g, fat_g) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET kcal = excluded.kcal,"
        " protein_g = excluded.protein_g, carbs_g = excluded.carbs_g, fat_g = excluded.fat_g",
        (user_of(conn), *(values[k] for k in TARGET_KEYS)),
    )


# --- Undo (dashboard edits) -----------------------------------------------------------------

# The columns an undo restores, per table. Foods only ever change name from the dashboard.
UNDO_COLUMNS = {
    "meals": ("id", "user_id", "eaten_at", "label", "note", "created_at"),
    "entries": ("id", "meal_id", "food_id", "grams", "estimated", *logic.NUTRIENTS, "created_at"),
    "foods": ("id", "name"),
}
UNDO_KEEP_S = 86400  # undo records older than a day are pruned


class UndoConflict(RuntimeError):
    """The rows changed after the edit, so undoing it would overwrite something newer."""


def capture(
    conn: sqlite3.Connection,
    *,
    meals: Iterable[int] = (),
    entries: Iterable[int] = (),
    foods: Iterable[int] = (),
    targets: bool = False,
) -> dict:
    """The current state of the given rows, JSON-ready: {table: {id: row or None}}, plus
    the targets when asked. A meal brings its entries along."""
    meals = set(meals)
    entries = set(entries)
    for m in meals:
        entries.update(r[0] for r in conn.execute("SELECT id FROM entries WHERE meal_id = ?", (m,)))
    state = {
        "meals": _rows(conn, "meals", meals),
        "entries": _rows(conn, "entries", entries),
        "foods": _rows(conn, "foods", foods),
    }
    if targets:
        state["targets"] = get_targets(conn)
    return state


def _rows(conn: sqlite3.Connection, table: str, ids: Iterable[int]) -> dict:
    cols = ", ".join(UNDO_COLUMNS[table])
    out = {}
    for i in sorted(set(ids)):
        row = conn.execute(f"SELECT {cols} FROM {table} WHERE id = ?", (i,)).fetchone()
        out[str(i)] = dict(row) if row else None
    return out


def record_undo(
    conn: sqlite3.Connection,
    summary: str,
    before: dict,
    *,
    meals: Iterable[int] = (),
) -> int:
    """Remember an edit for Undo: `before` from capture() ahead of it, and `meals` it created.

    The after state covers the same rows plus the new meals and their entries; rows that
    only exist after the edit are recorded as absent before it.
    """
    entries = {int(i) for i in before.get("entries", {})}
    meal_ids = {int(i) for i in before.get("meals", {})} | set(meals)
    after = capture(
        conn,
        meals=meal_ids,
        entries=entries,
        foods=map(int, before.get("foods", {})),
        targets="targets" in before,
    )
    full = {t: dict(rows) for t, rows in before.items()}
    for table in UNDO_COLUMNS:
        for i in after[table]:
            full.setdefault(table, {}).setdefault(i, None)
    now = int(time.time())
    with transaction(conn):
        conn.execute("DELETE FROM undo_actions WHERE created_at < ?", (now - UNDO_KEEP_S,))
        return conn.execute(
            "INSERT INTO undo_actions (user_id, summary, before, after, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_of(conn), summary, json.dumps(full), json.dumps(after), now),
        ).lastrowid


def get_undo(conn: sqlite3.Connection, undo_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, summary, used, created_at FROM undo_actions WHERE id = ? AND user_id = ?",
        (undo_id, user_of(conn)),
    ).fetchone()


def undo(conn: sqlite3.Connection, undo_id: int) -> str:
    """Put the rows an edit touched back as they were. Returns the edit's summary.

    Raises NotFound for an unknown or already undone edit, and UndoConflict if any of those
    rows changed since (the undo would overwrite the newer change).
    """
    row = conn.execute(
        "SELECT summary, before, after FROM undo_actions WHERE id = ? AND user_id = ? AND used = 0",
        (undo_id, user_of(conn)),
    ).fetchone()
    if row is None:
        raise NotFound("nothing to undo")
    before, after = json.loads(row["before"]), json.loads(row["after"])
    with transaction(conn):
        current = {t: _rows(conn, t, map(int, after.get(t, {}))) for t in UNDO_COLUMNS}
        if "targets" in after:
            current["targets"] = get_targets(conn)
        for i in after.get("meals", {}):
            # an entry added to this meal since would be lost or orphaned by the undo
            rows = conn.execute("SELECT id FROM entries WHERE meal_id = ?", (int(i),))
            ids = {str(r[0]) for r in rows}
            if not ids <= set(after.get("entries", {})):
                raise UndoConflict("that meal changed since")
        if json.loads(json.dumps(current)) != after:
            raise UndoConflict("those entries changed since")
        # Meals first (entries point at them), then entries, then meals that didn't exist
        meals = before.get("meals", {})
        for i, r in meals.items():
            if r is not None:
                _restore(conn, "meals", int(i), r)
        for i, r in before.get("entries", {}).items():
            if r is None:
                conn.execute("DELETE FROM entries WHERE id = ?", (int(i),))
            else:
                _restore(conn, "entries", int(i), r)
        for i, r in meals.items():
            if r is None:
                conn.execute("DELETE FROM meals WHERE id = ?", (int(i),))
        for i, r in before.get("foods", {}).items():
            _restore(conn, "foods", int(i), r)
        if "targets" in before:
            _write_targets(conn, before["targets"])
        conn.execute("UPDATE undo_actions SET used = 1 WHERE id = ?", (undo_id,))
    return row["summary"]


def _restore(conn: sqlite3.Connection, table: str, row_id: int, values: dict) -> None:
    cols = [c for c in UNDO_COLUMNS[table] if c != "id"]
    exists = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if exists:
        conn.execute(
            f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
            (*(values[c] for c in cols), row_id),
        )
    else:
        conn.execute(
            f"INSERT INTO {table} (id, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})",
            (row_id, *(values[c] for c in cols)),
        )


# --- Body weight (#94) ----------------------------------------------------------------------

WEIGHT_COLS = "id, measured_at, kg, source"


@dataclass
class WeightCounts:
    added: int = 0
    updated: int = 0
    unchanged: int = 0


def add_weights(
    conn: sqlite3.Connection, readings: Iterable[tuple[str, float]], source: str
) -> WeightCounts:
    """Save (UTC measured_at, kg) weigh-ins for the connection's user. A reading at a time
    already saved replaces it, so a phone resending the same readings changes nothing. The
    phone ("health") never brings back a weigh-in the person deleted; chat or the app does."""
    user_id = user_of(conn)
    counts = WeightCounts()
    with transaction(conn):
        for measured_at, kg in readings:
            row = conn.execute(
                "SELECT id, kg, deleted FROM weights WHERE user_id = ? AND measured_at = ?",
                (user_id, measured_at),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO weights (user_id, measured_at, kg, source) VALUES (?, ?, ?, ?)",
                    (user_id, measured_at, kg, source),
                )
                counts.added += 1
            elif row["deleted"] and source == "health":
                counts.unchanged += 1
            elif row["deleted"]:
                conn.execute(
                    "UPDATE weights SET kg = ?, source = ?, deleted = 0 WHERE id = ?",
                    (kg, source, row["id"]),
                )
                counts.added += 1
            elif abs(row["kg"] - kg) > 1e-6:
                conn.execute(
                    "UPDATE weights SET kg = ?, source = ? WHERE id = ?", (kg, source, row["id"])
                )
                counts.updated += 1
            else:
                counts.unchanged += 1
    return counts


def add_weight(conn: sqlite3.Connection, measured_at: str, kg: float, source: str) -> sqlite3.Row:
    """Save one weigh-in (replacing one at the same time) and return its row."""
    add_weights(conn, [(measured_at, kg)], source)
    return conn.execute(
        f"SELECT {WEIGHT_COLS} FROM weights WHERE user_id = ? AND measured_at = ?",
        (user_of(conn), measured_at),
    ).fetchone()


def weights_between(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    """The user's weigh-ins in UTC [start, end), oldest first."""
    return conn.execute(
        f"SELECT {WEIGHT_COLS} FROM weights WHERE user_id = ? AND NOT deleted"
        " AND measured_at >= ? AND measured_at < ? ORDER BY measured_at",
        (user_of(conn), start, end),
    ).fetchall()


def latest_weight(conn: sqlite3.Connection, before: str | None = None) -> sqlite3.Row | None:
    """The user's most recent weigh-in, or the last one before a UTC time."""
    return conn.execute(
        f"SELECT {WEIGHT_COLS} FROM weights WHERE user_id = ? AND NOT deleted"
        " AND measured_at < ? ORDER BY measured_at DESC LIMIT 1",
        (user_of(conn), before or "9999"),
    ).fetchone()


def delete_weight(conn: sqlite3.Connection, weight_id: int) -> sqlite3.Row:
    """Delete one of the user's weigh-ins and return it. The row stays, marked deleted, so
    the phone resending it doesn't bring it back."""
    with transaction(conn):
        row = conn.execute(
            f"SELECT {WEIGHT_COLS} FROM weights WHERE id = ? AND user_id = ? AND NOT deleted",
            (weight_id, user_of(conn)),
        ).fetchone()
        if row is None:
            raise NotFound(f"no weigh-in {weight_id}")
        conn.execute("UPDATE weights SET deleted = 1 WHERE id = ?", (weight_id,))
    return row


# --- Workouts (#94) -------------------------------------------------------------------------

SET_COLS = (
    "position, exercise, exercise_notes, superset_id, set_index, set_type, weight_kg, reps,"
    " distance_m, duration_s, rpe"
)
WORKOUT_COLS = "id, started_at, ended_at, title, description, source"


@dataclass
class ImportCounts:
    added: int = 0
    updated: int = 0
    unchanged: int = 0


def _set_rows(workout) -> list[tuple]:
    return [
        (
            i,
            s.exercise,
            s.exercise_notes,
            s.superset_id,
            s.set_index,
            s.set_type,
            s.weight_kg,
            s.reps,
            s.distance_m,
            s.duration_s,
            s.rpe,
        )
        for i, s in enumerate(workout.sets)
    ]


def _same(a: tuple, b: tuple) -> bool:
    return all(
        abs(x - y) < 1e-6 if isinstance(x, float) and isinstance(y, float) else x == y
        for x, y in zip(a, b, strict=True)
    )


def import_workouts(conn: sqlite3.Connection, workouts: Iterable, source: str) -> ImportCounts:
    """Save parsed workouts (hevy.Workout) for the connection's user. A workout already saved
    at the same start time is replaced, so importing a newer export of the same history
    only adds what's new."""
    user_id = user_of(conn)
    counts = ImportCounts()
    with transaction(conn):
        for w in workouts:
            rows = _set_rows(w)
            head = (w.ended_at, w.title, w.description)
            old = conn.execute(
                "SELECT id, ended_at, title, description FROM workouts"
                " WHERE user_id = ? AND started_at = ?",
                (user_id, w.started_at),
            ).fetchone()
            if old is not None:
                old_rows = [
                    tuple(r)
                    for r in conn.execute(
                        f"SELECT {SET_COLS} FROM workout_sets WHERE workout_id = ?"
                        " ORDER BY position",
                        (old["id"],),
                    )
                ]
                same_sets = len(old_rows) == len(rows) and all(
                    _same(a, b) for a, b in zip(old_rows, rows, strict=True)
                )
                if same_sets and tuple(old)[1:] == head:
                    counts.unchanged += 1
                    continue
                conn.execute(
                    "UPDATE workouts SET ended_at = ?, title = ?, description = ? WHERE id = ?",
                    (*head, old["id"]),
                )
                conn.execute("DELETE FROM workout_sets WHERE workout_id = ?", (old["id"],))
                workout_id = old["id"]
                counts.updated += 1
            else:
                workout_id = conn.execute(
                    "INSERT INTO workouts (user_id, started_at, ended_at, title, description,"
                    " source) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, w.started_at, *head, source),
                ).lastrowid
                counts.added += 1
            conn.executemany(
                f"INSERT INTO workout_sets (workout_id, {SET_COLS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(workout_id, *r) for r in rows],
            )
    return counts


def workouts_between(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    """The user's workouts starting in UTC [start, end), newest first."""
    return conn.execute(
        f"SELECT {WORKOUT_COLS} FROM workouts WHERE user_id = ? AND started_at >= ?"
        " AND started_at < ? ORDER BY started_at DESC",
        (user_of(conn), start, end),
    ).fetchall()


def first_workout_at(conn: sqlite3.Connection) -> str | None:
    """When the user's earliest workout started (UTC), or None before any is imported."""
    return conn.execute(
        "SELECT min(started_at) FROM workouts WHERE user_id = ?", (user_of(conn),)
    ).fetchone()[0]


def workouts_summary(conn: sqlite3.Connection) -> sqlite3.Row:
    """How many workouts the user has and when the latest one started (UTC), for You >
    Integrations and Training (#105)."""
    return conn.execute(
        "SELECT count(*) AS count, max(started_at) AS latest FROM workouts WHERE user_id = ?",
        (user_of(conn),),
    ).fetchone()


def get_workout(conn: sqlite3.Connection, workout_id: int) -> sqlite3.Row:
    row = conn.execute(
        f"SELECT {WORKOUT_COLS} FROM workouts WHERE id = ? AND user_id = ?",
        (workout_id, user_of(conn)),
    ).fetchone()
    if row is None:
        raise NotFound(f"no workout {workout_id}")
    return row


def workout_sets(conn: sqlite3.Connection, workout_ids: Iterable[int]) -> dict[int, list]:
    """Each workout's sets in order, keyed by workout id. Callers pass ids they already read
    for this user (workouts_between, get_workout)."""
    ids = list(workout_ids)
    out: dict[int, list] = {i: [] for i in ids}
    for chunk in range(0, len(ids), 500):
        part = ids[chunk : chunk + 500]
        marks = ", ".join("?" * len(part))
        for r in conn.execute(
            f"SELECT workout_id, {SET_COLS} FROM workout_sets WHERE workout_id IN ({marks})"
            " ORDER BY workout_id, position",
            part,
        ):
            out[r["workout_id"]].append(r)
    return out


def delete_workout(conn: sqlite3.Connection, workout_id: int) -> sqlite3.Row:
    """Delete one of the user's workouts and its sets; returns it. Importing an export that
    still has it brings it back."""
    with transaction(conn):
        row = get_workout(conn, workout_id)
        conn.execute("DELETE FROM workouts WHERE id = ?", (workout_id,))
    return row


# --- Activity (#94) -------------------------------------------------------------------------


def add_activity(
    conn: sqlite3.Connection, readings: Iterable[tuple[str, str, str, float]]
) -> ImportCounts:
    """Save (metric, period, UTC start_at, qty) activity readings for the connection's user;
    period is "day" for a whole day's total, "part" for an hour or so. The phone resends a
    bucket as it fills up (today's steps so far), so a reading for a bucket already saved
    replaces it. Every reading sent is marked as just sent, changed or not, which dates the
    phone's last send and picks between a day's whole total and its parts."""
    user_id = user_of(conn)
    counts = ImportCounts()
    with transaction(conn):
        for metric, period, start_at, qty in readings:
            row = conn.execute(
                "SELECT id, qty FROM activity WHERE user_id = ? AND metric = ? AND period = ?"
                " AND start_at = ?",
                (user_id, metric, period, start_at),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO activity (user_id, metric, period, start_at, qty)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (user_id, metric, period, start_at, qty),
                )
                counts.added += 1
                continue
            if abs(row["qty"] - qty) > 1e-6:
                counts.updated += 1
            else:
                counts.unchanged += 1
            conn.execute(
                "UPDATE activity SET qty = ?, source = 'phone', updated_at = datetime('now')"
                " WHERE id = ?",
                (qty, row["id"]),
            )
    return counts


@dataclass
class DayImportCounts:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    kept: int = 0  # days the phone sent, left as they are


def import_activity_days(
    conn: sqlite3.Connection, days: Iterable[tuple[str, str, str, float]]
) -> DayImportCounts:
    """Save (metric, UTC day start, UTC day end, total) whole days from Health's export for
    the connection's user. A day the phone sent anything for (its total or its hours) is kept
    as it is: the phone's figure is Health's own, the export's is pieced together. A day an
    earlier export brought in is refreshed, since that export may have caught it half done.
    Imported days are dated to their start and marked 'export', so they never count as the
    phone's last send."""
    user_id = user_of(conn)
    counts = DayImportCounts()
    with transaction(conn):
        for metric, start_at, end_at, qty in days:
            rows = conn.execute(
                "SELECT id, period, source, qty FROM activity WHERE user_id = ? AND metric = ?"
                " AND start_at >= ? AND start_at < ?",
                (user_id, metric, start_at, end_at),
            ).fetchall()
            if any(r["source"] == "phone" for r in rows):
                counts.kept += 1
                continue
            mine = next((r for r in rows if r["period"] == "day"), None)
            if mine is None:
                conn.execute(
                    "INSERT INTO activity (user_id, metric, period, start_at, qty, updated_at,"
                    " source) VALUES (?, ?, 'day', ?, ?, ?, 'export')",
                    (user_id, metric, start_at, qty, start_at.replace("T", " ").rstrip("Z")),
                )
                counts.added += 1
            elif abs(mine["qty"] - qty) > 1e-6:
                conn.execute("UPDATE activity SET qty = ? WHERE id = ?", (qty, mine["id"]))
                counts.updated += 1
            else:
                counts.unchanged += 1
    return counts


def activity_between(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    """The user's activity readings starting in UTC [start, end), oldest first."""
    return conn.execute(
        "SELECT metric, period, start_at, qty, updated_at FROM activity WHERE user_id = ?"
        " AND start_at >= ? AND start_at < ? ORDER BY start_at, metric",
        (user_of(conn), start, end),
    ).fetchall()


def first_activity_at(conn: sqlite3.Connection) -> str | None:
    """When the user's earliest activity reading starts (UTC), or None without any."""
    return conn.execute(
        "SELECT min(start_at) FROM activity WHERE user_id = ?", (user_of(conn),)
    ).fetchone()[0]


def latest_activity_at(conn: sqlite3.Connection, metrics: Iterable[str]) -> str | None:
    """When the user's latest reading of any of `metrics` starts (UTC), or None."""
    metrics = list(metrics)
    marks = ", ".join("?" * len(metrics))
    return conn.execute(
        f"SELECT max(start_at) FROM activity WHERE user_id = ? AND metric IN ({marks})",
        (user_of(conn), *metrics),
    ).fetchone()[0]


def last_activity_sync(conn: sqlite3.Connection) -> str | None:
    """When the phone last sent the user's activity (UTC storage format), or None."""
    row = conn.execute(
        "SELECT max(updated_at) FROM activity WHERE user_id = ? AND source = 'phone'",
        (user_of(conn),),
    ).fetchone()
    return row[0].replace(" ", "T") + "Z" if row[0] else None
