"""All macro math and day-boundary logic. Pure functions, no I/O.

Foods hold nutrients per 100 g. An entry's snapshot is the food's per-100 g values scaled
to the logged grams at log time, so later edits to the food never rewrite history.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CORE = ("kcal", "protein_g", "carbs_g", "fat_g")
OPTIONAL = ("fiber_g", "sugar_g", "sodium_mg")
NUTRIENTS = CORE + OPTIONAL

# Decimal places when showing values to a person (and to Claude).
DISPLAY_PLACES = {
    "kcal": 0,
    "protein_g": 1,
    "carbs_g": 1,
    "fat_g": 1,
    "fiber_g": 1,
    "sugar_g": 1,
    "sodium_mg": 0,
}
# Stored snapshots keep more precision so summed totals don't drift.
STORE_PLACES = 3

Nutrients = dict[str, float | None]

UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _check_amount(name: str, value: float) -> None:
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a finite number greater than 0, got {value!r}")


def _clean(values: Mapping[str, float | None]) -> Nutrients:
    """Validate a nutrient mapping: core values required, none negative, unknown keys rejected."""
    unknown = set(values) - set(NUTRIENTS)
    if unknown:
        raise ValueError(f"unknown nutrients: {sorted(unknown)}")
    out: Nutrients = {}
    for k in NUTRIENTS:
        v = values.get(k)
        if v is None:
            if k in CORE:
                raise ValueError(f"{k} is required")
            out[k] = None
            continue
        v = float(v)
        if not math.isfinite(v):
            raise ValueError(f"{k} must be a finite number, got {v}")
        if v < 0:
            raise ValueError(f"{k} cannot be negative, got {v}")
        out[k] = v
    return out


def per_serving_to_per_100g(per_serving: Mapping[str, float | None], serving_g: float) -> Nutrients:
    """Convert label values for one serving of `serving_g` grams to per-100 g values."""
    _check_amount("serving_g", serving_g)
    factor = 100.0 / serving_g
    return {
        k: None if v is None else round(v * factor, STORE_PLACES)
        for k, v in _clean(per_serving).items()
    }


def scale(per_100g: Mapping[str, float | None], grams: float) -> Nutrients:
    """Nutrients in `grams` of a food given its per-100 g values (unrounded)."""
    _check_amount("grams", grams)
    factor = grams / 100.0
    return {k: None if v is None else v * factor for k, v in _clean(per_100g).items()}


def snapshot(per_100g: Mapping[str, float | None], grams: float) -> Nutrients:
    """The values stored on an entry at log time."""
    scaled = scale(per_100g, grams)
    return {k: None if v is None else round(v, STORE_PLACES) for k, v in scaled.items()}


def sum_nutrients(rows: Iterable[Mapping[str, float | None]]) -> Nutrients:
    """Sum nutrient rows. An optional nutrient is None only if no row reports it."""
    totals: Nutrients = dict.fromkeys(NUTRIENTS, None)
    for row in rows:
        for k in NUTRIENTS:
            v = row.get(k)
            if v is not None:
                totals[k] = (totals[k] or 0.0) + v
    for k in CORE:
        if totals[k] is None:
            totals[k] = 0.0
    return totals


def round_nutrients(values: Mapping[str, float | None]) -> Nutrients:
    """Round to storage precision (used when caching upstream foods)."""
    return {k: None if v is None else round(v, STORE_PLACES) for k, v in _clean(values).items()}


def round_for_display(values: Mapping[str, float | None]) -> Nutrients:
    out: Nutrients = {}
    for k in NUTRIENTS:
        v = values.get(k)
        if v is None:
            out[k] = None
            continue
        out[k] = round(v, DISPLAY_PLACES[k])
    return out


def progress(totals: Mapping[str, float | None], targets: Mapping[str, float | None]) -> dict:
    """Per-nutrient progress against targets; nutrients without a target are left out."""
    out = {}
    for k in CORE:
        target = targets.get(k)
        if target is None:
            continue
        eaten = totals.get(k) or 0.0
        out[k] = {
            "target": target,
            "eaten": eaten,
            "remaining": target - eaten,
            "percent": round(100.0 * eaten / target, 0) if target else None,
        }
    return out


# --- Time -----------------------------------------------------------------------------------


def format_utc(dt: datetime) -> str:
    """Canonical storage format for timestamps: UTC, second precision, trailing Z."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime; attach a timezone first")
    return dt.astimezone(UTC).strftime(UTC_FORMAT)


def parse_utc(s: str) -> datetime:
    return datetime.strptime(s, UTC_FORMAT).replace(tzinfo=UTC)


def local_day_bounds(day: date, tz: str) -> tuple[str, str]:
    """UTC [start, end) for a local calendar day, correct across DST changes.

    Returned as storage-format strings so they compare directly with meals.eaten_at.
    """
    zone = ZoneInfo(tz)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return format_utc(start), format_utc(end)


def local_date(utc: str, tz: str) -> date:
    """The local calendar date a stored UTC timestamp falls on."""
    return parse_utc(utc).astimezone(ZoneInfo(tz)).date()


def local_iso(utc: str, tz: str) -> str:
    """A stored UTC timestamp as local ISO 8601 with offset, e.g. 2026-10-05T08:12:00-04:00."""
    return parse_utc(utc).astimezone(ZoneInfo(tz)).isoformat()


def parse_eaten_at(value: str | None, tz: str, now: datetime | None = None) -> str:
    """Normalize a meal time to storage format.

    None means now. An ISO 8601 string with an offset (or Z) is taken as is; one without an
    offset is local time in `tz`. A bare date is rejected so a meal never silently lands at
    midnight.
    """
    if value is None or not value.strip():
        return format_utc(now or datetime.now(UTC))
    text = value.strip()
    if len(text) <= 10:
        raise ValueError(f"eaten_at needs a time as well as a date, got {value!r}")
    try:
        dt = datetime.fromisoformat(text.replace("z", "Z"))
    except ValueError as e:
        raise ValueError(f"eaten_at must be ISO 8601 like 2026-10-05T08:30, got {value!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return format_utc(dt)


def parse_local_date(value: str | None, tz: str, now: datetime | None = None) -> date:
    """A local calendar date from YYYY-MM-DD, 'today', 'yesterday', or None (today)."""
    today = (now or datetime.now(UTC)).astimezone(ZoneInfo(tz)).date()
    text = (value or "today").strip().lower()
    if text == "today":
        return today
    if text == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError as e:
        raise ValueError(f"date must be YYYY-MM-DD, 'today' or 'yesterday', got {value!r}") from e


def date_range(start: date, end: date) -> list[date]:
    """Every local date from start to end, inclusive."""
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def average(totals: Mapping[str, float | None], days: int) -> Nutrients:
    """Per-day average of summed nutrients over `days` days."""
    if days <= 0:
        raise ValueError("days must be positive")
    return {k: None if (v := totals.get(k)) is None else v / days for k in NUTRIENTS}


# --- Upstream data --------------------------------------------------------------------------

KJ_PER_KCAL = 4.184
# Label rounding makes small values disagree a lot in relative terms, so differences below
# these absolute amounts never count as a mismatch.
MISMATCH_FLOOR = {"kcal": 10.0, "protein_g": 1.0, "carbs_g": 1.0, "fat_g": 1.0}


def kj_to_kcal(kj: float) -> float:
    return kj / KJ_PER_KCAL


def g_to_mg(grams: float) -> float:
    return grams * 1000.0


def label_mismatch(
    per_100g: Mapping[str, float | None],
    label_per_100g: Mapping[str, float | None],
    tolerance: float = 0.10,
) -> list[str]:
    """Core nutrients where database per-100 g values and label-derived per-100 g values
    differ by more than `tolerance` (relative to the larger value) and by more than the
    absolute floor. Nutrients missing from either side are skipped."""
    out = []
    for k in CORE:
        a, b = per_100g.get(k), label_per_100g.get(k)
        if a is None or b is None:
            continue
        diff = abs(a - b)
        if diff > MISMATCH_FLOOR[k] and diff > tolerance * max(abs(a), abs(b)):
            out.append(k)
    return out
