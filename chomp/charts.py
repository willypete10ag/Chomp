"""Server-drawn SVG charts for Progress (#105).

Every chart is plain SVG that renders with no script, wrapped in a figure that carries an
inspector line above the plot, a "Show as table" fold under it, and the inspectable points
as JSON in data-points. app.js reads those to scrub: press and drag (or arrow keys) moves a
crosshair to the nearest point and the inspector says what's there; a tap pins it.

Charts are honest about their data: a day without a value is a gap, never a zero, and a
day still in progress (today) is hatched and called "so far".
"""

from __future__ import annotations

import json
import math
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape as e

W, H = 340, 172
TOP, BOTTOM, RIGHT = 14, 22, 38  # gridline labels sit in the right gutter
PLOT_W, PLOT_H = W - RIGHT, H - TOP - BOTTOM
BASE = TOP + PLOT_H


@dataclass(frozen=True)
class Stop:
    """One inspectable point: the inspector reads "title · value · note", plus "prev" while
    Compare is on, and offers href (open that day) once pinned."""

    title: str
    value: str
    note: str = ""
    prev: str = ""
    href: str = ""


def nice_ticks(lo: float, hi: float, count: int = 3) -> list[float]:
    """About `count` round gridline values from lo to hi: steps of 1, 2, 2.5 or 5 x 10^n."""
    span = hi - lo
    if span <= 0:
        span = abs(hi) or 1.0
    raw = span / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw - 1e-12)
    out = []
    v = math.ceil(lo / step - 1e-9) * step
    while v <= hi + 1e-9:
        out.append(round(v, 6))
        v += step
    return out


def axis_text(v: float) -> str:
    """A gridline's label, short: 12k, 2.5k, 950, 172.5."""
    a = abs(v)
    if a >= 10_000:
        return f"{v / 1000:.0f}k"
    if a >= 1000:
        return f"{v / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{v:.0f}" if v == int(v) else f"{v:.1f}"


def stop_title(d: date, unit: str) -> str:
    if unit == "week":
        return f"Week of {d.strftime('%b')} {d.day}"
    if unit == "month":
        return f"{d.strftime('%B')} {d.year}"
    return f"{d.strftime('%a %b')} {d.day}"


def x_label(d: date, unit: str, count: int, first: bool) -> str:
    if unit == "month":
        return str(d.year) if d.month == 1 or first else d.strftime("%b")
    if unit == "day" and count <= 7:
        return d.strftime("%a")[0]
    return f"{d.month}/{d.day}"


def label_indexes(count: int, most: int = 6) -> list[int]:
    """Which of `count` slots get an x label: all of a week, else every few counted back from
    the last, so the newest always has one."""
    if count <= 7:
        return list(range(count))
    step = math.ceil(count / most)
    return sorted(range(count - 1, -1, -step))


def gridlines(ticks: Sequence[float], y: Callable[[float], float]) -> str:
    return "".join(
        f'<line class="grid" x1="0" x2="{PLOT_W}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
        f'<text class="tick" x="{W - 2}" y="{y(t) + 3.5:.1f}" text-anchor="end">'
        f"{axis_text(t)}</text>"
        for t in ticks
    )


def ref_line(
    cls: str, value: float, label: str, y: Callable[[float], float], below: bool = False
) -> str:
    """A labelled horizontal reference: goal (dashed), average (dotted). The label sits just
    above the line, or below it when another reference is close above."""
    at = y(value)
    text_y = at + 11 if below else at - 4
    return (
        f'<line class="ref {cls}" x1="0" x2="{PLOT_W}" y1="{at:.1f}" y2="{at:.1f}"/>'
        f'<text class="tick label {cls}" x="{PLOT_W - 2}" y="{text_y:.1f}" text-anchor="end">'
        f"{e(label)}</text>"
    )


def path(points: Sequence[tuple[float, float | None]]) -> str:
    """An SVG path through (x, y) points that lifts the pen over a None, so a gap stays one."""
    out, pen = [], "M"
    for x, y in points:
        if y is None:
            pen = "M"
            continue
        out.append(f"{pen}{x:.1f} {y:.1f}")
        pen = "L"
    return " ".join(out)


def figure(
    svg: str,
    stops: Sequence[tuple[float, Stop]],
    summary: str,
    table: Sequence[tuple[str, ...]],
    head: tuple[str, ...],
    *,
    title: str = "",
    compare: bool = False,
    cls: str = "",
) -> str:
    """The chart's frame: a title row (with a Compare chip when there's a previous period),
    the inspector, the SVG and its data as a table."""
    points = json.dumps(
        [{k: v for k, v in (("x", round(x, 1)), *vars(s).items()) if v != ""} for x, s in stops],
        separators=(",", ":"),
    )
    chip = (
        '<button type="button" class="chip js-only" data-compare aria-pressed="false">'
        "Compare</button>"
        if compare
        else ""
    )
    heading = f'<div class="chart-head"><h2>{title}</h2>{chip}</div>' if title or chip else ""
    rows = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in table)
    header = "".join(f"<th>{e(h)}</th>" for h in head)
    return (
        f'<figure class="chart-box{" " + cls if cls else ""}" data-chart'
        f' data-points="{e(points)}" data-w="{PLOT_W}">{heading}'
        f'<figcaption class="inspect" aria-live="polite">{summary}</figcaption>{svg}'
        '<details class="table"><summary>Show as table</summary>'
        f"<table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table></details>"
        "</figure>"
    )


def bar_chart(
    points: Sequence[tuple[date, float | None]],
    *,
    kind: str,
    title: str,
    value_text: Callable[[float], str],
    summary: str,
    unit: str = "day",
    per_day: bool = True,
    goal: float | None = None,
    goal_label: str = "goal",
    average: float | None = None,
    rolling: Sequence[float | None] | None = None,
    ghost: Sequence[float | None] | None = None,
    partial_last: bool = False,
    tone: Callable[[float], str] | None = None,
    href: Callable[[date], str] | None = None,
    heading: str = "",
    ref_text: Callable[[float], str] | None = None,
) -> str:
    """Bars, one per day, week or month. With per_day a week's or month's bar is its daily
    average ("a day"); without, its total. Optional: a dashed goal, a dotted average, a
    rolling-average line over the bars, the previous period's bars as outlined ghosts behind
    them (shown with Compare), and the last bar hatched as still in progress."""
    count = len(points)
    values = [v for _, v in points if v is not None]
    ghosts = [g for g in (ghost or ()) if g is not None]
    rolls = [r for r in (rolling or ()) if r is not None]
    peak = max([*values, *ghosts, *rolls, goal or 0, average or 0, 1.0]) * 1.06
    ticks = nice_ticks(0, peak)
    top = max(peak, ticks[-1])

    def y(v: float) -> float:
        return TOP + PLOT_H - PLOT_H * v / top

    slot = PLOT_W / max(count, 1)
    width = max(slot * (0.62 if count <= 31 else 0.78), 1.0)
    radius = min(width / 3, 4)
    pattern = f"hatch-{kind}-{zlib.crc32(title.encode()) % 100_000}"
    parts = [
        f'<svg class="chart bars {kind}" viewBox="0 0 {W} {H}" role="img" tabindex="0"'
        f' aria-label="{e(title)}">',
        f'<defs><pattern id="{pattern}" width="5" height="5" patternUnits="userSpaceOnUse"'
        f' patternTransform="rotate(45)"><rect class="hatch {kind}" width="2.2" height="5"/>'
        "</pattern></defs>",
        gridlines(ticks, y),
        f'<line class="axis" x1="0" x2="{PLOT_W}" y1="{BASE}" y2="{BASE}"/>',
    ]
    if ghost:
        parts.append('<g class="ghosts">')
        for i, g in enumerate(ghost):
            if g:
                x = i * slot + (slot - width) / 2
                parts.append(
                    f'<rect class="ghost" x="{x:.1f}" y="{y(g):.1f}" width="{width:.1f}"'
                    f' height="{BASE - y(g):.1f}" rx="{radius:.1f}"/>'
                )
        parts.append("</g>")
    stops: list[tuple[float, Stop]] = []
    table: list[tuple[str, ...]] = []
    parts.append('<g class="marks">')
    for i, (d, v) in enumerate(points):
        x = i * slot + (slot - width) / 2
        last = i == count - 1
        partial = partial_last and last
        if v:
            cls = f"bar {kind}"
            if tone and (t := tone(v)):
                cls += f" {t}"
            box = (
                f' x="{x:.1f}" y="{y(v):.1f}" width="{width:.1f}" height="{BASE - y(v):.1f}"'
                f' rx="{radius:.1f}"'
            )
            parts.append(f'<rect class="{cls}{" partial" if partial else ""}" data-i="{i}"{box}/>')
            if partial:
                # The stylesheet colors bars, so the hatching is a layer of its own on top
                parts.append(f'<rect class="hatched {kind}"{box} fill="url(#{pattern})"/>')
        text = (
            "no data"
            if v is None
            else value_text(v) + (" a day" if per_day and unit != "day" else "")
        )
        note = []
        if partial:
            note.append("so far")
        elif v and average and unit == "day":
            note.append(f"{(v - average) / average * 100:+.0f}% vs avg")
        prev = ghost[i] if ghost is not None and i < len(ghost) else None
        stops.append(
            (
                i * slot + slot / 2,
                Stop(
                    stop_title(d, unit),
                    text,
                    " · ".join(note),
                    "" if prev is None else f"prev {value_text(prev)}",
                    href(d) if href and unit == "day" and v is not None else "",
                ),
            )
        )
        row = (e(stop_title(d, unit)), text + (" so far" if partial else ""))
        if ghost is not None:
            row += ("&ndash;" if prev is None else value_text(prev),)
        table.append(row)
    parts.append("</g>")
    if rolling:
        line = path(
            [(i * slot + slot / 2, None if r is None else y(r)) for i, r in enumerate(rolling)]
        )
        if line:
            parts.append(f'<path class="roll {kind}" d="{line}"/>')
    short = ref_text or value_text
    close = bool(average and goal and abs(y(average) - y(goal)) < 14)
    if average:
        # When the goal is just above the average, the average's label goes under its line
        below = close and goal > average
        parts.append(ref_line("avg", average, f"avg {short(average)}", y, below))
    if goal:
        below = close and goal < average
        parts.append(ref_line(f"goal {kind}", goal, f"{goal_label} {short(goal)}", y, below))
    for i in label_indexes(count):
        d = points[i][0]
        last = i == count - 1
        x_at, anchor = (PLOT_W, "end") if last and count > 7 else (i * slot + slot / 2, "middle")
        parts.append(
            f'<text class="tick" x="{x_at:.1f}" y="{H - 5}" text-anchor="{anchor}">'
            f"{e(x_label(d, unit, count, i == 0))}</text>"
        )
    parts.append(f'<line class="cross" x1="0" x2="0" y1="{TOP - 4}" y2="{BASE}"/></svg>')
    head = ("When", "Value") + (("Previous",) if ghost is not None else ())
    return figure(
        "".join(parts), stops, summary, table, head, title=heading, compare=ghost is not None
    )


@dataclass(frozen=True)
class Line:
    """A series drawn as a line: its CSS class (and metric) and its (day, value) points."""

    cls: str
    points: Sequence[tuple[date, float]]


def line_chart(
    *,
    first: date,
    last: date,
    lines: Sequence[Line],
    title: str,
    summary: str,
    stops: Sequence[tuple[date, Stop]],
    value_text: Callable[[float], str],
    dots: Sequence[tuple[date, float]] = (),
    goal: float | None = None,
    goal_label: str = "goal",
    start: float | None = None,
    zero: bool = False,
    table_head: tuple[str, ...] = ("Day", "Value"),
    table: Sequence[tuple[str, ...]] = (),
    heading: str = "",
    kind: str = "",
) -> str:
    """Lines over days from first to last: optional raw readings as small dots under them, a
    dashed goal and a dotted start-of-range reference. The y axis fits the data with padding
    (from zero with zero=True)."""
    values = [v for line in lines for _, v in line.points] + [v for _, v in dots]
    lo, hi = (min(values), max(values)) if values else (0.0, 1.0)
    for ref in (goal, start):
        if ref is not None:
            lo, hi = min(lo, ref), max(hi, ref)
    if zero:
        lo = min(lo, 0.0)
        hi = hi * 1.08 or 1.0
    else:
        pad = max((hi - lo) * 0.12, 0.5)
        lo, hi = lo - pad, hi + pad
    ticks = [t for t in nice_ticks(lo, hi) if lo <= t <= hi]
    span = max((last - first).days, 1)

    def x(d: date) -> float:
        return PLOT_W * (d - first).days / span if span else PLOT_W / 2

    def y(v: float) -> float:
        return TOP + PLOT_H - PLOT_H * (v - lo) / (hi - lo)

    parts = [
        f'<svg class="chart lines {kind}" viewBox="0 0 {W} {H}" role="img" tabindex="0"'
        f' aria-label="{e(title)}">',
        gridlines(ticks, y),
    ]
    if zero and lo < 0:
        parts.append(f'<line class="axis" x1="0" x2="{PLOT_W}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    if start is not None:
        parts.append(ref_line("avg", start, f"start {value_text(start)}", y))
    if goal is not None:
        parts.append(ref_line(f"goal {kind}", goal, f"{goal_label} {value_text(goal)}", y))
    if dots:
        parts.append('<g class="dots">')
        parts.extend(
            f'<circle class="reading" cx="{x(d):.1f}" cy="{y(v):.1f}" r="2.4"/>' for d, v in dots
        )
        parts.append("</g>")
    for line in lines:
        if len(line.points) > 1:
            d = path([(x(day), y(v)) for day, v in line.points])
            parts.append(f'<path class="line {line.cls}" d="{d}"/>')
        if line.points:
            end_day, end = line.points[-1]
            parts.append(
                f'<circle class="end {line.cls}" cx="{x(end_day):.1f}" cy="{y(end):.1f}" r="3.5"/>'
            )
    # Each stop's marker sits on its reading, else on the first line that day
    at = {d: v for line in reversed(lines) for d, v in line.points} | dict(dots)
    parts.append('<g class="marks">')
    parts.extend(
        f'<circle class="hit" data-i="{i}" cx="{x(d):.1f}" cy="{y(at[d]) if d in at else TOP:.1f}"'
        ' r="0"/>'
        for i, (d, _) in enumerate(stops)
    )
    parts.append("</g>")
    if span <= 1:
        labels = [(first, "start")]
    else:
        labels = [(first, "start"), (last, "end")]
        if span >= 21:
            labels.insert(
                1, (date.fromordinal((first.toordinal() + last.toordinal()) // 2), "middle")
            )
    for d, anchor in labels:
        text = f"{d.month}/{d.day}" if span <= 366 else f"{d.strftime('%b')} {d.year}"
        parts.append(
            f'<text class="tick" x="{x(d):.1f}" y="{H - 5}" text-anchor="{anchor}">{e(text)}</text>'
        )
    parts.append(f'<line class="cross" x1="0" x2="0" y1="{TOP - 4}" y2="{BASE}"/></svg>')
    return figure(
        "".join(parts), [(x(d), s) for d, s in stops], summary, table, table_head, title=heading
    )


def sparkline(
    values: Sequence[float | None],
    kind: str,
    *,
    bars: bool = False,
    also: Sequence[float | None] | None = None,
    also_kind: str = "",
) -> str:
    """A tiny trend with no axes for a tile or an overview card; a line's last point gets a
    dot. `also` draws a second line on the same scale (eaten against burned)."""
    w, h, pad = 340, 44, 4
    present = [v for v in [*values, *(also or ())] if v is not None]
    if len([v for v in values if v is not None]) < 2:
        return ""
    lo, hi = (0.0, max(present)) if bars else (min(present), max(present))
    hi = hi if hi > lo else lo + 1

    def y(v: float) -> float:
        return pad + (h - 2 * pad) * (1 - (v - lo) / (hi - lo))

    n = len(values)
    parts = [f'<svg class="spark {kind}" viewBox="0 0 {w} {h}" aria-hidden="true">']
    if bars:
        slot = w / n
        for i, v in enumerate(values):
            if v:
                parts.append(
                    f'<rect x="{i * slot + slot * 0.15:.1f}" y="{y(v):.1f}"'
                    f' width="{max(slot * 0.7, 0.8):.1f}" height="{h - pad - y(v):.1f}" rx="1.5"/>'
                )
    else:
        step = (w - 2 * pad) / max(n - 1, 1)
        for series, cls in ((also, also_kind), (values, kind)):
            if not series:
                continue
            points = [(pad + i * step, None if v is None else y(v)) for i, v in enumerate(series)]
            ends = [p for p in points if p[1] is not None]
            if not ends:
                continue
            parts.append(f'<path class="{cls}" d="{path(points)}"/>')
            lx, ly = ends[-1]
            parts.append(f'<circle class="{cls}" cx="{lx:.1f}" cy="{ly:.1f}" r="3.5"/>')
    parts.append("</svg>")
    return "".join(parts)


def heat_strip(by_day: dict[date, float], days: Sequence[date], kind: str, title: str) -> str:
    """One cell per day, shaded by the day's amount in four steps between the lightest and
    heaviest days shown; empty days stay a faint cell. A week or less is one row, each cell
    with its weekday and date under it; up to six weeks is a calendar, a row per week;
    longer is a column per week (Monday on top) with weekday letters down the side and
    months along the top."""
    if len(days) < 2:
        return ""
    size, gap = 11, 3
    step = size + gap
    amounts = sorted(v for d in days if (v := by_day.get(d)))

    def cell(d: date, x: float, y: float) -> str:
        v = by_day.get(d)
        level = 0
        if v and amounts:
            rank = sum(1 for a in amounts if a <= v) / len(amounts)
            level = min(4, max(1, math.ceil(rank * 4)))
        label = f"{d.strftime('%a %b')} {d.day}" + ("" if level else ": none")
        return (
            f'<rect class="lv{level}" x="{x}" y="{y}" width="{size}" height="{size}"'
            f' rx="3"><title>{e(label)}</title></rect>'
        )

    parts = []
    if len(days) <= 7:
        w, h = len(days) * step * 2 - gap, size + 22
        for i, d in enumerate(days):
            x = i * step * 2 + size / 2
            parts.append(cell(d, x, 0))
            mid = x + size / 2
            parts.append(
                f'<text class="tick mid" x="{mid}" y="{size + 9}">{d.strftime("%a")[0]}</text>'
            )
            parts.append(f'<text class="tick mid" x="{mid}" y="{size + 19}">{d.day}</text>')
    elif len(days) <= 42:
        # a calendar: a row per week, Monday first, each row's first date at its left
        left, top = 22, 12
        lead = days[0].weekday()
        weeks = math.ceil((lead + len(days)) / 7)
        w, h = left + 7 * step - gap, top + weeks * step - gap
        for col, letter in enumerate("MTWTFSS"):
            x = left + col * step + size / 2
            parts.append(f'<text class="tick mid" x="{x}" y="8">{letter}</text>')
        for row in range(weeks):
            first = max(days[0] + timedelta(days=row * 7 - lead), days[0])
            y = top + row * step + size - 2
            parts.append(f'<text class="tick" x="0" y="{y}">{first.month}/{first.day}</text>')
        for i, d in enumerate(days):
            row, col = divmod(lead + i, 7)
            parts.append(cell(d, left + col * step, top + row * step))
    else:
        left, top = 10, 12
        lead = days[0].weekday()
        weeks = math.ceil((lead + len(days)) / 7)
        w, h = left + weeks * step - gap, top + 7 * step - gap
        for row, letter in enumerate("MTWTFSS"):
            if row % 2 == 0:
                y = top + row * step + size - 2
                parts.append(f'<text class="tick" x="0" y="{y}">{letter}</text>')
        shown = None
        for col in range(weeks):
            first = max(days[0] + timedelta(days=col * 7 - lead), days[0])
            x = left + col * step
            if first.month != shown:
                text = first.strftime("%b")
                if col == 0 and (first + timedelta(days=21)).month != first.month:
                    continue  # the next month's label would sit on top of it
            else:
                continue
            shown = first.month
            parts.append(f'<text class="tick" x="{x}" y="8">{e(text)}</text>')
        for i, d in enumerate(days):
            col, row = divmod(lead + i, 7)
            parts.append(cell(d, left + col * step, top + row * step))
    # drawn at a fixed cell size, so a short range is a few cells rather than a few huge ones
    head = (
        f'<svg class="heat {kind}" viewBox="0 0 {w} {h}" width="{w * 1.4:.0f}"'
        f' role="img" aria-label="{e(title)}">'
    )
    return head + "".join(parts) + "</svg>"
