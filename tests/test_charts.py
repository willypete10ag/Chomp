"""Server-drawn charts (#105): gridlines, gaps, reference and ghost lines, inspector data."""

from __future__ import annotations

import html
import json
import re
from datetime import date, timedelta

from chomp import charts


def points_of(svg: str) -> list[dict]:
    return json.loads(html.unescape(re.search(r'data-points="([^"]*)"', svg).group(1)))


def days(n: int, values) -> list[tuple[date, float | None]]:
    start = date(2026, 10, 1)
    return [(start + timedelta(days=i), v) for i, v in zip(range(n), values, strict=True)]


def test_nice_ticks_and_axis_text():
    assert charts.nice_ticks(0, 2600) == [0, 1000, 2000]
    assert charts.nice_ticks(0, 12000) == [0, 5000, 10000]
    assert charts.nice_ticks(170, 176) == [170, 172, 174, 176]
    assert charts.nice_ticks(0, 0) == [0]
    assert [charts.axis_text(v) for v in (12000, 2500, 950, 172.5, 2000)] == [
        "12k",
        "2.5k",
        "950",
        "172.5",
        "2k",
    ]


def test_a_missing_value_is_a_gap_in_a_line():
    assert charts.path([(0, 1), (1, None), (2, 3), (3, 4)]) == "M0.0 1.0 M2.0 3.0 L3.0 4.0"


def test_bar_chart_draws_gaps_goal_average_ghosts_and_a_partial_last_bar():
    svg = charts.bar_chart(
        days(4, [100, None, 300, 50]),
        kind="steps",
        title="Steps",
        value_text=lambda v: f"{v:.0f} steps",
        summary="Avg 200",
        goal=250,
        average=200,
        ghost=[80, 90, None, 120],
        partial_last=True,
        href=lambda d: f"/app?date={d}",
    )
    assert svg.count('class="bar steps') == 3  # the missing day draws nothing
    assert 'class="bar steps partial"' in svg and 'class="hatched steps"' in svg
    assert "goal 250 steps</text>" in svg and "avg 200 steps</text>" in svg
    assert svg.count('class="ghost"') == 3 and "data-compare" in svg
    stops = points_of(svg)
    assert len(stops) == 4 and stops[0]["title"] == "Thu Oct 1"
    assert stops[0]["value"] == "100 steps" and stops[0]["note"] == "-50% vs avg"
    assert stops[0]["prev"] == "prev 80 steps" and stops[0]["href"] == "/app?date=2026-10-01"
    assert stops[1]["value"] == "no data" and "href" not in stops[1]
    assert stops[3]["note"] == "so far"
    assert "<th>Previous</th>" in svg and "50 steps so far" in svg  # the table


def test_bar_chart_by_week_reads_a_day_and_labels_week_starts():
    monday = date(2026, 9, 7)
    weekly = [(monday + timedelta(weeks=i), 7000.0 + i) for i in range(10)]
    svg = charts.bar_chart(
        weekly, kind="steps", title="t", value_text=lambda v: f"{v:.0f}", summary="", unit="week"
    )
    stops = points_of(svg)
    assert stops[0]["title"] == "Week of Sep 7" and stops[0]["value"] == "7000 a day"
    assert "data-compare" not in svg and ">11/9</text>" in svg  # the newest gets a label
    total = charts.bar_chart(
        weekly[:2],
        kind="train",
        title="t",
        value_text=lambda v: f"{v:.0f} lb",
        summary="",
        unit="week",
        per_day=False,
    )
    assert points_of(total)[0]["value"] == "7000 lb"


def test_reference_labels_move_apart_when_close():
    svg = charts.bar_chart(
        days(2, [100, 100]),
        kind="kcal",
        title="t",
        value_text=str,
        summary="",
        goal=101,
        average=100,
    )
    goal_y = float(re.search(r'class="tick label goal kcal" x="[\d.]+" y="([\d.]+)"', svg)[1])
    avg_y = float(re.search(r'class="tick label avg" x="[\d.]+" y="([\d.]+)"', svg)[1])
    assert avg_y - goal_y > 12


def test_line_chart_puts_stops_on_readings():
    first = date(2026, 10, 1)
    dots = [(first, 180.0), (first + timedelta(days=10), 178.0)]
    svg = charts.line_chart(
        first=first,
        last=first + timedelta(days=30),
        lines=[charts.Line("trend", dots)],
        dots=dots,
        title="Weight",
        summary="2 readings",
        stops=[(d, charts.Stop(str(d), f"{v} lb")) for d, v in dots],
        value_text=lambda v: f"{v:.1f} lb",
        goal=175,
    )
    assert svg.count('class="reading"') == 2 and 'class="line trend"' in svg
    assert "goal 175.0 lb</text>" in svg
    assert ">10/1</text>" in svg and ">10/31</text>" in svg
    hits = re.findall(r'class="hit" data-i="\d" cx="([\d.]+)" cy="([\d.]+)"', svg)
    assert len(hits) == 2 and hits[0][1] != hits[1][1]  # each on its own reading


def test_sparkline_and_heat_strip():
    assert charts.sparkline([1.0], "kcal") == ""  # one point is no trend
    line = charts.sparkline([1.0, None, 3.0], "kcal", also=[2.0, 2.0, 2.0], also_kind="burn")
    assert line.count("<path") == 2 and 'class="burn"' in line
    assert charts.sparkline([1.0, 2.0], "steps", bars=True).count("<rect") == 2
    start = date(2026, 10, 5)  # a Monday
    span = [start + timedelta(days=i) for i in range(14)]
    strip = charts.heat_strip({span[0]: 100, span[3]: 400}, span, "train", "Training days")
    assert strip.count("<rect") == 14 and 'class="lv4"' in strip and "Mon Oct 5" in strip
    assert "style=" not in strip  # the page's CSP allows no inline styles
    assert ">10/5<" in strip and ">10/12<" in strip and ">M<" in strip  # weeks and weekdays
    year = [start + timedelta(days=i) for i in range(120)]
    assert ">Oct<" in charts.heat_strip({}, year, "train", "Training days")  # months on top
    week = charts.heat_strip({}, span[:7], "weight", "Weigh-in days")  # one row, dated
    assert week.count("<rect") == 7 and ">5<" in week and ">11<" in week
    assert charts.heat_strip({}, span[:1], "weight", "Weigh-in days") == ""
