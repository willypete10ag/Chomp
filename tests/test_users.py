"""#46: separate users. The migration hands existing data to the owner, each user sees only
their own log and targets, and the password typed at sign-in decides whose log it is."""

from __future__ import annotations

import dataclasses
import io
import json
import shutil
from contextlib import closing

import pytest
from test_auth import (
    BEARER,
    CALLBACK,
    CLIENT_ID,
    CLIENT_SECRET,
    INIT,
    MCP_HEADERS,
    PASSWORD,
    PUBLIC,
    pkce,
    start,
)
from test_tools import EGGS, WHITES, add_food
from test_web import ORIGIN, client_for

from chomp import cli, db, store, users
from tests.conftest import MIGRATIONS, SCHEMA

HER_PASSWORD = "her own long password"
WHEN = "2026-10-07T12:00:00Z"


@pytest.fixture
def public_settings(settings, conn):
    return dataclasses.replace(
        settings,
        host="0.0.0.0",
        public_url=PUBLIC,
        auth_password=PASSWORD,
        oauth_client_id=CLIENT_ID,
        oauth_client_secret=CLIENT_SECRET,
        bearer_token=BEARER,
    )


def add_her(conn) -> int:
    return users.add_user(conn, "Sam", HER_PASSWORD, PASSWORD)


def as_user(settings, user_id: int):
    return closing(db.connect(settings.db_path, user_id))


# --- migration ------------------------------------------------------------------------------


def test_migration_gives_existing_data_to_the_owner(tmp_path):
    old = tmp_path / "migrations"
    old.mkdir()
    for p in MIGRATIONS.glob("000[2-5]_*.sql"):
        shutil.copy(p, old)
    path = tmp_path / "chomp.db"
    with closing(db.open_db(path, SCHEMA, old)) as conn:
        assert db.schema_version(conn) == 5
        food = add_food(conn, "Egg", *EGGS)
        conn.execute("INSERT INTO meals (id, eaten_at, label) VALUES (7, ?, 'lunch')", (WHEN,))
        conn.execute(
            "INSERT INTO entries (meal_id, food_id, grams, kcal, protein_g, carbs_g, fat_g)"
            " VALUES (7, ?, 100, 143, 12.6, 0.7, 9.5)",
            (food,),
        )
        conn.execute("INSERT INTO aliases VALUES ('my eggs', ?, 100)", (food,))
        conn.execute("INSERT INTO meal_templates (id, name) VALUES (3, 'usual')")
        conn.execute(
            "INSERT INTO meal_template_items (template_id, food_id, grams) VALUES (3, ?, 50)",
            (food,),
        )
        conn.execute("INSERT INTO targets (id, kcal) VALUES (1, 2100)")
        conn.execute(
            "INSERT INTO oauth_tokens (token_hash, kind, grant_id, client_id, scopes, expires_at)"
            " VALUES ('h', 'access', 'g', 'c', 'chomp', 9999999999)"
        )
        conn.execute("INSERT INTO web_sessions (token_hash, expires_at) VALUES ('w', 9999999999)")
        conn.execute(
            "INSERT INTO undo_actions (summary, before, after, created_at)"
            " VALUES ('x', '{}', '{}', 1)"
        )
    with closing(db.open_db(path, SCHEMA, MIGRATIONS, users.OWNER_ID)) as conn:
        assert db.schema_version(conn) == 16
        # Rebuilding meals and meal_templates must not cascade into their children
        assert [e.grams for e in store.meal_entries(conn, 7)] == [100]
        assert store.get_template(conn, "usual")[1][0]["grams"] == 50
        assert store.get_meal(conn, 7)["label"] == "lunch"
        assert [(p["name"], p["grams"]) for p in store.portions_of(conn, [food])[food]] == [
            ("my eggs", 100)
        ]
        assert store.get_targets(conn)["kcal"] == 2100
        for table in ("oauth_tokens", "web_sessions"):
            assert [r[0] for r in conn.execute(f"SELECT user_id FROM {table}")] == [1]
        # Old undo snapshots lack user_id, so pending undos are dropped
        assert conn.execute("SELECT count(*) FROM undo_actions").fetchone()[0] == 0
        assert [tuple(r) for r in conn.execute("SELECT id, name FROM users")] == [(1, "owner")]
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert [tuple(r) for r in conn.execute("SELECT user_id, kcal FROM daily_totals")] == [
            (1, 143)
        ]


def test_a_migration_leaving_broken_references_is_rolled_back(tmp_path):
    bad = tmp_path / "migrations"
    bad.mkdir()
    (bad / "0002_bad.sql").write_text("INSERT INTO aliases VALUES ('ghost', 999, NULL);")
    with closing(db.connect(tmp_path / "x.db")) as conn:
        with pytest.raises(db.MigrationError, match="missing"):
            db.migrate(conn, SCHEMA, bad)
        assert db.schema_version(conn) == 1
        assert conn.execute("SELECT count(*) FROM aliases").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# --- users ----------------------------------------------------------------------------------


def test_passwords_are_hashed_unique_and_identify_the_user(conn):
    her = add_her(conn)
    stored = conn.execute("SELECT password_hash FROM users WHERE id = ?", (her,)).fetchone()[0]
    assert HER_PASSWORD not in stored and stored.startswith("scrypt$")
    assert users.find_by_password(conn, HER_PASSWORD, PASSWORD) == her
    assert users.find_by_password(conn, PASSWORD, PASSWORD) == users.OWNER_ID
    assert users.find_by_password(conn, "nope nope nope", PASSWORD) is None
    with pytest.raises(users.UserError, match="someone else"):
        users.add_user(conn, "Third", PASSWORD, PASSWORD)
    with pytest.raises(users.UserError, match="someone else"):
        users.add_user(conn, "Third", HER_PASSWORD, PASSWORD)
    with pytest.raises(users.UserError, match="already a user"):
        users.add_user(conn, "sam", "yet another password", PASSWORD)
    with pytest.raises(users.UserError, match="at least"):
        users.add_user(conn, "Third", "short", PASSWORD)


def test_server_refuses_an_owner_password_another_user_has(settings, conn, monkeypatch):
    import dataclasses

    from chomp import server

    add_her(conn)
    assert users.password_clash(conn, PASSWORD) is None
    assert users.password_clash(conn, HER_PASSWORD) == "Sam"
    clash = dataclasses.replace(
        settings,
        host="0.0.0.0",
        public_url="https://chomp.example.com",
        auth_password=HER_PASSWORD,
        oauth_client_id=CLIENT_ID,
        oauth_client_secret=CLIENT_SECRET,
    )
    monkeypatch.setattr("chomp.config.Settings.from_env", lambda: clash)
    with pytest.raises(SystemExit, match="Sam's password"):
        server.main()


def test_new_password_signs_the_user_out_everywhere(conn):
    her = add_her(conn)
    conn.execute(
        "INSERT INTO web_sessions (token_hash, user_id, expires_at) VALUES ('a', ?, 9e9)", (her,)
    )
    conn.execute("INSERT INTO web_sessions (token_hash, user_id, expires_at) VALUES ('b', 1, 9e9)")
    users.set_password(conn, her, "a brand new password", PASSWORD)
    assert users.find_by_password(conn, "a brand new password", PASSWORD) == her
    assert users.find_by_password(conn, HER_PASSWORD, PASSWORD) is None
    assert [r[0] for r in conn.execute("SELECT token_hash FROM web_sessions")] == ["b"]
    with pytest.raises(users.UserError, match="AUTH_PASSWORD"):
        users.set_password(conn, users.OWNER_ID, "a brand new password", PASSWORD)


def test_cli_adds_lists_and_renames_users(settings, conn, monkeypatch, capsys):
    monkeypatch.setattr("chomp.config.Settings.from_env", lambda: settings)
    monkeypatch.setattr("sys.stdin", io.StringIO(HER_PASSWORD + "\n"))
    assert cli.main(["user", "add", "Sam"]) == 0
    assert cli.main(["user", "rename", "owner", "Will"]) == 0
    assert cli.main(["user", "list"]) == 0
    out = capsys.readouterr().out
    assert "Will: 0 meals (owner" in out and "Sam: 0 meals" in out
    assert cli.main(["user", "rename", "nobody", "x"]) == 1


# --- separate logs --------------------------------------------------------------------------


def test_each_user_sees_only_their_own_log(settings, conn):
    her = add_her(conn)
    eggs = add_food(conn, "Egg", *EGGS)
    whites = add_food(conn, "Egg white", *WHITES)
    mine = store.log_meal(conn, [(eggs, 100)], WHEN, None)
    store.save_portion(conn, eggs, "egg", 50)
    store.save_template(conn, "usual", [(eggs, 100)])
    store.set_targets(conn, {"kcal": 2500})
    with as_user(settings, her) as hers:
        theirs = store.log_meal(hers, [(whites, 200)], WHEN, None)
        store.save_portion(hers, eggs, "egg", 60)  # same portion, her own weight
        store.save_template(hers, "usual", [(whites, 50)])  # same template name
        store.set_targets(hers, {"kcal": 1800})
        assert [m["id"] for m in store.meals_between(hers, "2026", "2027")] == [theirs]
        assert store.get_portion(hers, eggs, "egg")["grams"] == 60
        assert store.get_template(hers, "usual")[1][0]["grams"] == 50
        assert store.get_targets(hers)["kcal"] == 1800
        # My meal and entry don't exist for her
        for call in (
            lambda: store.get_meal(hers, mine),
            lambda: store.get_entry(hers, store.meal_entries(conn, mine)[0].id),
            lambda: store.update_meal(hers, mine, None, "hers now", None),
            lambda: store.add_to_meal(hers, mine, [(whites, 10)]),
            lambda: store.delete_meal(hers, mine),
        ):
            with pytest.raises(store.NotFound):
                call()
        # Foods are shared, but use counts are her own
        uses = {r["id"]: r["uses"] for r in store.list_used_foods(hers, 10)}
        assert uses == {whites: 1, eggs: 0}
    assert [m["id"] for m in store.meals_between(conn, "2026", "2027")] == [mine]
    assert store.get_portion(conn, eggs, "egg")["grams"] == 50
    assert store.get_targets(conn)["kcal"] == 2500
    assert store.get_meal(conn, mine)["label"] is None


def test_undo_is_per_user(settings, conn):
    her = add_her(conn)
    before = store.capture(conn, targets=True)
    store.set_targets(conn, {"kcal": 2000})
    undo_id = store.record_undo(conn, "Updated your daily goals", before)
    with as_user(settings, her) as hers:
        assert store.get_undo(hers, undo_id) is None
        with pytest.raises(store.NotFound):
            store.undo(hers, undo_id)
    store.undo(conn, undo_id)
    assert store.get_targets(conn)["kcal"] is None


# --- sign-in --------------------------------------------------------------------------------


def connector_token(http, password: str) -> str:
    verifier, challenge = pkce()
    r = start(http, challenge)
    session = r.headers["location"].split("session=")[1]
    r = http.post(
        "/consent", data={"session": session, "password": password}, follow_redirects=False
    )
    code = r.headers["location"].split("code=")[1].split("&")[0]
    tok = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "code_verifier": verifier,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    assert tok.status_code == 200, tok.text
    return tok.json()["access_token"]


def call_tool(http, token: str, name: str, arguments: dict) -> dict:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    r = http.post("/mcp", json=INIT, headers=headers)
    assert r.status_code == 200, r.text
    headers["mcp-session-id"] = r.headers["mcp-session-id"]
    http.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
    )
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    r = http.post("/mcp", json=call, headers=headers)
    assert r.status_code == 200, r.text
    body = r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        body = next(line[5:] for line in body.splitlines() if line.startswith("data:"))
    result = json.loads(body)["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def test_connectors_read_and_write_the_signed_in_users_log(public_settings, conn):
    her = add_her(conn)
    eggs = add_food(conn, "Egg", *EGGS)
    store.log_meal(conn, [(eggs, 100)], WHEN, "mine")
    with client_for(public_settings) as http:
        token = connector_token(http, HER_PASSWORD)
        assert call_tool(http, token, "server_info", {})["user"] == "Sam"
        out = call_tool(
            http,
            token,
            "log_entries",
            {"items": [{"food_id": eggs, "grams": 50}], "eaten_at": WHEN},
        )
        day = call_tool(http, token, "get_day", {"date": "2026-10-07"})
        assert [m["meal_id"] for m in day["meals"]] == [out["meal"]["meal_id"]]
        # The static bearer and the owner's own sign-in are the owner's log
        assert call_tool(http, BEARER, "server_info", {})["user"] == "owner"
        mine = call_tool(http, connector_token(http, PASSWORD), "get_day", {"date": "2026-10-07"})
        assert [m["label"] for m in mine["meals"]] == ["mine"]
    with as_user(public_settings, her) as hers:
        assert len(store.meals_between(hers, "2026", "2027")) == 1


def test_dashboard_shows_the_signed_in_users_log(public_settings, conn):
    add_her(conn)
    eggs = add_food(conn, "Egg, whole", *EGGS)
    store.log_meal(conn, [(eggs, 100)], WHEN, "lunchbox")
    with client_for(public_settings) as http:
        r = http.post("/app/login", data={"password": HER_PASSWORD, "next": "/app"}, headers=ORIGIN)
        assert r.status_code == 303
        page = http.get("/app?date=2026-10-07").text
        assert "Lunchbox" not in page and "Egg, whole" not in page
        assert "Sam" in http.get("/app/you").text
        http.cookies.clear()
        r = http.post("/app/login", data={"password": PASSWORD, "next": "/app"}, headers=ORIGIN)
        assert r.status_code == 303
        assert "Lunchbox" in http.get("/app?date=2026-10-07").text
    rows = conn.execute("SELECT user_id FROM web_sessions ORDER BY created_at").fetchall()
    assert sorted(r[0] for r in rows) == [1, 2]


def test_wrong_password_is_still_refused(public_settings, conn):
    add_her(conn)
    with client_for(public_settings) as http:
        r = http.post("/app/login", data={"password": "not anyones pw"}, headers=ORIGIN)
        assert r.status_code == 401


def test_export_covers_one_users_log(settings, conn):
    from chomp import export

    her = add_her(conn)
    eggs = add_food(conn, "Egg", *EGGS)
    store.log_meal(conn, [(eggs, 100)], WHEN, "mine")
    with as_user(settings, her) as hers:
        store.log_meal(hers, [(eggs, 30)], WHEN, "hers")
        data = export.to_json(hers, settings.tz)
    assert data["user"] == "Sam"
    assert [m["label"] for m in data["meals"]] == ["hers"]
    assert [m["label"] for m in export.to_json(conn, settings.tz)["meals"]] == ["mine"]
