"""The people who keep a log in Chomp (#46).

User 1 is the owner: everything logged before users existed is theirs, and they sign in with
AUTH_PASSWORD from the environment. Everyone else is added by the owner, on the dashboard's
People page (#109) or with `chomp user add`, and has their own password, stored as a salted
scrypt hash. From the dashboard the owner never sees it: adding someone makes a one-time
invite link, and opening it lets them pick their password. A password alone says who is signing in,
so no two users may share one; the consent and dashboard sign-in pages stay a single box.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time

from chomp import store
from chomp.db import transaction

OWNER_ID = 1
MIN_PASSWORD_LEN = 12
NAME_MAX = 40
INVITE_TTL_S = 7 * 86400
INVITE_PREFIX = "chomp_invite_"
# scrypt cost: ~16 MiB and a few tens of ms per check, fine for a handful of users
_N, _R, _P = 2**14, 8, 1


class UserError(ValueError):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, key = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(key) // 2,
        )
    except ValueError:
        return False
    return hmac.compare_digest(got.hex(), key)


def find_by_password(conn: sqlite3.Connection, password: str, owner_password: str) -> int | None:
    """The id of the user whose password this is, or None."""
    if owner_password and hmac.compare_digest(password.encode(), owner_password.encode()):
        return OWNER_ID
    rows = conn.execute(
        "SELECT id, password_hash FROM users WHERE password_hash IS NOT NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        if verify_password(password, row["password_hash"]):
            return row["id"]
    return None


def password_clash(conn: sqlite3.Connection, owner_password: str) -> str | None:
    """The name of a user whose password is also AUTH_PASSWORD, if any. That password would
    sign them in as the owner, so the server refuses to start until one of them changes."""
    rows = conn.execute(
        "SELECT name, password_hash FROM users WHERE password_hash IS NOT NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        if owner_password and verify_password(owner_password, row["password_hash"]):
            return row["name"]
    return None


def name_of(conn: sqlite3.Connection, user_id: int) -> str | None:
    row = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["name"] if row else None


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every user with how many meals they have logged, whether they have a password yet,
    and when their open invite (if any) expires."""
    return conn.execute(
        "SELECT u.id, u.name, u.created_at, u.password_hash IS NOT NULL AS has_password,"
        " (SELECT count(*) FROM meals m WHERE m.user_id = u.id) AS meals,"
        " (SELECT expires_at FROM user_invites i WHERE i.user_id = u.id) AS invite_expires"
        " FROM users u ORDER BY u.id"
    ).fetchall()


def get(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return next((r for r in list_users(conn) if r["id"] == user_id), None)


def get_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, name FROM users WHERE name = ?", (name.strip(),)).fetchone()
    if row is None:
        known = ", ".join(r["name"] for r in list_users(conn))
        raise UserError(f"no user named {name!r} (users: {known})")
    return row


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name or len(name) > NAME_MAX:
        raise UserError(f"a name is 1 to {NAME_MAX} characters")
    return name


def _check_password(
    conn: sqlite3.Connection, password: str, owner_password: str, user_id: int | None
) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise UserError(f"a password needs at least {MIN_PASSWORD_LEN} characters")
    other = find_by_password(conn, password, owner_password)
    if other is not None and other != user_id:
        # Sign-in identifies people by password alone, so it must be unique
        raise UserError("someone else already uses that password; pick another")


def add_user(conn: sqlite3.Connection, name: str, password: str, owner_password: str) -> int:
    name = _clean_name(name)
    _check_password(conn, password, owner_password, None)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
            raise UserError(f"there is already a user named {name!r}")
        return conn.execute(
            "INSERT INTO users (name, password_hash) VALUES (?, ?)",
            (name, hash_password(password)),
        ).lastrowid


def set_password(
    conn: sqlite3.Connection, user_id: int, password: str, owner_password: str
) -> None:
    """Change a user's password and sign them out everywhere (dashboard and connectors)."""
    if user_id == OWNER_ID:
        raise UserError("the owner's password is AUTH_PASSWORD in .env; change it there")
    _check_password(conn, password, owner_password, user_id)
    with transaction(conn):
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id)
        )
        conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM oauth_tokens WHERE user_id = ?", (user_id,))


def rename(conn: sqlite3.Connection, user_id: int, name: str) -> None:
    name = _clean_name(name)
    with transaction(conn):
        clash = conn.execute(
            "SELECT 1 FROM users WHERE name = ? AND id != ?", (name, user_id)
        ).fetchone()
        if clash:
            raise UserError(f"there is already a user named {name!r}")
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))


def add_person(conn: sqlite3.Connection, name: str) -> int:
    """Add someone with no password yet (#109); an invite link lets them pick one."""
    name = _clean_name(name)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
            raise UserError(f"there is already a user named {name!r}")
        return conn.execute("INSERT INTO users (name) VALUES (?)", (name,)).lastrowid


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_invite(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> str:
    """A one-time invite token for someone (not the owner), replacing any earlier one. Their
    current password, if any, keeps working until the link is used."""
    if user_id == OWNER_ID:
        raise UserError("the owner's password is AUTH_PASSWORD in .env; change it there")
    token = INVITE_PREFIX + secrets.token_urlsafe(32)
    expires = int(now if now is not None else time.time()) + INVITE_TTL_S
    with transaction(conn):
        conn.execute(
            "INSERT INTO user_invites (user_id, token_hash, expires_at) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET token_hash = excluded.token_hash,"
            " expires_at = excluded.expires_at, created_at = datetime('now')",
            (user_id, _token_hash(token), expires),
        )
    return token


def invite_user(conn: sqlite3.Connection, token: str, now: float | None = None) -> int | None:
    """Whose invite this is, or None when it is unknown, used or expired."""
    if not token.startswith(INVITE_PREFIX):
        return None
    row = conn.execute(
        "SELECT user_id FROM user_invites WHERE token_hash = ? AND expires_at > ?",
        (_token_hash(token), int(now if now is not None else time.time())),
    ).fetchone()
    return row["user_id"] if row else None


def accept_invite(conn: sqlite3.Connection, token: str, password: str, owner_password: str) -> int:
    """Set the invited person's password and use up the link. Returns their id."""
    with transaction(conn):
        user_id = invite_user(conn, token)
        if user_id is None:
            raise UserError("this invite link has expired or was already used")
        set_password(conn, user_id, password, owner_password)
        conn.execute("DELETE FROM user_invites WHERE user_id = ?", (user_id,))
    return user_id


def remove_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Delete someone and their whole log (#109). Foods stay, being shared; their recipes'
    food rows are deleted like any food (gone from everyone's saved meals and portions)."""
    if user_id == OWNER_ID:
        raise UserError("the owner can't be removed")
    with transaction(conn):
        recipe_foods = [
            r["food_id"]
            for r in conn.execute(
                "SELECT food_id FROM recipes WHERE user_id = ? AND food_id IS NOT NULL",
                (user_id,),
            )
        ]
        conn.execute("DELETE FROM recipes WHERE user_id = ?", (user_id,))
        for food_id in recipe_foods:
            # As deleting the recipe would: out of everyone's saved meals and portions
            store.delete_food(conn, food_id)
        # These predate ON DELETE CASCADE on user_id (0006); the rest cascade from users
        for table in ("meals", "meal_templates", "targets"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    with transaction(conn):
        if value:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        else:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
