"""Health data from the phone (#94, auth decided on #101).

Apple Health has no web API, so an iPhone app (Health Auto Export's REST automation, or an
iOS Shortcut) posts readings to POST /ingest/health. Each person has their own ingest token,
made with `chomp user ingest-token NAME`; it is stored as a SHA-256 hash, can only add that
person's health readings, and is refused everywhere else (as OAuth tokens are refused here).

Two payload shapes are accepted:

    Health Auto Export:  {"data": {"metrics": [{"name": "weight_&_body_mass", "units": "lb",
                           "data": [{"date": "2026-10-13 07:02:11 -0400", "qty": 182.4}]}]}}
    Simple (Shortcuts):  {"weight": [{"date": "2026-10-13T07:02:11-04:00", "value": 182.4,
                           "unit": "lb"}]}

Readings are de-duplicated by person and time, so the phone may resend overlapping windows.
Metrics Chomp doesn't keep yet are counted and ignored.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from chomp import db, logic, store
from chomp.config import Settings

log = logging.getLogger("chomp.health")

PATH = "/ingest/health"
TOKEN_PREFIX = "chomp_ingest_"
MAX_BODY = 10 * 1024 * 1024  # a long history backfill is still well under this
MAX_PROBLEMS = 5
# Health Auto Export calls weight "weight_&_body_mass"; names are compared without punctuation
WEIGHT_METRICS = {"weight_body_mass", "weight", "body_mass"}
HAE_DATE = "%Y-%m-%d %H:%M:%S %z"


# --- Tokens ---------------------------------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(conn: sqlite3.Connection, user_id: int) -> str:
    """Make a person's ingest token, replacing any earlier one. Shown once, stored hashed."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO ingest_tokens (user_id, token_hash) VALUES (?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET token_hash = excluded.token_hash,"
            " created_at = datetime('now'), last_used_at = NULL",
            (user_id, _hash(token)),
        )
    return token


def revoke_token(conn: sqlite3.Connection, user_id: int) -> bool:
    with db.transaction(conn):
        return conn.execute("DELETE FROM ingest_tokens WHERE user_id = ?", (user_id,)).rowcount > 0


def user_of_token(conn: sqlite3.Connection, token: str) -> int | None:
    """The person an ingest token belongs to, or None. Marks it used."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    digest = _hash(token)
    # Looked up by hash, so timing reveals nothing about the token itself
    row = conn.execute(
        "SELECT user_id FROM ingest_tokens WHERE token_hash = ?", (digest,)
    ).fetchone()
    if row is None:
        return None
    with db.transaction(conn):
        conn.execute(
            "UPDATE ingest_tokens SET last_used_at = datetime('now') WHERE user_id = ?",
            (row["user_id"],),
        )
    return row["user_id"]


def token_from(request: Request) -> str:
    """The ingest token from `Authorization: Bearer`, or X-Chomp-Token for apps that can't
    set Authorization. Never from the URL, which proxies log."""
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return request.headers.get("x-chomp-token", "").strip()


# --- Payloads -------------------------------------------------------------------------------


class PayloadError(ValueError):
    pass


@dataclass
class Parsed:
    weights: dict[str, float] = field(default_factory=dict)  # UTC measured_at -> kg
    rejected: int = 0
    problems: list[str] = field(default_factory=list)
    ignored: dict[str, int] = field(default_factory=dict)  # metric name -> readings

    def reject(self, problem: str) -> None:
        self.rejected += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(problem)


def parse_date(value: Any, tz: str) -> str:
    """A reading's time to storage format: Health Auto Export's "2026-10-13 07:02:11 -0400",
    or ISO 8601 (local in `tz` when it has no offset)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing date: {value!r}")
    try:
        return logic.format_utc(datetime.strptime(value.strip(), HAE_DATE))
    except ValueError:
        return logic.parse_time(value, tz, field="date")


def metric_key(name: str) -> str:
    """A metric name compared loosely: "weight_&_body_mass" and "Weight Body Mass" are both
    weight_body_mass."""
    return "_".join(re.findall(r"[a-z0-9]+", name.lower()))


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"not a number: {value!r}")
    return float(value)


def _weight(parsed: Parsed, item: Any, unit: Any, tz: str) -> None:
    try:
        if not isinstance(item, dict):
            raise ValueError(f"not a reading: {item!r}")
        unit = item.get("unit", item.get("units", unit))
        if not isinstance(unit, str):
            raise ValueError("missing unit (lb or kg)")
        value = item.get("qty", item.get("value"))
        kg = logic.to_kg(_number(value), unit)
        parsed.weights[parse_date(item.get("date"), tz)] = kg
    except ValueError as e:
        parsed.reject(f"weight: {e}")


def _items(value: Any) -> list:
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, dict) else []


def parse(payload: Any, tz: str) -> Parsed:
    """Read the weigh-ins (and count anything else) from either payload shape."""
    if not isinstance(payload, dict):
        raise PayloadError("expected a JSON object")
    parsed = Parsed()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    metrics = data.get("metrics")
    if isinstance(metrics, list):  # Health Auto Export
        for metric in metrics:
            if not isinstance(metric, dict):
                parsed.reject("metric: not an object")
                continue
            name = str(metric.get("name", "")).strip()
            readings = _items(metric.get("data"))
            if metric_key(name) in WEIGHT_METRICS:
                for item in readings:
                    _weight(parsed, item, metric.get("units"), tz)
            else:
                parsed.ignored[name or "?"] = parsed.ignored.get(name or "?", 0) + len(readings)
        for other in ("workouts", "stateOfMind", "ecg", "symptoms"):
            if items := _items(data.get(other)):
                parsed.ignored[other] = parsed.ignored.get(other, 0) + len(items)
    elif "weight" in payload:  # simple
        for item in _items(payload["weight"]):
            _weight(parsed, item, None, tz)
    else:
        raise PayloadError("no readings found: expected data.metrics or weight")
    return parsed


# --- Route ----------------------------------------------------------------------------------


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _body(request: Request) -> bytes | None:
    """The request body, or None once it passes MAX_BODY."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def add_routes(server: MCPServer, settings: Settings) -> None:
    @server.custom_route(PATH, methods=["POST"])
    async def ingest(request: Request) -> JSONResponse:
        token = token_from(request)
        with closing(db.connect(settings.db_path)) as conn:
            user_id = user_of_token(conn, token) if token else None
        if user_id is None:
            return _error("unauthorized", 401)
        body = await _body(request)
        if body is None:
            return _error(f"body over {MAX_BODY // (1024 * 1024)} MB", 413)
        try:
            parsed = parse(json.loads(body), settings.tz)
        except (ValueError, PayloadError) as e:  # JSONDecodeError is a ValueError
            return _error(str(e) if isinstance(e, PayloadError) else "body is not JSON", 400)
        with closing(db.connect(settings.db_path, user_id)) as conn:
            counts = store.add_weights(conn, parsed.weights.items(), "health")
        log.info(
            "ingest for user %d: weights %d added, %d updated, %d unchanged; %d rejected",
            user_id,
            counts.added,
            counts.updated,
            counts.unchanged,
            parsed.rejected,
        )
        return JSONResponse(
            {
                "weights": {
                    "added": counts.added,
                    "updated": counts.updated,
                    "unchanged": counts.unchanged,
                },
                "rejected": parsed.rejected,
                "problems": parsed.problems,
                "ignored": parsed.ignored,
            }
        )
