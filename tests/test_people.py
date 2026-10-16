"""Adding a second person without the command line (#109): People and invite links, Connect
iPhone, and each person's own Hevy key."""

from __future__ import annotations

import dataclasses
import re
import threading
from contextlib import closing

import pytest
import test_web
from test_auth import PASSWORD
from test_hevy_sync import FakeHevy, api_workout
from test_web import ORIGIN, client_for, sign_in
from test_web_edits import follow

from chomp import db, health, hevy_sync, store, users, web

HER_PASSWORD = "her very own password"
public_settings = test_web.public_settings
LINK = "https://www.icloud.com/shortcuts/0123456789abcdef0123456789abcdef"


@pytest.fixture
def owner(public_settings):
    with client_for(public_settings) as http:
        sign_in(http)
        yield http


def invite_from(page: str) -> str:
    """The path of the invite link a page shows."""
    m = re.search(r'id="invite-link" value="https://[^/"]+(/app/invite/[^"]+)"', page)
    assert m, "no invite link on the page"
    return m.group(1)


def add_sam(owner) -> tuple[int, str]:
    r = owner.post("/app/you/people", data={"name": " Sam "}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    return int(re.search(r'action="/app/you/people/(\d+)/invite"', r.text).group(1)), invite_from(
        r.text
    )


def accept(http, path: str, password: str = HER_PASSWORD, again: str | None = None):
    return http.post(path, data={"password": password, "again": again or password}, headers=ORIGIN)


def test_owner_adds_someone_who_picks_their_own_password(public_settings, owner, conn):
    assert 'href="/app/you/people"' in owner.get("/app/you").text
    sam, path = add_sam(owner)
    page = owner.get("/app/you/people").text
    assert "Sam" in page and "Invited · link open until" in page
    assert users.find_by_password(conn, HER_PASSWORD, PASSWORD) is None
    with client_for(public_settings) as her:
        page = her.get(path).text
        assert "Welcome, Sam" in page and 'autocomplete="new-password"' in page
        assert accept(her, path, again="something else entirely").status_code == 400
        assert "at least 12" in accept(her, path, "short").text
        assert accept(her, path, PASSWORD).status_code == 400  # the owner's password
        r = her.post(path, data={"password": HER_PASSWORD, "again": HER_PASSWORD})
        assert r.status_code == 403  # not from the page
        r = accept(her, path)
        assert r.status_code == 303 and r.headers["location"] == "/app"
        assert web.COOKIE in r.cookies or her.cookies.get(web.COOKIE)
        page = her.get("/app/you").text
        assert "Sam" in page and 'href="/app/you/people"' not in page  # not the owner
        assert her.get("/app/you/people").status_code == 404
        assert her.post("/app/you/people", data={"name": "Eve"}, headers=ORIGIN).status_code == 404
    assert users.find_by_password(conn, HER_PASSWORD, PASSWORD) == sam
    with client_for(public_settings) as again:
        assert again.get(path).status_code == 410  # used up
        r = again.post(
            "/app/login", data={"password": HER_PASSWORD, "next": "/app"}, headers=ORIGIN
        )
        assert r.status_code == 303
    assert "1 meal" not in owner.get("/app/you/people").text and "0 meals logged" in (
        owner.get("/app/you/people").text
    )


def test_bad_and_expired_links(public_settings, owner, conn):
    sam, path = add_sam(owner)
    conn.execute("UPDATE user_invites SET expires_at = 1")
    with client_for(public_settings) as her:
        assert her.get(path).status_code == 410
        assert accept(her, path).status_code == 410
        assert her.get("/app/invite/chomp_invite_nope").status_code == 410
    assert "Invite expired" in owner.get("/app/you/people").text
    page = owner.post(f"/app/you/people/{sam}/invite", headers=ORIGIN).text
    fresh = invite_from(page)
    assert fresh != path
    with client_for(public_settings) as her:
        assert accept(her, fresh).status_code == 303


def test_reset_password_keeps_the_old_one_until_used(public_settings, owner, conn):
    sam = users.add_user(conn, "Sam", HER_PASSWORD, PASSWORD)
    page = owner.get(f"/app/you/people/{sam}").text
    assert "Reset password" in page
    path = invite_from(owner.post(f"/app/you/people/{sam}/invite", headers=ORIGIN).text)
    assert users.find_by_password(conn, HER_PASSWORD, PASSWORD) == sam
    assert "password reset link open" in owner.get("/app/you/people").text
    with client_for(public_settings) as her:
        assert accept(her, path, "a brand new password").status_code == 303
    assert users.find_by_password(conn, HER_PASSWORD, PASSWORD) is None
    assert users.find_by_password(conn, "a brand new password", PASSWORD) == sam


def test_rename_and_remove(public_settings, owner, conn):
    sam = users.add_user(conn, "Sam", HER_PASSWORD, PASSWORD)
    with closing(db.connect(public_settings.db_path, sam)) as hers:
        store.add_weights(hers, [("2026-10-13T11:00:00Z", 60.0)], "app")
        store.set_targets(hers, {"kcal": 1800})
        health.new_token(hers, sam)
        hevy_sync.save_key(hers, sam, "her-hevy-key")
    other = users.add_user(conn, "Max", "max's own password", PASSWORD)
    r = owner.post(f"/app/you/people/{sam}/rename", data={"name": "Max"}, headers=ORIGIN)
    assert r.status_code == 400 and "already a user named" in r.text
    r = owner.post(f"/app/you/people/{sam}/rename", data={"name": "Samantha"}, headers=ORIGIN)
    assert r.status_code == 303 and users.name_of(conn, sam) == "Samantha"
    r = owner.post(f"/app/you/people/{sam}/remove", data={"confirm": "Sam"}, headers=ORIGIN)
    assert r.status_code == 400 and users.name_of(conn, sam) == "Samantha"
    assert (
        owner.post(f"/app/you/people/{sam}/remove", data={"confirm": "samantha"}).status_code == 403
    )
    r = owner.post(f"/app/you/people/{sam}/remove", data={"confirm": "samantha"}, headers=ORIGIN)
    assert r.status_code == 303 and users.name_of(conn, sam) is None
    for table in ("weights", "targets", "ingest_tokens", "hevy_keys"):
        assert (
            conn.execute(f"SELECT count(*) FROM {table} WHERE user_id = ?", (sam,)).fetchone()[0]
            == 0
        )
    assert users.name_of(conn, other) == "Max"
    assert owner.get("/app/you/people/1").status_code == 404  # the owner isn't removable here
    assert (
        owner.post(
            "/app/you/people/1/remove", data={"confirm": "owner"}, headers=ORIGIN
        ).status_code
        == 404
    )
    with pytest.raises(users.UserError):
        users.remove_user(conn, users.OWNER_ID)


def test_connect_iphone_makes_a_token_shown_once(public_settings, owner, conn):
    page = owner.get("/app/you/integrations").text
    assert 'action="/app/you/integrations/ingest-token"' in page and "Connect iPhone" in page
    assert "Share your Shortcut" in page  # the owner's
    page = owner.post("/app/you/integrations/ingest-token", headers=ORIGIN).text
    token = re.search(r'id="ingest-token" value="([^"]+)"', page).group(1)
    assert "README, step 9" in page  # no shared Shortcut yet
    r = owner.post(
        health.PATH,
        json={"weight": {"date": "2026-10-13T07:00", "value": 80, "unit": "kg"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    page = owner.get("/app/you/integrations").text
    assert token not in page and "Replace token" in page
    assert owner.post("/app/you/integrations/ingest-token").status_code == 403

    r = owner.post(
        "/app/you/integrations/shortcut-link", data={"url": "https://evil.example"}, headers=ORIGIN
    )
    assert "problem=shortcut_link" in r.headers["location"]
    r = owner.post("/app/you/integrations/shortcut-link", data={"url": LINK}, headers=ORIGIN)
    assert r.status_code == 303 and users.get_setting(conn, web.SHORTCUT_LINK) == LINK
    sam = users.add_user(conn, "Sam", HER_PASSWORD, PASSWORD)
    with client_for(public_settings) as her:
        her.post("/app/login", data={"password": HER_PASSWORD, "next": "/app"}, headers=ORIGIN)
        page = her.get("/app/you/integrations").text
        assert "Share your Shortcut" not in page
        page = her.post("/app/you/integrations/ingest-token", headers=ORIGIN).text
        assert f'href="{LINK}"' in page and "Add Shortcut" in page
        hers = re.search(r'id="ingest-token" value="([^"]+)"', page).group(1)
        r = her.post("/app/you/integrations/shortcut-link", data={"url": ""}, headers=ORIGIN)
        assert r.status_code == 403 and users.get_setting(conn, web.SHORTCUT_LINK) == LINK
    with closing(db.connect(public_settings.db_path)) as c:
        assert health.user_of_token(c, hers) == sam and health.user_of_token(c, token) == 1


def test_each_person_syncs_with_their_own_hevy_key(public_settings, conn, monkeypatch):
    fake = FakeHevy()
    fake.workouts = [api_workout()]
    keys = []

    def client(key):
        keys.append(key)
        return fake.client()

    monkeypatch.setattr(hevy_sync, "HevyClient", client)
    s = dataclasses.replace(public_settings, hevy_api_key="server-key")
    sam = users.add_user(conn, "Sam", HER_PASSWORD, PASSWORD)
    assert hevy_sync.keyed_users(conn, s) == [1]
    assert hevy_sync.key_for(conn, s, sam) == ""
    with client_for(s) as her:
        her.post("/app/login", data={"password": HER_PASSWORD, "next": "/app"}, headers=ORIGIN)
        page = her.get("/app/you/integrations").text
        assert "Sync now" not in page and 'name="key"' in page
        r = her.post("/app/you/integrations/hevy-key", data={"key": "no spaces!"}, headers=ORIGIN)
        assert "problem=hevy_key_bad" in r.headers["location"]
        page = follow(
            her,
            her.post("/app/you/integrations/hevy-key", data={"key": "sam-key-123"}, headers=ORIGIN),
        )
        assert "Synced with Hevy: 1 new workout" in page and "your Hevy API key" in page
        assert keys == ["sam-key-123"]
        fake.status = 401
        r = her.post("/app/you/integrations/hevy-key", data={"key": "wrong-key-1"}, headers=ORIGIN)
        assert "problem=hevy_key" in r.headers["location"]
        assert hevy_sync.saved_key(conn, sam) == ""  # a refused key isn't kept
        fake.status = 200
        her.post("/app/you/integrations/hevy-key", data={"key": "sam-key-123"}, headers=ORIGIN)
        assert hevy_sync.keyed_users(conn, s) == [1, sam]
        keys.clear()
        results = hevy_sync.run_all(s)
        assert keys == ["server-key", "sam-key-123"] and set(results) == {1, sam}
        with closing(db.connect(s.db_path, sam)) as hers:
            assert store.workouts_summary(hers)["count"] == 1
        r = her.post("/app/you/integrations/hevy-key", data={"remove": "1"}, headers=ORIGIN)
        assert r.status_code == 303 and hevy_sync.key_for(conn, s, sam) == ""
    # The owner's own key wins over HEVY_API_KEY
    hevy_sync.save_key(conn, 1, "owner-own-key")
    assert hevy_sync.key_for(conn, s, 1) == "owner-own-key"
    stop = threading.Event()
    assert hevy_sync.start_background(s, stop) is not None  # runs even with no key yet
    stop.set()
