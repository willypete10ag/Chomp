"""The barcode source chain, shared by the lookup_barcode tool and the dashboard (#59):
foods already in Chomp, then USDA FoodData Central Branded foods, then Open Food Facts.
A hit is cached into `foods` and marked saved, so the next scan is instant."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from chomp import store
from chomp.sources import SourceError
from chomp.sources.fdc import FDCClient
from chomp.sources.off import OFFClient

log = logging.getLogger("chomp.barcode")


@dataclass
class Lookup:
    found_in: str | None  # 'saved', 'fdc', 'off', or None when nothing has it
    row: sqlite3.Row | None
    flags: list[str] = field(default_factory=list)  # data-quality notes from the source
    warnings: list[str] = field(default_factory=list)  # sources that couldn't be reached
    unreachable: bool = False  # every source failed, so a miss may not be a real miss
    image_url: str | None = None  # the source's product photo, when it gave one (#74)


async def lookup(conn: sqlite3.Connection, gtin14: str, fdc: FDCClient, off: OFFClient) -> Lookup:
    """Find a normalized 14-digit GTIN (see sources.gtin.normalize)."""
    row = store.food_by_gtin(conn, gtin14)
    if row is not None:
        return Lookup("saved", store.mark_saved(conn, row["id"]))
    warnings: list[str] = []
    tried = 0
    for name, client, enabled in (("fdc", fdc, fdc.enabled), ("off", off, True)):
        if not enabled:
            continue
        tried += 1
        try:
            rec = await client.lookup_gtin(gtin14)
        except SourceError as e:
            log.warning("barcode lookup: %s", e)
            warnings.append(str(e))
            continue
        if rec is not None:
            row, _ = store.cache_record(conn, rec)
            row = store.mark_saved(conn, row["id"])
            return Lookup(name, row, list(rec.flags), warnings, image_url=rec.image_url)
    return Lookup(None, None, warnings=warnings, unreachable=len(warnings) == tried)
