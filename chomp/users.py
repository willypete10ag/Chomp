"""The people who keep a log in Chomp (#46).

User 1 is the owner: everything logged before users existed is theirs, and they sign in with
AUTH_PASSWORD from the environment. Everyone else is added with `chomp user add` and gets
their own password, stored as a salted scrypt hash. A password alone says who is signing in,
so no two users may share one; the consent and dashboard sign-in pages stay a single box.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3

from chomp.db import transaction

OWNER_ID = 1
MIN_PASSWORD_LEN = 12
NAME_MAX = 40
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
    """Every user with how many meals they have logged."""
    return conn.execute(
        "SELECT u.id, u.name, u.created_at,"
        " (SELECT count(*) FROM meals m WHERE m.user_id = u.id) AS meals"
        " FROM users u ORDER BY u.id"
    ).fetchall()


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
