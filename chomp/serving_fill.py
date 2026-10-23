"""Serving sizes for used USDA foods, from their details: household measures for generic
foods, and the label serving in mL for branded drinks.

Generic foods (Foundation, SR Legacy) have no label serving, so they were cached with none
and the dashboard offered grams only. Their USDA details list household measures ("1 large"
= 50 g). Branded drinks were cached without their mL serving before it counted as one. The
foods someone has used or saved get one as their serving size, checked again every few
minutes for foods used since. Each food is asked about once per process.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import closing

from chomp import db, store
from chomp.sources import SourceError
from chomp.sources import fdc as fdc_source
from chomp.sources.fdc import FDCClient

log = logging.getLogger("chomp.serving_fill")

PAUSE_S = 0.5  # between FoodData Central requests
RESCAN_S = 600.0  # at most this often, for foods used since the last scan
_last_scan: dict[str, float] = {}
_tried: dict[str, set[int]] = {}  # foods already asked about, per database
_tasks: set[asyncio.Task] = set()

USED_WITHOUT_SERVING = """
    SELECT id, source_id FROM foods
    WHERE source = 'fdc' AND serving_g IS NULL AND deleted = 0 AND (
        saved = 1 OR verified = 1
        OR id IN (SELECT food_id FROM entries)
        OR id IN (SELECT food_id FROM portions)
        OR id IN (SELECT food_id FROM meal_template_items)
    )
    ORDER BY id
"""


async def backfill(db_path: str, fdc: FDCClient, pause_s: float = PAUSE_S) -> int:
    """Look up USDA details for each used FDC food without a serving size and fill in its
    household serving. Returns how many got one. Foods already asked about in this process
    are skipped, so one USDA lists no measures for isn't fetched again."""
    if not fdc.enabled:
        return 0
    tried = _tried.setdefault(db_path, set())
    with closing(db.connect(db_path)) as conn:
        rows = [r for r in conn.execute(USED_WITHOUT_SERVING) if r["id"] not in tried]
    filled = 0
    for i, row in enumerate(rows):
        if i:
            await asyncio.sleep(pause_s)
        try:
            detail = await fdc.food(row["source_id"])
        except SourceError as e:  # not marked tried: the next scan asks again
            log.info("no USDA details for food %d: %s", row["id"], e)
            continue
        tried.add(row["id"])
        record = fdc_source.to_record(detail) if detail else None
        if record is None or record.serving_g is None:
            continue
        with closing(db.connect(db_path)) as conn:
            food = store.get_foods(conn, [row["id"]]).get(row["id"])
            if food is not None and store.fill_serving(conn, food, record)["serving_g"]:
                filled += 1
    if rows:
        log.info("serving fill: %d of %d USDA foods got a serving size", filled, len(rows))
    return filled


def backfill_soon(db_path: str, fdc: FDCClient) -> None:
    """Start the backfill in the background, at most once every RESCAN_S per database."""
    now = time.monotonic()
    last = _last_scan.get(db_path)
    if not fdc.enabled or (last is not None and now - last < RESCAN_S):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # no event loop here (a sync tool call); try again next time
        return
    _last_scan[db_path] = now
    task = loop.create_task(_backfill_logged(db_path, fdc))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _backfill_logged(db_path: str, fdc: FDCClient) -> None:
    try:
        await backfill(db_path, fdc)
    except (OSError, sqlite3.Error):
        log.exception("serving fill failed; it runs again after a restart")
