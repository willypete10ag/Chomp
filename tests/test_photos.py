"""#74: food photos. Open Food Facts product photos for barcode foods (on lookup, and a
one-time backfill), photos the person adds on the dashboard, and backups of them."""

from __future__ import annotations

import io
from datetime import datetime

import httpx
import pytest
from conftest import fixture_json
from mcp import Client
from PIL import Image
from starlette.testclient import TestClient
from test_barcode import NUTELLA
from test_tools import add_food
from test_web import ORIGIN, http, public_settings, sign_in  # noqa: F401

from chomp import backup, photos, store
from chomp.photos import Photos
from chomp.server import build_server, instructions
from chomp.sources.off import OFFClient, image_url, is_off_image

IMAGE = "https://images.openfoodfacts.org/images/products/301/762/401/0701/front_en.400.jpg"


def picture(size=(1200, 900), mode="RGB", fmt="JPEG", exif=None) -> bytes:
    img = Image.new(mode, size, (200, 80, 40, 255)[: len(mode)] if mode != "P" else 3)
    out = io.BytesIO()
    kwargs = {"exif": exif} if exif is not None else {}
    img.save(out, fmt, **kwargs)
    return out.getvalue()


class OFF:
    """Open Food Facts with Nutella's product (plus a front photo) and its image."""

    def __init__(self, image_url: str | None = IMAGE, image_status: int = 200):
        self.product = fixture_json("off/product_3017624010701_nutella.json")
        if image_url:
            self.product["product"]["image_front_url"] = image_url
        self.image_status = image_status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "images.openfoodfacts.org":
            if self.image_status != 200:
                return httpx.Response(self.image_status)
            return httpx.Response(200, content=picture(), headers={"content-type": "image/jpeg"})
        if request.url.path == f"/api/v2/product/{NUTELLA}":
            return httpx.Response(200, json=self.product)
        return httpx.Response(404, json={"status": 0})

    def client(self) -> OFFClient:
        return OFFClient(
            "Chomp-tests/0",
            http=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            backoff_s=0,
        )


# --- Pictures --------------------------------------------------------------------------------


def test_prepare_shrinks_to_800_and_makes_jpeg():
    out = Image.open(io.BytesIO(photos.prepare(picture((3000, 1500), fmt="PNG"))))
    assert out.format == "JPEG" and out.size == (800, 400)


def test_prepare_turns_upright_and_drops_exif():
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise to display
    exif[0x010F] = "PhoneMaker"
    out = Image.open(io.BytesIO(photos.prepare(picture((400, 200), exif=exif))))
    assert out.size == (200, 400)
    assert not out.getexif()


def test_prepare_flattens_transparency():
    out = Image.open(io.BytesIO(photos.prepare(picture((50, 50), mode="RGBA", fmt="PNG"))))
    assert out.mode == "RGB"


def test_prepare_refuses_non_pictures():
    with pytest.raises(photos.PhotoError, match="isn't a photo"):
        photos.prepare(b"not a picture")


def test_save_replace_remove(tmp_path):
    p = Photos(tmp_path / "photos")
    assert not p.has(7) and p.version(7) is None
    p.save(7, picture())
    first = p.version(7)
    assert p.has(7) and first is not None
    p.save(7, picture((600, 600)))
    assert p.version(7) != first
    assert p.remove(7) is True
    assert not p.has(7) and p.removed(7)
    p.save(7, picture())  # adding one again clears the removal
    assert p.has(7) and not p.removed(7)


# --- Open Food Facts -------------------------------------------------------------------------


def test_only_https_open_food_facts_images_are_used():
    assert is_off_image(IMAGE)
    assert is_off_image("https://static.openfoodfacts.org/images/x.jpg")
    assert not is_off_image("http://images.openfoodfacts.org/x.jpg")
    assert not is_off_image("https://openfoodfacts.org.evil.example/x.jpg")
    assert not is_off_image("https://images.openfoodfacts.org:8443/x.jpg")
    assert not is_off_image("file:///etc/passwd")
    assert image_url({"image_front_url": "https://evil.example/x.jpg", "image_url": IMAGE}) == IMAGE
    assert image_url({}) is None


@pytest.mark.anyio
async def test_fill_from_off_skips_removed_and_existing(tmp_path):
    upstream = OFF()
    p = Photos(tmp_path, upstream.client())
    assert await p.fill_from_off(1, f"0{NUTELLA}") is True
    assert Image.open(p.path(1)).size == (800, 600)
    n = len(upstream.requests)
    assert await p.fill_from_off(1, f"0{NUTELLA}") is False  # has one
    p.remove(2)
    assert await p.fill_from_off(2, f"0{NUTELLA}") is False  # the person removed it
    assert len(upstream.requests) == n


@pytest.mark.anyio
async def test_a_late_fill_never_undoes_the_persons_change(tmp_path, monkeypatch):
    p = Photos(tmp_path, OFF().client())
    real = p.off.fetch_image

    async def slow(url):  # the person removes the photo while the download runs
        data = await real(url)
        p.remove(1)
        return data

    monkeypatch.setattr(p.off, "fetch_image", slow)
    assert await p.fill_from_off(1, f"0{NUTELLA}") is False
    assert not p.has(1) and p.removed(1)


@pytest.mark.anyio
async def test_fill_from_off_without_a_photo_or_unreachable(tmp_path):
    p = Photos(tmp_path, OFF(image_url=None).client())
    assert await p.fill_from_off(1, f"0{NUTELLA}") is False
    p = Photos(tmp_path, OFF(image_status=503).client())
    assert await p.fill_from_off(1, f"0{NUTELLA}") is False
    assert not p.has(1)


@pytest.mark.anyio
async def test_lookup_barcode_gives_the_food_its_off_photo(settings, conn):
    p = Photos(settings.photos_path, (off := OFF().client()))
    server = build_server(settings, off=off, photos=p)
    async with Client(server) as client:
        out = (await client.call_tool("lookup_barcode", {"barcode": NUTELLA})).structured_content
        await p.drain()
    food_id = out["food"]["food_id"]
    assert p.has(food_id)


@pytest.mark.anyio
async def test_backfill_runs_once_for_barcode_foods(settings, conn):
    upstream = OFF()
    p = Photos(settings.photos_path, upstream.client())
    nutella = add_food(conn, "Nutella", 539, 6.3, 57.5, 30.9)
    conn.execute("UPDATE foods SET gtin = ? WHERE id = ?", (f"0{NUTELLA}", nutella))
    plain = add_food(conn, "Banana", 89, 1.1, 22.8, 0.3)
    assert await p.backfill(settings.db_path) == 1
    assert p.has(nutella) and not p.has(plain)
    p.remove(nutella)
    n = len(upstream.requests)
    assert await p.backfill(settings.db_path) == 0  # done once already
    assert len(upstream.requests) == n


# --- Dashboard -------------------------------------------------------------------------------


@pytest.fixture
def app(settings, conn):
    server = build_server(settings, photos=Photos(settings.photos_path))
    with TestClient(server.streamable_http_app(), follow_redirects=False) as client:
        yield client


def upload(client, food_id, data, back="", headers=None):
    return client.post(
        f"/app/foods/{food_id}/photo",
        data={"back": back} if back else {},
        files={"photo": ("pic.jpg", data, "image/jpeg")},
        headers=headers or {},
    )


def test_food_page_add_replace_remove(app, conn, settings):
    food_id = add_food(conn, "Banana", 89, 1.1, 22.8, 0.3)
    page = app.get(f"/app/foods/{food_id}").text
    assert "Add photo" in page and 'id="photo"' in page and "<img" not in page

    r = upload(app, food_id, picture(), back=f"/app/foods/{food_id}")
    assert r.status_code == 303 and "pic=saved" in r.headers["location"]
    page = app.get(r.headers["location"]).text
    assert "Photo saved." in page and 'alt="Photo of Banana"' in page
    assert "Replace photo" in page and "Remove photo" in page

    p = Photos(settings.photos_path)
    shown = app.get(f"/app/foods/{food_id}/photo?v={p.version(food_id)}")
    assert shown.status_code == 200 and shown.headers["content-type"] == "image/jpeg"
    assert "immutable" in shown.headers["cache-control"]

    r = app.post(f"/app/foods/{food_id}/photo/delete", data={"back": f"/app/foods/{food_id}"})
    assert "pic=removed" in r.headers["location"]
    assert not p.has(food_id) and p.removed(food_id)
    assert app.get(f"/app/foods/{food_id}/photo").status_code == 404


def test_upload_refuses_a_non_picture(app, conn):
    food_id = add_food(conn, "Banana", 89, 1.1, 22.8, 0.3)
    r = upload(app, food_id, b"plain text")
    assert "problem=photo" in r.headers["location"]
    assert "a photo Chomp can read" in app.get(r.headers["location"]).text


def test_upload_to_a_deleted_food_is_refused(app, conn, settings):
    food_id = add_food(conn, "Banana", 89, 1.1, 22.8, 0.3)
    store.delete_food(conn, food_id)
    assert upload(app, food_id, picture()).status_code == 404
    assert not Photos(settings.photos_path).has(food_id)


def test_thumbnails_on_foods_today_and_add(app, conn, settings):
    banana = add_food(conn, "Banana", 89, 1.1, 22.8, 0.3)
    add_food(conn, "Oats", 389, 16.9, 66.3, 6.9)
    Photos(settings.photos_path).save(banana, picture())
    app.post("/app/log", data={"food_id": banana, "grams": "120", "time": "08:00"})
    foods = app.get("/app/foods").text
    assert f'src="/app/foods/{banana}/photo?v=' in foods
    assert 'data-photo="1"' in foods and 'data-photo="0"' in foods
    assert '<span class="thumb none" aria-hidden="true">O</span>' in foods
    assert f"/app/foods/{banana}/photo?v=" in app.get("/app/add?q=banana").text
    assert f"/app/foods/{banana}/photo?v=" in app.get("/app").text


def test_photos_need_sign_in_and_same_origin(http, conn, public_settings):  # noqa: F811
    food_id = add_food(conn, "Banana", 89, 1.1, 22.8, 0.3)
    Photos(public_settings.photos_path).save(food_id, picture())
    assert http.get(f"/app/foods/{food_id}/photo").status_code == 401
    r = upload(http, food_id, picture())
    assert r.status_code == 303 and "/app/login" in r.headers["location"]
    sign_in(http)
    assert http.get(f"/app/foods/{food_id}/photo").status_code == 200
    assert upload(http, food_id, picture()).status_code == 403  # no Origin header
    assert upload(http, food_id, picture(), headers=ORIGIN).status_code == 303


# --- Chat ------------------------------------------------------------------------------------


def test_instructions_point_chat_photos_at_the_dashboard():
    assert "Photos" not in instructions("")
    text = instructions("https://chomp.example")
    assert "https://chomp.example/app/foods/<food_id>#photo" in text
    assert "never a food's picture" in text


@pytest.mark.anyio
async def test_save_food_has_no_photo_link_in_local_mode(settings, conn):
    server = build_server(settings, photos=Photos(settings.photos_path))
    args = {"name": "Bowl", "serving_g": 400, "kcal": 600, "protein_g": 40}
    args |= {"carbs_g": 60, "fat_g": 20}
    async with Client(server) as client:
        out = (await client.call_tool("save_food", args)).structured_content
    assert out["add_photo_url"] is None and out["food_id"]


# --- Backups ---------------------------------------------------------------------------------


def test_backup_copies_photos_and_restore_brings_back_missing(settings, conn, tmp_path):
    a, b, c = (add_food(conn, n, 100, 1, 1, 1) for n in ("A", "B", "C"))
    p = Photos(settings.photos_path)
    p.save(a, picture())
    p.remove(b)
    dest = tmp_path / "backups"
    made = backup.backup(settings.db_path, dest, 3, datetime(2026, 10, 11), settings.photos_path)
    assert (dest / "photos" / f"{a}.jpg").exists() and (dest / "photos" / f"{b}.none").exists()
    assert backup.copy_photos(settings.photos_path, dest / "photos") == 0  # nothing changed

    p.path(a).unlink()
    p.save(c, picture())
    backup.restore(made, settings.db_path, settings.migrations_dir, settings.photos_path)
    assert p.has(a) and p.has(c) and p.removed(b)


def test_restore_drops_photos_of_foods_added_after_the_backup(settings, conn, tmp_path):
    p = Photos(settings.photos_path)
    banana = add_food(conn, "Banana", 89, 1.1, 22.8, 0.3)
    p.save(banana, picture())
    dest = tmp_path / "backups"
    made = backup.backup(settings.db_path, dest, 3, datetime(2026, 10, 11), settings.photos_path)
    later = add_food(conn, "Pizza", 266, 11, 33, 10)
    p.save(later, picture())
    backup.backup(settings.db_path, dest, 3, datetime(2026, 10, 12), settings.photos_path)
    p.path(banana).unlink()

    backup.restore(made, settings.db_path, settings.migrations_dir, settings.photos_path)
    assert p.has(banana)  # from the backup
    assert not p.has(later)  # its id may go to the next new food
    aside = list(settings.photos_path.parent.glob("photos.pre-restore-*"))
    assert len(aside) == 1 and (aside[0] / f"{later}.jpg").exists()
