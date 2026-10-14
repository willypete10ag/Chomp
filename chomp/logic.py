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
# Label details under fat and carbs (#63): fat subtypes, cholesterol, added sugars and sugar
# alcohols. Optional like fiber, sugar and sodium; the unit is in the name.
DETAILS = (
    "sat_fat_g",
    "trans_fat_g",
    "poly_fat_g",
    "mono_fat_g",
    "cholesterol_mg",
    "added_sugar_g",
    "sugar_alcohol_g",
)
# Vitamins and minerals (#53), in the order US labels list them. The unit is in the name and
# matches the label: mg or mcg (vitamin A as RAE, folate as DFE).
MICROS = (
    "vitamin_d_mcg",
    "calcium_mg",
    "iron_mg",
    "potassium_mg",
    "vitamin_a_mcg",
    "vitamin_c_mg",
    "vitamin_e_mg",
    "vitamin_k_mcg",
    "thiamin_mg",
    "riboflavin_mg",
    "niacin_mg",
    "vitamin_b6_mg",
    "folate_mcg",
    "vitamin_b12_mcg",
    "phosphorus_mg",
    "magnesium_mg",
    "zinc_mg",
)
NUTRIENTS = CORE + OPTIONAL + DETAILS + MICROS

# FDA Daily Values for adults and children 4 and over (21 CFR 101.9), in each nutrient's
# unit. Used to read '% DV' off a label and to show a day's totals as a share of the DV.
DAILY_VALUES = {
    "vitamin_d_mcg": 20.0,
    "calcium_mg": 1300.0,
    "iron_mg": 18.0,
    "potassium_mg": 4700.0,
    "vitamin_a_mcg": 900.0,
    "vitamin_c_mg": 90.0,
    "vitamin_e_mg": 15.0,
    "vitamin_k_mcg": 120.0,
    "thiamin_mg": 1.2,
    "riboflavin_mg": 1.3,
    "niacin_mg": 16.0,
    "vitamin_b6_mg": 1.7,
    "folate_mcg": 400.0,
    "vitamin_b12_mcg": 2.4,
    "phosphorus_mg": 1250.0,
    "magnesium_mg": 420.0,
    "zinc_mg": 11.0,
}

# Decimal places when showing values to a person (and to Claude).
DISPLAY_PLACES = {
    "kcal": 0,
    "protein_g": 1,
    "carbs_g": 1,
    "fat_g": 1,
    "fiber_g": 1,
    "sugar_g": 1,
    "sodium_mg": 0,
    "sat_fat_g": 1,
    "trans_fat_g": 1,
    "poly_fat_g": 1,
    "mono_fat_g": 1,
    "cholesterol_mg": 0,
    "added_sugar_g": 1,
    "sugar_alcohol_g": 1,
    "vitamin_d_mcg": 1,
    "calcium_mg": 0,
    "iron_mg": 1,
    "potassium_mg": 0,
    "vitamin_a_mcg": 0,
    "vitamin_c_mg": 1,
    "vitamin_e_mg": 1,
    "vitamin_k_mcg": 1,
    "thiamin_mg": 2,
    "riboflavin_mg": 2,
    "niacin_mg": 1,
    "vitamin_b6_mg": 2,
    "folate_mcg": 0,
    "vitamin_b12_mcg": 2,
    "phosphorus_mg": 0,
    "magnesium_mg": 0,
    "zinc_mg": 1,
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


# FDC computes carbohydrate "by difference" (100 g minus water, protein, fat, ash), which
# can land slightly below zero; anything further below zero is bad data.
UPSTREAM_NEGATIVE_FLOOR = -1.0


def tidy_upstream(values: Mapping[str, float | None]) -> Nutrients | None:
    """Upstream per-100 g values with slightly negative carbs set to 0, or None if a value
    is unusable (not finite, or negative anywhere else)."""
    out: Nutrients = {}
    for k in NUTRIENTS:
        v = values.get(k)
        if v is not None:
            floor = UPSTREAM_NEGATIVE_FLOOR if k == "carbs_g" else 0.0
            if not math.isfinite(v) or v < floor:
                return None
            v = max(v, 0.0)
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


def partial_per_serving_to_per_100g(
    per_serving: Mapping[str, float], serving_g: float
) -> dict[str, float]:
    """Like per_serving_to_per_100g, for a subset of nutrients (e.g. label corrections)."""
    _check_amount("serving_g", serving_g)
    out = {}
    for k, v in per_serving.items():
        if k not in NUTRIENTS:
            raise ValueError(f"unknown nutrient {k}")
        v = float(v)
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"{k} must be a finite number of at least 0, got {v}")
        out[k] = v * 100.0 / serving_g
    return out


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


def recipe_nutrition(
    ingredients: Iterable[tuple[Mapping[str, float | None], float]], servings: float
) -> dict:
    """Nutrition of a recipe (#82) from its ingredients' per-100 g values and grams.

    Returns `total` (whole recipe), `per_serving`, `per_100g` (of the raw ingredient weight,
    for the food row that logs it), `total_g` and `serving_g`. An optional nutrient is kept
    only when every ingredient reports it, so a recipe never claims a partial sum as its
    whole amount.
    """
    _check_amount("servings", servings)
    items = [(_clean(n), g) for n, g in ingredients]
    if not items:
        raise ValueError("a recipe needs at least one ingredient")
    snaps = [scale(n, g) for n, g in items]
    total_g = sum(g for _, g in items)
    total = sum_nutrients(snaps)
    for k in NUTRIENTS:
        if k not in CORE and any(s[k] is None for s in snaps):
            total[k] = None
    per_serving = {k: None if v is None else v / servings for k, v in total.items()}
    per_100g = {
        k: None if v is None else round(v * 100.0 / total_g, STORE_PLACES) for k, v in total.items()
    }
    return {
        "total": total,
        "per_serving": per_serving,
        "per_100g": per_100g,
        "total_g": total_g,
        "serving_g": total_g / servings,
    }


def partial_micros(rows: Iterable[Mapping[str, float | None]]) -> list[str]:
    """Vitamins and minerals that some rows report and others don't, so their sum covers
    only part of what was eaten (a floor, not the whole amount)."""
    rows = list(rows)
    out = []
    for k in MICROS:
        known = sum(1 for r in rows if r.get(k) is not None)
        if 0 < known < len(rows):
            out.append(k)
    return out


def percent_dv(key: str, value: float | None) -> float | None:
    """A vitamin or mineral amount as a whole-number percent of its Daily Value."""
    if value is None or key not in DAILY_VALUES:
        return None
    return round(100.0 * value / DAILY_VALUES[key])


def percent_dv_to_amounts(percents: Mapping[str, float]) -> dict[str, float]:
    """Label '% DV' figures for vitamins and minerals as amounts in each nutrient's unit."""
    out = {}
    for k, pct in percents.items():
        if k not in DAILY_VALUES:
            raise ValueError(f"no Daily Value for {k}; give a vitamin or mineral")
        pct = float(pct)
        if not math.isfinite(pct) or pct < 0:
            raise ValueError(f"{k} percent must be a finite number of at least 0, got {pct}")
        out[k] = pct * DAILY_VALUES[k] / 100.0
    return out


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


# Atwater factors, kcal per gram. Only used to split calories between protein, carbs and fat
# (targets, and a day's breakdown); logged calories always come from the food data.
KCAL_PER_G = {"protein_g": 4.0, "carbs_g": 4.0, "fat_g": 9.0}


def macro_kcal(targets: Mapping[str, float | None]) -> dict[str, float] | None:
    """Calories implied by protein, carbs and fat targets: each macro's share and the total.

    None unless all three targets are set.
    """
    if any(targets.get(k) is None for k in KCAL_PER_G):
        return None
    parts = {k: float(targets[k]) * f for k, f in KCAL_PER_G.items()}
    return {**parts, "total": sum(parts.values())}


# Daily goals: calories when none is set yet, and the smallest share of them a macro can have
# on the goals page's calorie bar
DEFAULT_KCAL = 2000.0
MIN_SHARE = 0.05


def fit_macros(kcal: float, shares: Mapping[str, float] | None = None) -> dict[str, float]:
    """Whole grams of protein, carbs and fat that split `kcal` by `shares` (each macro's
    fraction of the calories; even thirds when None). Protein and fat round to the gram and
    carbs take what's left, so the three come to `kcal` within a couple of kcal."""
    if shares is None:
        shares = dict.fromkeys(KCAL_PER_G, 1 / 3)
    protein = round(kcal * shares["protein_g"] / KCAL_PER_G["protein_g"])
    fat = round(kcal * shares["fat_g"] / KCAL_PER_G["fat_g"])
    rest = kcal - protein * KCAL_PER_G["protein_g"] - fat * KCAL_PER_G["fat_g"]
    carbs = max(round(rest / KCAL_PER_G["carbs_g"]), 0)
    return {"protein_g": float(protein), "carbs_g": float(carbs), "fat_g": float(fat)}


def with_default_targets(targets: Mapping[str, float | None]) -> dict[str, float]:
    """The daily goals progress is measured against: a goal that isn't set counts as the
    default, 2,000 kcal and a third of the calorie goal for each of protein, carbs and fat."""
    kcal = targets.get("kcal") or DEFAULT_KCAL
    fitted = fit_macros(kcal)
    out = {"kcal": float(kcal)}
    for k in KCAL_PER_G:
        v = targets.get(k)
        out[k] = float(v) if v is not None else fitted[k]
    return out


# A star on the calendar (#71): calories, protein, carbs and fat each within this share of
# their goals
STAR_WITHIN = 0.10


def near_goals(totals: Mapping[str, float | None], targets: Mapping[str, float | None]) -> bool:
    """True when calories and each macro land within STAR_WITHIN of their goals. Goals not
    set count as the defaults (with_default_targets), as on Today."""
    goals = with_default_targets(targets)
    for k in CORE:
        goal = goals[k]
        eaten = totals.get(k) or 0.0
        if not goal or abs(eaten - goal) > STAR_WITHIN * goal:
            return False
    return True


def macro_split(totals: Mapping[str, float | None]) -> dict[str, float] | None:
    """Each macro's percent of the calories protein, carbs and fat give together (4/4/9 kcal
    per g). None when there is none of any."""
    parts = {k: (totals.get(k) or 0.0) * f for k, f in KCAL_PER_G.items()}
    total = sum(parts.values())
    if total <= 0:
        return None
    return {k: 100.0 * v / total for k, v in parts.items()}


def net_carbs(totals: Mapping[str, float | None]) -> float | None:
    """Carbs minus fiber and sugar alcohols (never below 0); None unless sugar alcohols are
    known, since then it's just carbs minus fiber."""
    if totals.get("sugar_alcohol_g") is None:
        return None
    rest = (totals.get("carbs_g") or 0.0) - (totals.get("fiber_g") or 0.0)
    return max(rest - totals["sugar_alcohol_g"], 0.0)


def top_foods(
    items: Iterable[tuple[int, Mapping[str, float | None]]], key: str, n: int = 3
) -> list[tuple[int, float]]:
    """The `n` foods that gave the most of `key`, as (food_id, amount), largest first. Entries
    of the same food are added together; foods that gave none are left out."""
    by_food: dict[int, float] = {}
    for food_id, values in items:
        if (v := values.get(key)) is not None:
            by_food[food_id] = by_food.get(food_id, 0.0) + v
    ranked = sorted(by_food.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(f, v) for f, v in ranked if v > 0][:n]


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

    "now" means now. An ISO 8601 string with an offset (or Z) is taken as is; one without an
    offset is local time in `tz`. A missing time is rejected so a meal is never stamped with
    whenever it happened to be logged, and a bare date so it never lands at midnight.
    """
    if value is None or not value.strip():
        raise ValueError(
            "eaten_at is required: ask the user what time they ate this, or pass 'now' if "
            "they are eating it now"
        )
    return parse_time(value, tz, now, "eaten_at")


def parse_time(value: str, tz: str, now: datetime | None = None, field: str = "time") -> str:
    """A time to storage format: "now", ISO 8601 with an offset (or Z), or local ISO 8601 in
    `tz`. A bare date is rejected so nothing lands at midnight by accident."""
    text = value.strip()
    if text.lower() == "now":
        return format_utc(now or datetime.now(UTC))
    if len(text) <= 10:
        raise ValueError(f"{field} needs a time as well as a date, got {value!r}")
    try:
        dt = datetime.fromisoformat(text.replace("z", "Z"))
    except ValueError as e:
        raise ValueError(f"{field} must be ISO 8601 like 2026-10-05T08:30, got {value!r}") from e
    if dt.tzinfo is None:
        zone = ZoneInfo(tz)
        local = dt.replace(tzinfo=zone)
        # replace() accepts wall times skipped by a spring-forward change and shifts them;
        # a round trip through UTC exposes that.
        if local.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != dt:
            raise ValueError(
                f"{field} {value!r} does not exist in {tz} (clocks skip that hour); "
                "ask the user for the time again"
            )
        dt = local
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


def totals_by_day(rows: Iterable[tuple[str, Mapping[str, float | None]]], tz: str) -> dict:
    """Sum (UTC eaten_at, nutrients) rows per local date. Days with no rows are absent."""
    by_day: dict[date, list] = {}
    for eaten_at, n in rows:
        by_day.setdefault(local_date(eaten_at, tz), []).append(n)
    return {d: sum_nutrients(ns) for d, ns in by_day.items()}


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


# Mass units as upstream sources spell them, in grams
_MASS_G = {"g": 1.0, "mg": 1e-3, "ug": 1e-6, "µg": 1e-6, "μg": 1e-6, "mcg": 1e-6}
_UNIT_OF = {"_mg": "mg", "_mcg": "mcg", "_g": "g"}
IU_PER_MCG_VITAMIN_D = 40.0


def unit_of(key: str) -> str:
    """The unit a nutrient is stored in, from its name ('iron_mg' -> 'mg')."""
    for suffix, unit in _UNIT_OF.items():
        if key.endswith(suffix):
            return unit
    return key  # kcal


def convert_amount(key: str, value: float, unit: str) -> float | None:
    """An upstream amount in `unit` converted to the unit `key` is stored in, or None when
    the unit can't be converted (e.g. vitamin A in IU, which depends on the food)."""
    unit = unit.strip().lower()
    if unit == "iu":
        return value / IU_PER_MCG_VITAMIN_D if key == "vitamin_d_mcg" else None
    if unit not in _MASS_G or key == "kcal":
        return None
    return value * _MASS_G[unit] / _MASS_G[unit_of(key)]


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


# --- Body weight (#94) ----------------------------------------------------------------------

KG_PER_LB = 0.45359237
WEIGHT_UNITS = {
    "kg": 1.0,
    "kgs": 1.0,
    "lb": KG_PER_LB,
    "lbs": KG_PER_LB,
    "pound": KG_PER_LB,
    "pounds": KG_PER_LB,
}
WEIGHT_MAX_KG = 700


def to_kg(value: float, unit: str) -> float:
    """A body weight in kg from kg or lb. Raises ValueError for other units or odd values."""
    factor = WEIGHT_UNITS.get(unit.strip().lower())
    if factor is None:
        raise ValueError(f"weight unit must be lb or kg, got {unit!r}")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"weight must be a positive number, got {value!r}")
    kg = value * factor
    if kg >= WEIGHT_MAX_KG:
        raise ValueError(f"{value} {unit} is not a body weight")
    return kg


def weight_out(kg: float) -> dict[str, float]:
    """A weight in both units, rounded for display: kg to 0.01, lb to 0.1."""
    return {"kg": round(kg, 2), "lb": round(kg / KG_PER_LB, 1)}


# --- Workouts (#94) -------------------------------------------------------------------------


def workout_volume_kg(sets: Iterable[Mapping]) -> float:
    """Total weight lifted: weight x reps over the working sets (warm-ups left out)."""
    return sum((s["weight_kg"] or 0) * (s["reps"] or 0) for s in sets if s["set_type"] != "warmup")


def best_set(sets: Iterable[Mapping]) -> Mapping | None:
    """The heaviest working set (most reps breaking a tie), or None without weighted sets."""
    weighted = [s for s in sets if s["set_type"] != "warmup" and s["weight_kg"] and s["reps"]]
    return max(weighted, key=lambda s: (s["weight_kg"], s["reps"]), default=None)


# --- Activity (#94) -------------------------------------------------------------------------

M_PER_MILE = 1609.344
_KCAL = {"kcal": 1.0, "cal": 1.0, "kj": 1 / KJ_PER_KCAL}
_COUNT = {"": 1.0, "count": 1.0}
# Stored unit per metric, and the units a phone may send it in
ACTIVITY_UNITS: dict[str, dict[str, float]] = {
    "steps": {**_COUNT, "steps": 1.0},
    "active_kcal": _KCAL,
    "resting_kcal": _KCAL,
    "distance_m": {"m": 1.0, "km": 1000.0, "mi": M_PER_MILE, "ft": 0.3048, "yd": 0.9144},
    "exercise_min": {"min": 1.0, "s": 1 / 60, "sec": 1 / 60, "h": 60.0, "hr": 60.0},
    "flights": {**_COUNT, "flights": 1.0},
}
ACTIVITY_METRICS = tuple(ACTIVITY_UNITS)
# Most one reading (a day at most) can hold; more is a misread or the wrong metric
ACTIVITY_MAX = {
    "steps": 200_000,
    "active_kcal": 20_000,
    "resting_kcal": 20_000,
    "distance_m": 500_000,
    "exercise_min": 1440,
    "flights": 5_000,
}


def to_activity(metric: str, value: float, unit: str | None) -> float:
    """An activity reading in the metric's stored unit (steps, kcal, metres, minutes,
    flights). Raises ValueError for unknown units or impossible values."""
    factor = ACTIVITY_UNITS[metric].get((unit or "").strip().lower())
    if factor is None:
        raise ValueError(f"unknown unit {unit!r}")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"not a usable amount: {value!r}")
    qty = value * factor
    if qty > ACTIVITY_MAX[metric]:
        raise ValueError(f"{value:g}{' ' + unit if unit else ''} is too much for one reading")
    return qty


def is_local_midnight(utc: str, tz: str) -> bool:
    return datetime.fromisoformat(local_iso(utc, tz)).time() == time()


def activity_by_day(rows: Iterable[Mapping], tz: str) -> dict[date, dict[str, float]]:
    """Each local day's total per metric from (metric, period, start_at, qty, updated_at)
    rows, in day order. A day's "part" buckets (hours) add up; a "day" reading is the whole
    day. When a day has both for a metric (the phone switched from one to the other), the
    kind sent most recently counts, a whole day winning a tie."""
    sums: dict[tuple[date, str, str], list] = {}  # (day, metric, period) -> [total, newest]
    for r in rows:
        key = (local_date(r["start_at"], tz), r["metric"], r["period"])
        total = sums.setdefault(key, [0.0, ""])
        total[0] += r["qty"]
        total[1] = max(total[1], r["updated_at"])
    out: dict[date, dict[str, float]] = {}
    for (day, metric, period), (qty, newest) in sorted(sums.items()):
        other = sums.get((day, metric, "part" if period == "day" else "day"))
        if other is not None and (other[1] > newest or (other[1] == newest and period == "part")):
            continue
        out.setdefault(day, {})[metric] = qty
    return out


def activity_average(days: Mapping[date, Mapping[str, float]]) -> dict[str, float]:
    """Each metric's average over the days that have it."""
    out = {}
    for metric in ACTIVITY_METRICS:
        values = [d[metric] for d in days.values() if metric in d]
        if values:
            out[metric] = sum(values) / len(values)
    return out


def burned_kcal(day: Mapping[str, float]) -> float | None:
    """Active plus resting energy, when the phone sent both."""
    if "active_kcal" in day and "resting_kcal" in day:
        return day["active_kcal"] + day["resting_kcal"]
    return None


def miles(metres: float) -> float:
    return metres / M_PER_MILE
