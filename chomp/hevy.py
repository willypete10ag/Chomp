"""Hevy's workout CSV export (#94). Hevy has no API without Pro, so workouts come in as the
CSV it exports (Profile > Settings > Export & Import Data > Export Workouts): one row per set,

    title, start_time, end_time, description, exercise_title, superset_id, exercise_notes,
    set_index, set_type, weight_lbs, reps, distance_miles, duration_seconds, rpe

(weight_kg and distance_km instead with metric units). Times are local, like "13 Oct 2026,
07:15". Rows are grouped into workouts by title and start time. Pure parsing; no I/O.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from chomp import logic

KG_PER_LB = logic.KG_PER_LB
M_PER_MILE = 1609.344
MAX_PROBLEMS = 5
DATE_FORMATS = (
    "%d %b %Y, %H:%M",
    "%d %b %Y, %H:%M:%S",
    "%d %B %Y, %H:%M",
    "%b %d, %Y, %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)
SET_TYPES = {"normal", "warmup", "failure", "dropset"}
REQUIRED = {"title", "start_time", "exercise_title"}


class HevyError(ValueError):
    pass


@dataclass
class WorkoutSet:
    exercise: str
    set_index: int
    set_type: str = "normal"
    weight_kg: float | None = None
    reps: int | None = None
    distance_m: float | None = None
    duration_s: float | None = None
    rpe: float | None = None
    superset_id: int | None = None
    exercise_notes: str | None = None


@dataclass
class Workout:
    started_at: str  # UTC storage format
    title: str
    ended_at: str | None = None
    description: str | None = None
    sets: list[WorkoutSet] = field(default_factory=list)


@dataclass
class Parsed:
    workouts: list[Workout] = field(default_factory=list)
    rejected: int = 0
    problems: list[str] = field(default_factory=list)

    def reject(self, line: int, problem: str) -> None:
        self.rejected += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(f"row {line}: {problem}")


def parse_time(value: str, tz: str) -> str:
    """A Hevy time (local) to UTC storage format."""
    text = " ".join(value.split())
    for fmt in DATE_FORMATS:
        try:
            local = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        try:
            local = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"unknown time {value!r}") from None
    if local.tzinfo is None:
        local = local.replace(tzinfo=ZoneInfo(tz))
    return logic.format_utc(local)


def _number(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    number = float(text)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"not a usable number: {value!r}")
    return number


def _int(value: str | None) -> int | None:
    number = _number(value)
    return None if number is None else round(number)


def _text(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def parse(data: bytes | str, tz: str) -> Parsed:
    """Workouts from a Hevy CSV export. Bad rows are skipped and reported; a file that isn't
    a Hevy export at all raises HevyError."""
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise HevyError("the file is empty")
    reader.fieldnames = [(name or "").strip().lower() for name in reader.fieldnames]
    columns = set(reader.fieldnames)
    if missing := REQUIRED - columns:
        raise HevyError(
            "this doesn't look like a Hevy workout export (missing "
            + ", ".join(sorted(missing))
            + ")"
        )
    weight_factor = KG_PER_LB if "weight_lbs" in columns else 1.0
    weight_col = "weight_lbs" if "weight_lbs" in columns else "weight_kg"
    distance_factor = M_PER_MILE if "distance_miles" in columns else 1000.0
    distance_col = "distance_miles" if "distance_miles" in columns else "distance_km"

    parsed = Parsed()
    by_key: dict[tuple[str, str], Workout] = {}
    counters: dict[tuple[tuple[str, str], str], int] = {}
    for line, row in enumerate(reader, start=2):
        try:
            title = _text(row.get("title")) or "Workout"
            started = parse_time(row.get("start_time") or "", tz)
            exercise = _text(row.get("exercise_title"))
            if exercise is None:
                raise ValueError("no exercise")
            key = (started, title)
            workout = by_key.get(key)
            if workout is None:
                end = _text(row.get("end_time"))
                workout = Workout(
                    started_at=started,
                    title=title,
                    ended_at=parse_time(end, tz) if end else None,
                    description=_text(row.get("description")),
                )
                by_key[key] = workout
            set_type = (_text(row.get("set_type")) or "normal").lower()
            weight = _number(row.get(weight_col))
            distance = _number(row.get(distance_col))
            index = _int(row.get("set_index"))
            if index is None:  # number sets per exercise in file order
                index = counters.get((key, exercise), 0)
            counters[(key, exercise)] = index + 1
            workout.sets.append(
                WorkoutSet(
                    exercise=exercise,
                    set_index=index,
                    set_type=set_type if set_type in SET_TYPES else "normal",
                    weight_kg=None if weight is None else weight * weight_factor,
                    reps=_int(row.get("reps")),
                    distance_m=None if distance is None else distance * distance_factor,
                    duration_s=_number(row.get("duration_seconds")),
                    rpe=_number(row.get("rpe")),
                    superset_id=_int(row.get("superset_id")),
                    exercise_notes=_text(row.get("exercise_notes")),
                )
            )
        except ValueError as e:
            parsed.reject(line, str(e))
    parsed.workouts = sorted(by_key.values(), key=lambda w: w.started_at)
    if not parsed.workouts and not parsed.rejected:
        raise HevyError("the export has no workouts in it")
    return parsed
