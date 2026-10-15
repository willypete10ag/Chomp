"""Apple Health's own export (#94), for history the phone's daily send doesn't cover. In the
Health app: your picture > Export All Health Data gives export.zip, with
apple_health_export/export.xml inside: one <Record> per sample,

    <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Will's Apple Watch"
            unit="count" startDate="2026-10-13 07:02:11 -0400" ... value="412"/>

Weigh-ins are kept as they are. Activity samples are added up into local days, per source:
the export lists the iPhone's and the Watch's samples for the same steps separately (Health
itself merges them), so each day keeps its largest source's total rather than adding them up.
That can come out a little under Health's own figure when two sources each saw different
parts of a day. The file can be hundreds of MB, so it's read as a stream. Pure parsing; no
database.
"""

from __future__ import annotations

import io
import math
import re
import shutil
import tempfile
import zipfile
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import IO, BinaryIO
from xml.etree.ElementTree import ParseError, XMLPullParser

from chomp import health, logic

MAX_PROBLEMS = 5
CHUNK = 1 << 20
WEIGHT_TYPE = "HKQuantityTypeIdentifierBodyMass"
ACTIVITY_TYPES = {
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierActiveEnergyBurned": "active_kcal",
    "HKQuantityTypeIdentifierBasalEnergyBurned": "resting_kcal",
    "HKQuantityTypeIdentifierDistanceWalkingRunning": "distance_m",
    "HKQuantityTypeIdentifierAppleExerciseTime": "exercise_min",
    "HKQuantityTypeIdentifierFlightsClimbed": "flights",
}
# The export starts with an inline DTD that some iOS versions write badly; it's not needed
DOCTYPE = re.compile(rb"<!DOCTYPE\b.*?\]\s*>", re.DOTALL)


class ExportError(ValueError):
    pass


@dataclass
class Parsed:
    weights: dict[str, float] = field(default_factory=dict)  # UTC measured_at -> kg
    # (metric, UTC start of the local day) -> that day's total in the stored unit
    days: dict[tuple[str, str], float] = field(default_factory=dict)
    records: int = 0  # samples of the kinds Chomp keeps
    rejected: int = 0
    problems: list[str] = field(default_factory=list)

    def reject(self, problem: str) -> None:
        self.rejected += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(problem)


def open_export(source: BinaryIO) -> IO[bytes]:
    """export.xml from Health's export.zip, or the XML itself, as a binary stream. `source`
    must be seekable (a file, not a pipe)."""
    head = source.read(4)
    source.seek(0)
    if head[:2] != b"PK":
        return source
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as e:
        raise ExportError(f"not a readable zip: {e}") from None
    names = [n for n in archive.namelist() if n.rsplit("/", 1)[-1] == "export.xml"]
    if not names:
        raise ExportError("no export.xml in the zip; is it Health's Export All Health Data?")
    return archive.open(min(names, key=len))


def spool(stream: BinaryIO) -> IO[bytes]:
    """A seekable copy of a pipe (standard input), on disk once it's big."""
    copy = tempfile.SpooledTemporaryFile(max_size=64 * CHUNK)  # noqa: SIM115 (the caller closes it)
    shutil.copyfileobj(stream, copy, CHUNK)
    copy.seek(0)
    return copy


def _chunks(stream: IO[bytes]):
    """The XML in pieces, with the DTD near the top taken out."""
    head = stream.read(CHUNK)
    yield DOCTYPE.sub(b"", head, count=1)
    while chunk := stream.read(CHUNK):
        yield chunk


def parse(stream: IO[bytes], tz: str) -> Parsed:
    parsed = Parsed()
    # (metric, day start, source) -> total
    totals: dict[tuple[str, str, str], float] = defaultdict(float)
    parser = XMLPullParser(events=("start", "end"))
    depth, root, found_root = 0, None, False
    try:
        for chunk in _chunks(stream):
            parser.feed(chunk)
            for event, elem in parser.read_events():
                if event == "start":
                    depth += 1
                    if root is None:
                        root = elem
                        found_root = elem.tag == "HealthData"
                    continue
                depth -= 1
                # Only top-level records: a Correlation repeats ones already listed
                if depth == 1 and elem.tag == "Record":
                    _record(parsed, totals, elem.attrib, tz)
                if depth == 1:
                    root.clear()  # keeps memory flat across millions of records
        parser.close()
    except ParseError as e:
        raise ExportError(f"not a readable Health export: {e}") from None
    if not found_root:
        raise ExportError("not a Health export: expected <HealthData>")
    best: dict[tuple[str, str], float] = {}
    for (metric, day, _source), total in totals.items():
        best[(metric, day)] = max(best.get((metric, day), 0.0), total)
    for (metric, day), total in best.items():
        if total > logic.ACTIVITY_MAX[metric]:
            parsed.reject(f"{metric} on {logic.local_date(day, tz)}: {total:g} is too much")
        else:
            parsed.days[(metric, day)] = total
    return parsed


def _record(parsed: Parsed, totals: dict, attrs: dict[str, str], tz: str) -> None:
    kind = attrs.get("type", "")
    metric = ACTIVITY_TYPES.get(kind)
    if metric is None and kind != WEIGHT_TYPE:
        return
    parsed.records += 1
    try:
        when = attrs.get("startDate", "")
        value = float(attrs.get("value", ""))
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"not a usable amount: {attrs.get('value')!r}")
        unit = attrs.get("unit", "")
        if metric is None:
            parsed.weights[health.parse_date(when, tz)] = logic.to_kg(value, unit)
            return
        factor = logic.ACTIVITY_UNITS[metric].get(unit.strip().lower())
        if factor is None:
            raise ValueError(f"unknown unit {unit!r}")
        # Samples a minute apart share a day; parsing every date is most of the work
        day = _day_start(when[:16] + when[19:], tz)
    except ValueError as e:
        parsed.reject(f"{metric or 'weight'}: {e}")
        return
    totals[(metric, day, attrs.get("sourceName", ""))] += value * factor


@lru_cache(maxsize=1 << 16)
def _day_start(minute: str, tz: str) -> str:
    """UTC start of the local day a "2026-10-13 07:02 -0400" falls on."""
    start = health.parse_date(minute[:16] + ":00" + minute[16:], tz)
    return logic.local_day_bounds(logic.local_date(start, tz), tz)[0]


def day_rows(parsed: Parsed, tz: str) -> list[tuple[str, str, str, float]]:
    """The days as (metric, UTC day start, UTC day end, total), oldest first, for
    store.import_activity_days."""
    return [
        (metric, start, logic.local_day_bounds(logic.local_date(start, tz), tz)[1], qty)
        for (metric, start), qty in sorted(parsed.days.items(), key=lambda item: item[0][1])
    ]


def read(source: BinaryIO, tz: str) -> Parsed:
    """Parse Health's export.zip (or export.xml) from a seekable binary file."""
    try:
        with open_export(source) as stream:
            return parse(stream, tz)
    except (zipfile.BadZipFile, zlib.error, EOFError) as e:  # a cut-off or damaged zip
        raise ExportError(f"not a readable zip: {e or type(e).__name__}") from None


def read_bytes(data: bytes, tz: str) -> Parsed:
    return read(io.BytesIO(data), tz)
