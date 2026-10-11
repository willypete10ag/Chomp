"""Food photos (#74): one picture per food, kept as a JPEG file named by the food's id in a
folder next to the database (PHOTOS_DIR), and copied by nightly backups.

Pictures come from Open Food Facts (the product's front photo, for any food with a barcode)
or from the person, on the food's dashboard page. Label photos are never used. A photo the
person removes leaves a `<id>.none` marker, so Open Food Facts doesn't put it back.
Every picture is re-encoded on the way in: turned upright, at most 800 px on its longest
side, with no EXIF (no location or camera details) left in it.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from chomp import db
from chomp.sources import SourceError
from chomp.sources.off import OFFClient

log = logging.getLogger("chomp.photos")

MAX_SIDE = 800
MAX_PIXELS = 50_000_000  # refuse bigger images (decompression bombs) before decoding them
QUALITY = 82
BACKFILL_DONE = ".off-backfill-done"
BACKFILL_PAUSE_S = 1.0  # between Open Food Facts requests, to be polite


class PhotoError(ValueError):
    """The picture couldn't be used; the message is fit to show the person."""


def prepare(data: bytes) -> bytes:
    """Any photo -> an upright JPEG at most MAX_SIDE px on its longest side, no metadata."""
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(data))  # reads only the header
        # Pillow only refuses images over twice MAX_IMAGE_PIXELS, so check before decoding
        if img.width * img.height > MAX_PIXELS:
            raise PhotoError("That photo is too large. Try a smaller one.")
        img.load()
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as e:
        raise PhotoError("That file isn't a photo Chomp can read. Try a JPEG or PNG.") from e
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (255, 255, 255))  # transparent bits go white
        flat.paste(img, mask=img.getchannel("A"))
        img = flat
    else:
        img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=QUALITY, optimize=True, progressive=True)
    return out.getvalue()


class Photos:
    """The photo folder, plus filling missing photos from Open Food Facts in the background."""

    def __init__(self, folder: str | Path, off: OFFClient | None = None) -> None:
        self.folder = Path(folder)
        self.off = off
        self._tasks: set[asyncio.Task] = set()
        self._backfill_started = False
        # Saves, removals and automatic fills change a food's files one at a time, so a fill
        # that finishes late never overwrites a photo the person just chose or removed
        self._lock = threading.Lock()

    def path(self, food_id: int) -> Path:
        return self.folder / f"{int(food_id)}.jpg"

    def _marker(self, food_id: int) -> Path:
        return self.folder / f"{int(food_id)}.none"

    def version(self, food_id: int) -> int | None:
        """A number that changes whenever the food's photo does (for cache busting), or None
        when it has no photo."""
        try:
            return self.path(food_id).stat().st_mtime_ns
        except OSError:
            return None

    def has(self, food_id: int) -> bool:
        return self.version(food_id) is not None

    def removed(self, food_id: int) -> bool:
        """The person removed this food's photo, so nothing fills it automatically."""
        return self._marker(food_id).exists()

    def save(self, food_id: int, data: bytes) -> None:
        """Store a picture as the food's photo, replacing any it had. Raises PhotoError."""
        jpeg = prepare(data)
        with self._lock:
            self._write(food_id, jpeg)
            self._marker(food_id).unlink(missing_ok=True)

    def _write(self, food_id: int, jpeg: bytes) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.path(food_id)
        partial = target.with_name(f".{target.name}.partial")
        partial.write_bytes(jpeg)
        os.replace(partial, target)

    def remove(self, food_id: int) -> bool:
        """Remove the food's photo; True if it had one."""
        with self._lock:
            had = self.has(food_id)
            self.folder.mkdir(parents=True, exist_ok=True)
            self._marker(food_id).touch()
            self.path(food_id).unlink(missing_ok=True)
        return had

    def _write_if_missing(self, food_id: int, jpeg: bytes) -> bool:
        """An automatic fill: only while the food still has no photo and none was removed."""
        with self._lock:
            if self.has(food_id) or self.removed(food_id):
                return False
            self._write(food_id, jpeg)
            return True

    # --- Open Food Facts ---

    async def fill_from_off(
        self, food_id: int, gtin: str | None, image_url: str | None = None
    ) -> bool:
        """Give a food without a photo its Open Food Facts product photo. True if it got one.
        Never raises: a missing photo is not worth an error."""
        if self.off is None or self.has(food_id) or self.removed(food_id):
            return False
        try:
            url = image_url or (await self.off.image_url(gtin) if gtin else None)
            if not url:
                return False
            jpeg = await asyncio.to_thread(prepare, await self.off.fetch_image(url))
        except (SourceError, PhotoError) as e:
            log.info("no Open Food Facts photo for food %d: %s", food_id, e)
            return False
        if not await asyncio.to_thread(self._write_if_missing, food_id, jpeg):
            return False  # the person got there first
        log.info("food %d: photo from Open Food Facts", food_id)
        return True

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no event loop here (a sync tool call); skip it
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def fill_soon(self, food_id: int, gtin: str | None, image_url: str | None = None) -> None:
        """fill_from_off in the background, so a barcode lookup doesn't wait on a download."""
        if self.off is None or self.has(food_id) or self.removed(food_id):
            return
        if not gtin and not image_url:
            return
        self._spawn(self.fill_from_off(food_id, gtin, image_url))

    async def backfill(self, db_path: str) -> int:
        """Once ever: look up a photo for every food with a barcode that has none. Returns
        how many it found."""
        if (self.folder / BACKFILL_DONE).exists() or self.off is None:
            return 0
        with closing(db.connect(db_path)) as conn:
            rows = conn.execute(
                "SELECT id, gtin FROM foods WHERE gtin IS NOT NULL AND deleted = 0 ORDER BY id"
            ).fetchall()
        found = 0
        for i, row in enumerate(rows):
            if self.has(row["id"]) or self.removed(row["id"]):
                continue
            if i:
                await asyncio.sleep(BACKFILL_PAUSE_S)
            found += await self.fill_from_off(row["id"], row["gtin"])
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / BACKFILL_DONE).touch()
        log.info("photo backfill: %d of %d barcode foods got a photo", found, len(rows))
        return found

    def backfill_soon(self, db_path: str) -> None:
        """Start the one-time backfill in the background, at most once per process."""
        if self._backfill_started or self.off is None:
            return
        self._backfill_started = True
        if not (self.folder / BACKFILL_DONE).exists():
            self._spawn(self._backfill_logged(db_path))

    async def _backfill_logged(self, db_path: str) -> None:
        try:
            await self.backfill(db_path)
        except (OSError, sqlite3.Error):
            log.exception("photo backfill failed; it runs again after a restart")

    async def drain(self) -> None:
        """Wait for background photo work (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
