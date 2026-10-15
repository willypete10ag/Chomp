"""Health data from the phone (#94, auth decided on #101).

Apple Health has no web API, so an iPhone app (Health Auto Export's REST automation, or an
iOS Shortcut) posts readings to POST /ingest/health. Each person has their own ingest token,
made with `chomp user ingest-token NAME`; it is stored as a SHA-256 hash, can only add that
person's health readings, and is refused everywhere else (as OAuth tokens are refused here).

These payload shapes are accepted:

    Health Auto Export:  {"data": {"metrics": [{"name": "weight_&_body_mass", "units": "lb",
                           "data": [{"date": "2026-10-13 07:02:11 -0400", "qty": 182.4}]}]}}
    Simple (Shortcuts):  {"weight": [{"date": "2026-10-13T07:02:11-04:00", "value": 182.4,
                           "unit": "lb"}], "steps": [{"date": "2026-10-13", "value": 8123}]}
    Flat (one Shortcut): {"date": "2026-10-13", "steps": 8123, "active_kcal": 412,
                           "distance_mi": 3.8, "weight_lb": 182.4,
                           "weight_date": "2026-10-13T07:02:11-04:00"}
                         (a flat field may also be the Health samples as text, one per line:
                           "9,898 count\n0 count" adds up to 9898 steps)

Weight is kept per weigh-in. Activity (steps, active and resting energy, walking + running
distance, exercise minutes, flights) is kept per time bucket: Health Auto Export with Summarize
Data sends one reading per day (or hour) of the metric, and a simple date with no time is that
day's total. Readings are de-duplicated by person, metric, bucket size (a day or part of
one) and time, so the phone may resend overlapping windows. Metrics Chomp doesn't keep are
counted and ignored.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
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
MAX_ECHO = 80
# Health Auto Export calls weight "weight_&_body_mass"; names are compared without punctuation
WEIGHT_METRICS = {"weight_body_mass", "weight", "body_mass"}
# Health Auto Export's names (and the simple shape's keys) for the activity Chomp keeps
ACTIVITY_METRICS = {
    "step_count": "steps",
    "steps": "steps",
    "active_energy": "active_kcal",
    "active_energy_burned": "active_kcal",
    "basal_energy_burned": "resting_kcal",
    "resting_energy": "resting_kcal",
    "walking_running_distance": "distance_m",
    "distance_walking_running": "distance_m",
    "distance": "distance_m",
    "apple_exercise_time": "exercise_min",
    "exercise_time": "exercise_min",
    "exercise_minutes": "exercise_min",
    "flights_climbed": "flights",
    "flights": "flights",
}
# A Shortcut's flat fields (README, step 9): the day's totals so far for "date", and the
# latest weigh-in taken at "weight_date"
FLAT_ACTIVITY = {
    "steps": ("steps", None),
    "flights": ("flights", None),
    "active_kcal": ("active_kcal", "kcal"),
    "resting_kcal": ("resting_kcal", "kcal"),
    "exercise_min": ("exercise_min", "min"),
    "distance_mi": ("distance_m", "mi"),
    "distance_km": ("distance_m", "km"),
}
FLAT_WEIGHT = {"weight_lb": "lb", "weight_kg": "kg"}
HAE_DATE = "%Y-%m-%d %H:%M:%S %z"
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


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
    # (metric, "day" or "part", UTC start of its bucket) -> amount in the stored unit
    activity: dict[tuple[str, str, str], float] = field(default_factory=dict)
    rejected: int = 0
    problems: list[str] = field(default_factory=list)
    ignored: dict[str, int] = field(default_factory=dict)  # metric name -> readings
    # A Shortcut's flat fields as they arrived, echoed back so a setup mistake shows
    received: dict[str, Any] = field(default_factory=dict)

    def reject(self, problem: str) -> None:
        self.rejected += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(problem)


def parse_date(value: Any, tz: str, *, day_ok: bool = False) -> str:
    """A reading's time to storage format: Health Auto Export's "2026-10-13 07:02:11 -0400",
    or ISO 8601 (local in `tz` when it has no offset). With day_ok, a bare date is the start
    of that local day (a day's total)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing date: {value!r}")
    text = value.strip()
    try:
        return logic.format_utc(datetime.strptime(text, HAE_DATE))
    except ValueError:
        pass
    if day_ok and DAY_RE.fullmatch(text):
        return logic.local_day_bounds(date.fromisoformat(text), tz)[0]
    return logic.parse_time(text, tz, field="date")


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


def _activity(parsed: Parsed, metric: str, items: list, unit: Any, tz: str) -> None:
    """One metric's readings. A bare date is a whole day; timed readings are whole days when
    every one of them starts at local midnight (Health Auto Export grouped by day), else
    parts of days (grouped by hour)."""
    timed: list[tuple[str, float]] = []
    for item in items:
        try:
            if not isinstance(item, dict):
                raise ValueError(f"not a reading: {item!r}")
            item_unit = item.get("unit", item.get("units", unit))
            if item_unit is not None and not isinstance(item_unit, str):
                raise ValueError(f"unknown unit {item_unit!r}")
            qty = logic.to_activity(metric, _number(item.get("qty", item.get("value"))), item_unit)
            date_value = item.get("date")
            start = parse_date(date_value, tz, day_ok=True)
        except ValueError as e:
            parsed.reject(f"{metric}: {e}")
            continue
        if isinstance(date_value, str) and DAY_RE.fullmatch(date_value.strip()):
            parsed.activity[(metric, "day", start)] = qty
        else:
            timed.append((start, qty))
    period = "day" if all(logic.is_local_midnight(t, tz) for t, _ in timed) else "part"
    for start, qty in timed:
        parsed.activity[(metric, period, start)] = qty


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


# One line of a Shortcut's text: "9,898", "9898 count", "412.5 kcal", "182.4 lb", or the
# phone region's way of writing numbers: "9.898", "9 898", "412,5"
FLAT_LINE = re.compile(r"(\d[\d,.' \u00a0\u202f]*|[.,]\d+)\s*([a-z]*)", re.IGNORECASE)
GROUPED = re.compile(r"[1-9]\d{0,2}(?:([,.' \u00a0\u202f])\d{3})(?:\1\d{3})*")


def _region_number(text: str) -> float:
    """A number as the phone's region writes it. With both "," and ".", the last is the
    decimal mark; a lone mark is digit grouping only where it splits off groups of three
    after a leading 1-9 ("9.898" steps, "1,234"), else the decimal mark ("412,5", "182.4",
    "0.250"). Health shows at most two decimals, so three after the mark is grouping."""
    text = text.strip()
    if GROUPED.fullmatch(text):
        return float(re.sub(r"\D", "", text))
    text = re.sub(r"[' \u00a0\u202f]", "", text)
    if "," in text and "." in text:
        mark = max(",.", key=text.rfind)
        whole, _, fraction = text.rpartition(mark)
        text = re.sub(r"\D", "", whole) + "." + fraction
    elif text.count(",") == 1 or text.count(".") == 1:
        text = text.replace(",", ".")
    if not re.fullmatch(r"\d*\.?\d+", text):
        raise ValueError(f"not a number: {text!r}")
    return float(text)


def _flat_amount(value: Any, *, first: bool) -> dict[str, Any]:
    """A flat field's amount, and its unit when the text names one. A Shortcut that inserts
    Health samples as they are sends text with one sample per line ("9898 count", and "0
    count" for tomorrow when grouped by day): activity lines are added up, and for weight
    (latest first) the first line is the reading."""
    if not isinstance(value, str):
        return {"value": value}
    total, units = 0.0, set()
    for line in filter(None, (part.strip() for part in value.splitlines())):
        match = FLAT_LINE.fullmatch(line)
        if match is None:
            raise ValueError(f"not a number: {line[:40]!r}")
        total += _region_number(match[1])
        if match[2]:
            units.add(match[2])
        if first:
            break
    if len(units) > 1:
        raise ValueError(f"mixed units: {', '.join(sorted(units))}")
    return {"value": total} | ({"unit": units.pop()} if units else {})


def _items(value: Any) -> list:
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, dict) else []


def parse(payload: Any, tz: str) -> Parsed:
    """Read the weigh-ins and activity (and count anything else) from either payload
    shape."""
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
            key = metric_key(name)
            if key in WEIGHT_METRICS:
                for item in readings:
                    _weight(parsed, item, metric.get("units"), tz)
            elif key in ACTIVITY_METRICS:
                _activity(parsed, ACTIVITY_METRICS[key], readings, metric.get("units"), tz)
            else:
                parsed.ignored[name or "?"] = parsed.ignored.get(name or "?", 0) + len(readings)
        for other in ("workouts", "stateOfMind", "ecg", "symptoms"):
            if items := _items(data.get(other)):
                parsed.ignored[other] = parsed.ignored.get(other, 0) + len(items)
    else:  # simple: lists of readings, or a Shortcut's flat fields
        found = False
        for name, value in payload.items():
            key = metric_key(str(name))
            flat = key in FLAT_ACTIVITY or key in FLAT_WEIGHT
            if isinstance(value, list) and flat and not any(isinstance(v, dict) for v in value):
                value = "\n".join(str(v) for v in value)  # a Number field given a list
            if (flat or key in ("date", "weight_date")) and not isinstance(value, dict | list):
                parsed.received[str(name)] = value[:MAX_ECHO] if isinstance(value, str) else value
            if isinstance(value, dict | list):
                if key in WEIGHT_METRICS:
                    for item in _items(value):
                        _weight(parsed, item, None, tz)
                elif key in ACTIVITY_METRICS:
                    _activity(parsed, ACTIVITY_METRICS[key], _items(value), None, tz)
                else:
                    continue
            elif flat:
                if _blank(value):  # a Shortcut sends "" when Health has none today
                    found = True
                    continue
                if key in FLAT_WEIGHT:
                    item = {"date": payload.get("weight_date"), "unit": FLAT_WEIGHT[key]}
                    try:
                        item |= _flat_amount(value, first=True)
                    except ValueError as e:
                        parsed.reject(f"weight: {e}")
                    else:
                        _weight(parsed, item, None, tz)
                else:
                    metric, unit = FLAT_ACTIVITY[key]
                    item = {"date": payload.get("date"), "unit": unit}
                    try:
                        item |= _flat_amount(value, first=False)
                    except ValueError as e:
                        parsed.reject(f"{metric}: {e}")
                    else:
                        _activity(parsed, metric, [item], None, tz)
            else:
                continue
            found = True
        if not found:
            raise PayloadError("no readings found: expected data.metrics, weight or steps")
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
            weights = store.add_weights(conn, parsed.weights.items(), "health")
            activity = store.add_activity(
                conn, ((*key, qty) for key, qty in parsed.activity.items())
            )
        log.info(
            "ingest for user %d: weights %d added, %d updated, %d unchanged;"
            " activity %d added, %d updated, %d unchanged; %d rejected",
            user_id,
            weights.added,
            weights.updated,
            weights.unchanged,
            activity.added,
            activity.updated,
            activity.unchanged,
            parsed.rejected,
        )
        return JSONResponse(
            {
                "weights": asdict(weights),
                "activity": asdict(activity),
                "rejected": parsed.rejected,
                "problems": parsed.problems,
                "ignored": parsed.ignored,
                "received": parsed.received,
            }
        )
