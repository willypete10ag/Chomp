"""Web dashboard at /app: today's meals against targets, history charts, and saved foods.

Server-rendered HTML on the same Starlette app as /mcp: charts are inline SVG and every
edit (log food, change grams, move an entry, edit or combine meals, rename a food, set
goals, undo) is a plain form post that goes through store.py. A small script served from
here (static/app.js, no third-party code) adds sliders, quick picks and live previews; the
pages work without it. Phones can add it to the home screen (web app manifest at
/app/manifest.webmanifest).

Undo: each edit stores the rows it touched before and after (undo_actions, migration 0005)
and the next page offers Undo, which only applies while those rows are unchanged.

Sign-in: with PUBLIC_URL set, a password page (AUTH_PASSWORD, sharing the consent page's
lockout) starts a 30-day session. The cookie holds a random token; only its SHA-256 hash is
stored (web_sessions, migration 0004). POSTs must come from the dashboard's own origin. In
local-only mode the dashboard needs no sign-in, like /mcp.

Cloudflare Access may sit in front of /app (README, "Dashboard"), but never in front of /mcp
or the OAuth routes: Claude's servers can't complete a browser login.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import re
import secrets
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from chomp import db, logic, store
from chomp.auth import ChompAuthProvider
from chomp.config import Settings
from chomp.sources import SourceError
from chomp.sources.fdc import FDCClient

log = logging.getLogger("chomp.web")

PREFIX = "/app"
COOKIE = "chomp_session"
SESSION_TTL_S = 30 * 86400
SUBJECT = "owner"
HISTORY_RANGES = (7, 30, 90)
FOODS_LIMIT = 200
MAX_GRAMS = 10_000
LABEL_MAX, NOTE_MAX, FOOD_NAME_MAX = 60, 200, 120
ADD_LIMIT = 30  # foods listed on the Add food page
USDA_LIMIT = 10
UNDO_SHOW_S = 30 * 60  # how long a confirmation keeps its Undo button
TIME_RE = re.compile(r"(\d{2}):(\d{2})(:\d{2})?")
MIN_YEAR, MAX_YEAR = 1900, 2999
STATIC_DIR = Path(__file__).resolve().parent / "static"
ICONS = {
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
}
THEME = "#1f7a4d"
# In the stylesheet and script URLs, so a phone's cached copy is replaced as soon as they change
CSS_VERSION = hashlib.sha256((STATIC_DIR / "app.css").read_bytes()).hexdigest()[:10]
JS_VERSION = hashlib.sha256((STATIC_DIR / "app.js").read_bytes()).hexdigest()[:10]

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self';"
        " manifest-src 'self';"
        " form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

NUTRIENT_LABELS = {
    "kcal": "Calories",
    "protein_g": "Protein",
    "carbs_g": "Carbs",
    "fat_g": "Fat",
    "fiber_g": "Fiber",
    "sugar_g": "Sugar",
    "sodium_mg": "Sodium",
}
UNITS = {
    "kcal": "kcal",
    "protein_g": "g",
    "carbs_g": "g",
    "fat_g": "g",
    "fiber_g": "g",
    "sugar_g": "g",
    "sodium_mg": "mg",
}
LONG_NAME = 40  # names longer than this may be cut to two lines; the full name shows on tap
NBSP = "\u00a0"  # keeps a number on the same line as its unit
MACROS = (("protein_g", "p", "P"), ("carbs_g", "c", "C"), ("fat_g", "f", "F"))
SOURCES = {"fdc": "USDA", "off": "Open Food Facts", "label": "Label", "manual": "Manual"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    """Browser sessions, stored hashed in SQLite so a restart doesn't sign anyone out."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self):
        return closing(db.connect(self.db_path))

    def create(self, subject: str = SUBJECT) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._connect() as conn, db.transaction(conn):
            conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
            conn.execute(
                "INSERT INTO web_sessions (token_hash, subject, expires_at) VALUES (?, ?, ?)",
                (_hash(token), subject, now + SESSION_TTL_S),
            )
        return token

    def subject(self, token: str) -> str | None:
        if not token:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT subject FROM web_sessions WHERE token_hash = ? AND expires_at > ?",
                (_hash(token), int(time.time())),
            ).fetchone()
        return row["subject"] if row else None

    def delete(self, token: str) -> None:
        with self._connect() as conn, db.transaction(conn):
            conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (_hash(token),))


# --- Formatting -----------------------------------------------------------------------------

e = html.escape


def num(value: float | None, places: int = 0) -> str:
    """A display number: thousands separators, no trailing '.0'."""
    if value is None:
        return "&ndash;"
    text = f"{value:,.{places}f}"
    return text[:-2] if places == 1 and text.endswith(".0") else text


def amount(key: str, value: float | None) -> str:
    return f"{num(value, logic.DISPLAY_PLACES[key])}{NBSP}{UNITS[key]}"


def clock(local_iso: str) -> str:
    dt = datetime.fromisoformat(local_iso)
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def capitalized(text: str) -> str:
    """First letter up, the rest as typed ('usual breakfast' -> 'Usual breakfast')."""
    return text[:1].upper() + text[1:]


def long_date(day: date, today: date) -> str:
    return (
        day.strftime("%A, %B ") + str(day.day) + (f", {day.year}" if day.year != today.year else "")
    )


def day_title(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    fmt = f"%a, %b {day.day}" + (", %Y" if day.year != today.year else "")
    return day.strftime(fmt)


def macros_line(n: logic.Nutrients) -> str:
    """'P 20 C 3.2 F 22' on screen; 'Protein 20 g' and so on for screen readers."""
    return " ".join(
        f'<span class="{cls}"><span aria-hidden="true">{letter}</span>'
        f'<span class="sr">{NUTRIENT_LABELS[key]}</span>{NBSP}{num(n[key], 1)}'
        f'<span class="sr"> g</span></span>'
        for key, cls, letter in MACROS
    )


def extras_line(n: logic.Nutrients) -> str:
    """Fiber, sugar and sodium, where known: 'Fiber 3 g · Sodium 120 mg'."""
    return " · ".join(
        f"{NUTRIENT_LABELS[k]} {amount(k, n[k])}" for k in logic.OPTIONAL if n.get(k) is not None
    )


def meter(percent: float | None, kind: str, *, warn_over: bool = False) -> str:
    """A progress bar. Only warn_over bars turn red past 100% (going over a protein target
    isn't a mistake)."""
    width = 0.0 if percent is None else max(0.0, min(percent, 100.0))
    over = " over" if warn_over and percent is not None and percent > 100 else ""
    return (
        f'<svg class="meter" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">'
        f'<rect class="track" width="100" height="8"/>'
        f'<rect class="fill {kind}{over}" width="{width:.1f}" height="8"/></svg>'
    )


def bar_chart(
    points: list[tuple[date, float | None]], target: float | None, kind: str, title: str
) -> str:
    """Inline SVG bars, one per day (a link to that day); an optional dashed target line."""
    w, h, top, bottom = 320, 150, 14, 18
    plot = h - top - bottom
    values = [v for _, v in points if v]
    peak = max([*values, target or 0, 1.0])
    slot = w / len(points)
    bar = max(slot * 0.7, 1.0)

    def y(v: float) -> float:
        return top + plot - plot * v / peak

    parts = [
        f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="{e(title)}">',
        f'<line class="axis" x1="0" x2="{w}" y1="{top + plot}" y2="{top + plot}"/>',
    ]
    step = 1 if len(points) <= 7 else math.ceil(len(points) / 6)
    for i, (d, v) in enumerate(points):
        x = i * slot + (slot - bar) / 2
        if v:
            label = f"{d.strftime('%a %b')} {d.day}: {num(v)}"
            parts.append(
                f'<a href="{PREFIX}?date={d.isoformat()}"><rect class="bar {kind}" x="{x:.1f}"'
                f' y="{y(v):.1f}" width="{bar:.1f}" height="{top + plot - y(v):.1f}">'
                f"<title>{e(label)}</title></rect></a>"
            )
        last = i == len(points) - 1
        if (len(points) - 1 - i) % step == 0 or last:
            text = d.strftime("%a")[0] if len(points) <= 7 else f"{d.month}/{d.day}"
            # the last label hugs the right edge so it isn't clipped
            x_label, anchor = (w, "end") if last else (i * slot + slot / 2, "middle")
            parts.append(
                f'<text class="tick" x="{x_label:.1f}" y="{h - 4}"'
                f' text-anchor="{anchor}">{e(text)}</text>'
            )
    if target:
        parts.append(
            f'<line class="target" x1="0" x2="{w}" y1="{y(target):.1f}" y2="{y(target):.1f}"/>'
            f'<text class="tick" x="{w - 2}" y="{y(target) - 3:.1f}" text-anchor="end">'
            f"target {num(target)}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


# --- Pages ----------------------------------------------------------------------------------

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="{theme}">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Chomp">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="manifest" href="{p}/manifest.webmanifest" crossorigin="use-credentials">
<link rel="apple-touch-icon" href="{p}/static/apple-touch-icon.png">
<link rel="icon" href="{p}/static/icon-192.png">
<link rel="stylesheet" href="{p}/static/app.css?v={css_version}">
<script src="{p}/static/app.js?v={js_version}" defer></script>
<title>{title}</title></head><body{body_class}>"""

NAV = (("today", "", "Today"), ("history", "/history", "History"), ("foods", "/foods", "Foods"))


def render(
    title: str, body: str, active: str | None = None, *, signed_in: bool = False, status: int = 200
) -> HTMLResponse:
    nav = ""
    if active is not None:
        links = "".join(
            f'<a href="{PREFIX}{path}"{" aria-current=page" if key == active else ""}>{label}</a>'
            for key, path, label in NAV
        )
        nav = f'<nav class="tabs">{links}</nav>'
    foot = ""
    if signed_in:
        foot = (
            f'<footer><form method="post" action="{PREFIX}/logout">'
            '<button class="link" type="submit">Sign out</button></form></footer>'
        )
    page = (
        HEAD.format(
            theme=THEME,
            p=PREFIX,
            css_version=CSS_VERSION,
            js_version=JS_VERSION,
            title=e(f"{title} · Chomp"),
            body_class=' class="has-tabs"' if nav else "",
        )
        + f"<main>{body}</main>{foot}{nav}</body></html>"
    )
    return HTMLResponse(page, status_code=status, headers=SECURITY_HEADERS)


def message(title: str, text: str, status: int, *, signed_in: bool = False) -> HTMLResponse:
    body = f'<h1>{e(title)}</h1><p>{e(text)}</p><p><a href="{PREFIX}">Back to today</a></p>'
    return render(title, body, "", signed_in=signed_in, status=status)


LOGIN_FORM = """<div class="login"><img src="{p}/static/icon-192.png" alt="" width="72"
 height="72"><h1>Chomp</h1>{error}
<form method="post" action="{p}/login"><input type="hidden" name="next" value="{next}">
<label>Password<input type="password" name="password" autofocus required
 autocomplete="current-password"></label>
<button type="submit">Sign in</button></form>
<p class="muted">The same password you used to connect Claude to Chomp.</p></div>"""


def login_page(next_url: str, error: str = "", status: int = 200) -> HTMLResponse:
    err = f'<p class="error">{e(error)}</p>' if error else ""
    body = LOGIN_FORM.format(p=PREFIX, next=e(next_url), error=err)
    return render("Sign in", body, status=status)


def safe_next(value: str) -> str:
    """Only redirect back into the dashboard after sign-in."""
    parts = urlsplit(value)
    if (
        parts.scheme
        or parts.netloc
        or not (value == PREFIX or value.startswith(PREFIX + "/") or value.startswith(PREFIX + "?"))
    ):
        return PREFIX
    return value


def grams_note(grams: float | None) -> str:
    return f" · {num(grams, 1)}{NBSP}g" if grams else ""


def parse_grams(value: str) -> float:
    try:
        grams = float(value)
    except ValueError:
        raise ValueError("Enter the grams as a number, like 150.") from None
    if not math.isfinite(grams) or grams <= 0 or grams > MAX_GRAMS:
        raise ValueError(f"Grams must be more than 0 and at most {MAX_GRAMS:,}.")
    return grams


def int_param(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def week_strip(day: date, today: date) -> str:
    """Seven day links around the shown day (never past today), the shown day marked."""
    end = min(day + timedelta(days=3), today)
    links = []
    for d in logic.date_range(end - timedelta(days=6), end):
        current = ' aria-current="date"' if d == day else ""
        mark = ' class="is-today"' if d == today else ""
        links.append(
            f'<a href="{PREFIX}?date={d.isoformat()}"{current}{mark}'
            f' aria-label="{e(long_date(d, today))}"><span>{d.strftime("%a")[0]}</span>'
            f"<b>{d.day}</b></a>"
        )
    return f'<nav class="week" aria-label="Nearby days">{"".join(links)}</nav>'


@dataclass
class DayContext:
    """What an entry or food form on a day needs to know about that day."""

    day: date
    today: date
    meals: list[tuple[int, str]]  # (meal id, "8:05 AM · Breakfast") for the menus
    kcal: float  # calories logged that day
    goal: float | None  # the calorie target, if set


def slider_top(*grams: float | None) -> int:
    """Where a grams slider ends: at least 500 g, else twice the largest suggestion."""
    top = max([500.0, *(2 * g for g in grams if g)])
    return int(math.ceil(top / 50) * 50)


def per100_attr(n: logic.Nutrients) -> str:
    return e(json.dumps({k: n[k] for k in logic.CORE}), quote=True)


def amount_field(
    name: str,
    value: str,
    *,
    label: str,
    unit: str,
    top: float,
    step: float = 1,
    low: float = 1,
    required: bool = True,
    placeholder: str = "",
) -> str:
    """A number box with a slider under it. The slider only shows with the script, and only
    the box is submitted, so the form works the same without it."""
    start = value.replace(",", "") or str(low)
    try:
        start = str(min(max(float(start), low), top))
    except ValueError:
        start = str(low)
    return (
        f'<div class="amount"><label>{label}<span class="field"><input name="{name}"'
        f' data-amount inputmode="decimal" autocomplete="off" value="{e(value)}"'
        + (" required" if required else "")
        + (f' placeholder="{e(placeholder)}"' if placeholder else "")
        + f'><span class="unit">{unit}</span></span></label>'
        f'<input type="range" class="js-only" min="{num(low)}" max="{num(top).replace(",", "")}"'
        f' step="{step}" value="{e(start)}" aria-hidden="true" tabindex="-1"></div>'
    )


def chips(options: list[tuple[str, float]]) -> str:
    """Quick picks that fill in the grams box (script only)."""
    if not options:
        return ""
    seen, buttons = set(), []
    for label, grams in options:
        value = num(grams, 1).replace(",", "")
        if value in seen:
            continue
        seen.add(value)
        buttons.append(f'<button type="button" class="chip" data-set="{value}">{label}</button>')
    return f'<div class="chips js-only">{"".join(buttons)}</div>'


def grams_chips(food, alias_grams: float | None = None, current: float | None = None) -> str:
    options = []
    if current:
        options += [(f"½ · {num(current / 2, 1)}{NBSP}g", current / 2)]
        options += [(f"2&times; · {num(current * 2, 1)}{NBSP}g", current * 2)]
    if alias_grams:
        options.append((f"Your usual · {num(alias_grams, 1)}{NBSP}g", alias_grams))
    if food["serving_g"]:
        options.append((f"1 serving · {num(food['serving_g'], 1)}{NBSP}g", food["serving_g"]))
    if not current:
        options.append((f"100{NBSP}g", 100))
    return chips(options)


def preview(per_100g: logic.Nutrients, grams: float, ctx: DayContext | None, base: float) -> str:
    """What the chosen grams come to, updated live by the script as the grams change."""
    n = logic.round_for_display(logic.scale(per_100g, grams))
    macros = " ".join(
        f'<span class="{cls}">{letter}{NBSP}<span data-n="{key}">{num(n[key], 1)}</span></span>'
        for key, cls, letter in MACROS
    )
    day = ""
    if ctx is not None:
        total = base + n["kcal"]
        goal = f" of {num(ctx.goal)}" if ctx.goal else ""
        warn = ' class="warn"' if ctx.goal and total > ctx.goal else ""
        day = (
            f'<span class="muted">{e(day_title(ctx.day, ctx.today))} after this:'
            f" <span data-day{warn}>{num(total)}{goal} kcal</span></span>"
        )
    return (
        f'<p class="preview small" aria-live="polite"><span class="kcal">'
        f'<span data-n="kcal">{num(n["kcal"])}</span>{NBSP}kcal</span> {macros}'
        + (f"<br>{day}" if day else "")
        + "</p>"
    )


def day_attrs(ctx: DayContext | None, base: float) -> str:
    if ctx is None:
        return ""
    goal = f' data-goal="{num(ctx.goal).replace(",", "")}"' if ctx.goal else ""
    return f' data-base="{round(base, 1)}"{goal}'


def meal_menu(
    ctx: DayContext, exclude: int | None, prompt: str, time: str, *, with_name: bool = False
) -> str:
    """'Add to' / 'Move to': another meal that day, or a new meal at a time (and name)."""
    options = "".join(
        f'<option value="{mid}">{e(title)}</option>' for mid, title in ctx.meals if mid != exclude
    )
    name = (
        f'<label>Meal name<input name="label" maxlength="{LABEL_MAX}"'
        ' placeholder="Optional, like Post-workout"></label>'
        if with_name
        else ""
    )
    return (
        f'<label>{prompt}<select name="to" data-new-meal>'
        f'<option value="new">A new meal</option>{options}</select></label>'
        f'<div class="new-meal"><label>Time<input type="time" name="time" value="{time}">'
        f"</label>{name}</div>"
    )


def time_nudges() -> str:
    return (
        '<div class="chips js-only"><button type="button" class="chip" data-nudge="-15">'
        '&minus;15 min</button><button type="button" class="chip" data-nudge="15">+15 min</button>'
        '<button type="button" class="chip" data-nudge="now">Now</button></div>'
    )


def hhmm(local_iso: str) -> str:
    return datetime.fromisoformat(local_iso).strftime("%H:%M")


def entry_item(
    x: store.EntryRow,
    food,
    single: bool,
    error: tuple[int, str, str] | None,
    ctx: DayContext,
    meal_time: str,
) -> str:
    """One logged food. Tapping the row opens its grams slider (with a live preview), a move
    to another meal, and a guarded delete.

    In a one-item meal the meal header already carries the calories, so the row leaves them out.
    """
    n = logic.round_for_display(x.nutrients)
    brand = f' <span class="muted">{e(x.brand)}</span>' if x.brand else ""
    kcal = "" if single else f'<span class="kcal">{num(n["kcal"])}{NBSP}<small>kcal</small></span>'
    value, problem, is_open = num(x.grams, 1).replace(",", ""), "", ""
    if error and error[0] == x.id:
        value, is_open = error[1], " open"
        problem = f'<p class="error" role="alert">{e(error[2])}</p>'
    extras = extras_line(n)
    per_100g = store.nutrients_of(food)
    base = ctx.kcal - (x.nutrients["kcal"] or 0)
    move = ""
    if len(ctx.meals) > 1 or not single:
        move = (
            '<details class="sub"><summary>Move to another meal…</summary>'
            f'<form class="stack" method="post" action="{PREFIX}/entries/{x.id}/move">'
            + meal_menu(ctx, x.meal_id, "Move to", meal_time)
            + '<button type="submit">Move</button></form></details>'
        )
    return (
        f'<li id="entry-{x.id}"><details{is_open}><summary>'
        f'<div class="row"><span class="name clamp"><span class="sr">Edit </span>{e(x.name)}'
        f"{brand}</span>{kcal}</div>"
        f'<div class="row small muted"><span>{num(x.grams, 1)}{NBSP}g</span>'
        f"<span>{macros_line(n)}</span></div></summary>"
        f'<div class="edit">{problem}'
        + (f'<p class="muted small">{e(x.name)}</p>' if len(x.name) > LONG_NAME else "")
        + f'<form class="stack" method="post" action="{PREFIX}/entries/{x.id}"'
        f' data-per100="{per100_attr(per_100g)}"{day_attrs(ctx, base)}>'
        + amount_field("grams", value, label="Grams", unit="g", top=slider_top(x.grams))
        + grams_chips(food, current=x.grams)
        + preview(per_100g, x.grams, ctx, base)
        + '<button type="submit">Save</button></form>'
        + (f'<p class="muted small">{extras}</p>' if extras else "")
        + move
        + '<details class="confirm"><summary>Delete…</summary>'
        f'<form method="post" action="{PREFIX}/entries/{x.id}/delete">'
        f'<button class="danger" type="submit">Yes, delete {e(x.name)}</button></form>'
        "</details></div></details></li>"
    )


def food_item(row, back: str) -> str:
    """A saved food: name (two lines at most), kcal and macros per 100 g; tap for details
    and to give it a shorter name."""
    per_100g = store.nutrients_of(row)
    n = logic.round_for_display(per_100g)
    meta = []
    if row["brand"]:
        meta.append(e(row["brand"]))
    if row["uses"]:
        meta.append(f"logged {row['uses']}&times;")
    meta.append(
        '<span class="badge ok">Confirmed</span>'
        if row["verified"]
        else '<span class="badge unsure">Unconfirmed</span>'
    )
    more = [f"<p><b>Name:</b> {e(row['name'])}</p>"]
    if row["serving_g"]:
        s = logic.round_for_display(logic.scale(per_100g, row["serving_g"]))
        desc = row["serving_desc"] or f"{num(row['serving_g'], 1)}{NBSP}g"
        more.append(
            f"<p><b>Per serving</b> ({e(desc)}): {num(s['kcal'])} kcal · {macros_line(s)}</p>"
        )
    if extras := extras_line(n):
        more.append(f"<p><b>Per 100 g:</b> {extras}</p>")
    checked = (
        "Confirmed by you" if row["verified"] else "Not confirmed yet; check it against the label"
    )
    more.append(f"<p><b>Source:</b> {SOURCES[row['source']]} · {checked}</p>")
    more.append(
        '<details class="sub"><summary>Rename…</summary>'
        f'<form class="stack" method="post" action="{PREFIX}/foods/{row["id"]}/rename">'
        f'<input type="hidden" name="back" value="{e(back)}">'
        f'<label>Name shown in Chomp<input name="name" value="{e(row["name"])}" required'
        f' maxlength="{FOOD_NAME_MAX}"></label>'
        '<button type="submit">Save name</button></form></details>'
    )
    return (
        f"<li{'' if row['verified'] else ' class="unconfirmed"'}><details><summary>"
        f'<div class="row"><span class="name clamp">{e(row["name"])}</span>'
        f'<span class="kcal">{num(n["kcal"])} <small>kcal</small></span></div>'
        f'<div class="row small muted"><span>{" · ".join(meta)}</span>'
        f"<span>{macros_line(n)}</span></div></summary>"
        f'<div class="more small">{"".join(more)}</div></details></li>'
    )


def with_params(url: str, anchor: str = "", **params: str | int) -> str:
    """A dashboard URL with its one-shot notices (done, undone, problem) replaced."""
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in ("done", "undone", "problem")
    ]
    query += [(k, str(v)) for k, v in params.items()]
    return parts.path + (f"?{urlencode(query)}" if query else "") + (f"#{anchor}" if anchor else "")


# Messages for ?problem=, shown after a rejected edit
PROBLEMS = {
    "when": "Pick a date up to today and a time.",
    "gap": "That time doesn't exist on that day (the clocks changed). Pick another.",
    "grams": f"Grams must be a number more than 0 and at most {MAX_GRAMS:,}.",
    "gone": "That was changed or deleted in the meantime; this is the latest.",
    "undo": "Couldn't undo: those items changed since (maybe from Claude), so nothing changed.",
    "name": "A food needs a name.",
}

# Daily goals: key, name, unit, slider start, slider end, slider step, most allowed
GOALS = (
    ("kcal", "Calories", "kcal", 800, 5000, 25, 20_000),
    ("protein_g", "Protein", "g", 0, 400, 5, 2_000),
    ("carbs_g", "Carbs", "g", 0, 600, 5, 2_000),
    ("fat_g", "Fat", "g", 0, 250, 1, 2_000),
)
GOAL_CLASS = {"kcal": "kcal", "protein_g": "p", "carbs_g": "c", "fat_g": "f"}


def toast(body: str, role: str, kind: str = "") -> str:
    """A banner pinned above the tab bar, so it shows wherever the page lands."""
    close = (
        '<button type="button" class="dismiss js-only" data-dismiss aria-label="Dismiss">'
        "&times;</button>"
    )
    return f'<div class="flash toast{kind}" role="{role}">{body}{close}</div>'


def macro_split(values: dict[str, float | None]) -> str:
    """How protein, carbs and fat goals split the calories (4/4/9 kcal per g), as a bar."""
    mk = logic.macro_kcal(values)
    total = mk["total"] if mk else 0
    hidden = "" if total > 0 else " hidden"
    rects, shares, x = [], [], 0.0
    for key, cls, _ in MACROS:
        share = 100 * mk[key] / total if total > 0 else 0
        rects.append(
            f'<rect data-k="{key}" class="fill {key}" x="{x:.1f}" y="0" width="{share:.1f}"'
            ' height="10"/>'
        )
        shares.append(
            f'<span class="{cls}">{NUTRIENT_LABELS[key].lower()}{NBSP}'
            f'<span data-pct="{key}">{round(share)}%</span></span>'
        )
        x += share
    diff = ""
    if total > 0 and values.get("kcal"):
        gap = round(total - values["kcal"])
        if abs(gap) >= 25:
            diff = f" ({num(abs(gap))} {'over' if gap > 0 else 'under'} your calorie goal)"
    return (
        f'<div class="split"{hidden}><svg class="splitbar" viewBox="0 0 100 10"'
        f' preserveAspectRatio="none" aria-hidden="true">{"".join(rects)}</svg>'
        f'<p class="small muted">Protein, carbs and fat goals add up to about'
        f" <b data-macro-kcal>{num(total)}</b>{NBSP}kcal<span data-kcal-diff>{diff}</span>:"
        f" {', '.join(shares)}.</p></div>"
    )


def goals_form(values: dict[str, str], back: str, error: str = "") -> str:
    # Sets calories to what the macro goals add up to; the script keeps it current
    match = (
        '<div class="chips js-only"><button type="button" class="chip" data-match hidden>'
        "Match protein, carbs and fat</button></div>"
    )
    fields = []
    for key, label, unit, low, top, step, _ in GOALS:
        fields.append(
            f'<div class="goal {GOAL_CLASS[key]}">'
            + amount_field(
                key,
                values[key],
                label=label,
                unit=unit,
                top=top,
                low=low,
                step=step,
                required=False,
                placeholder="No goal",
            )
            + (match if key == "kcal" else "")
            + "</div>"
        )
    parsed = {}
    for key, *_ in GOALS:
        try:
            parsed[key] = float(values[key].replace(",", "")) if values[key] else None
        except ValueError:
            parsed[key] = None
    problem = f'<p class="error" role="alert">{e(error)}</p>' if error else ""
    return (
        f'<form class="card goals stack" method="post" action="{PREFIX}/targets">{problem}'
        f'<input type="hidden" name="back" value="{e(back)}">{"".join(fields)}'
        + macro_split(parsed)
        + '<p class="muted small">Leave a box empty for no goal. Progress bars on Today follow'
        " these, and Claude sees the same goals.</p>"
        '<button type="submit">Save goals</button></form>'
    )


def pick_item(row, ctx: DayContext, meal_id: int | None, q: str, default_time: str) -> str:
    """A food on the Add food page: tap it for a grams slider, a live preview and where to
    log it."""
    keys = row.keys()
    per_100g = store.nutrients_of(row)
    n = logic.round_for_display(per_100g)
    usual = row["default_grams"] if "default_grams" in keys else None
    grams = usual or row["serving_g"] or 100.0
    meta = []
    if "alias" in keys and row["alias"]:
        meta.append(f"“{e(row['alias'])}”")
    if row["brand"]:
        meta.append(e(row["brand"]))
    if not row["verified"]:
        meta.append('<span class="badge unsure">Unconfirmed</span>')
    meta.append("per 100 g")
    where = (
        f'<input type="hidden" name="meal" value="{meal_id}">'
        if meal_id is not None
        else meal_menu(ctx, None, "Add to", default_time, with_name=True)
    )
    return (
        f"<li><details><summary>"
        f'<div class="row"><span class="name clamp">{e(row["name"])}</span>'
        f'<span class="kcal">{num(n["kcal"])} <small>kcal</small></span></div>'
        f'<div class="row small muted"><span>{" · ".join(meta)}</span>'
        f"<span>{macros_line(n)}</span></div></summary>"
        f'<form class="stack edit" method="post" action="{PREFIX}/log"'
        f' data-per100="{per100_attr(per_100g)}"{day_attrs(ctx, ctx.kcal)}>'
        f'<input type="hidden" name="food_id" value="{row["id"]}">'
        f'<input type="hidden" name="date" value="{ctx.day.isoformat()}">'
        f'<input type="hidden" name="q" value="{e(q)}">'
        + (f'<p class="muted small">{e(row["name"])}</p>' if len(row["name"]) > LONG_NAME else "")
        + amount_field(
            "grams",
            num(grams, 1).replace(",", ""),
            label="Grams",
            unit="g",
            top=slider_top(grams, row["serving_g"]),
        )
        + grams_chips(row, usual)
        + preview(per_100g, grams, ctx, ctx.kcal)
        + where
        + '<button type="submit">Add to log</button></form></details></li>'
    )


def template_pick(t, items, ctx: DayContext, meal_id: int | None, default_time: str) -> str:
    """A saved meal on the Add food page: its items and totals, logged in one tap."""
    snaps = [logic.snapshot(store.nutrients_of(i), i["grams"]) for i in items]
    totals = logic.round_for_display(logic.sum_nutrients(snaps))
    lis = "".join(
        f'<li class="row small"><span class="name">{e(i["name"])}</span>'
        f'<span class="muted">{num(i["grams"], 1)}{NBSP}g</span></li>'
        for i in items
    )
    where = (
        f'<input type="hidden" name="meal" value="{meal_id}">'
        if meal_id is not None
        else meal_menu(ctx, None, "Add to", default_time)
    )
    after = ctx.kcal + totals["kcal"]
    goal = f" of {num(ctx.goal)}" if ctx.goal else ""
    return (
        f"<li><details><summary>"
        f'<div class="row"><span class="name">{e(capitalized(t["name"]))}</span>'
        f'<span class="kcal">{num(totals["kcal"])} <small>kcal</small></span></div>'
        f'<div class="row small muted"><span>{len(items)} items</span>'
        f"<span>{macros_line(totals)}</span></div></summary>"
        f'<form class="stack edit" method="post" action="{PREFIX}/log-template">'
        f'<ul class="plain">{lis}</ul>'
        f'<p class="preview small muted">{e(day_title(ctx.day, ctx.today))} after this:'
        f" {num(after)}{goal} kcal</p>"
        f'<input type="hidden" name="template_id" value="{t["id"]}">'
        f'<input type="hidden" name="date" value="{ctx.day.isoformat()}">'
        + where
        + '<button type="submit">Log this meal</button></form></details></li>'
    )


MANIFEST = json.dumps(
    {
        "name": "Chomp",
        "short_name": "Chomp",
        "description": "Your meal log",
        "start_url": PREFIX,
        "scope": PREFIX,
        "display": "standalone",
        "background_color": "#f6f7f5",
        "theme_color": THEME,
        "icons": [
            {
                "src": f"{PREFIX}/static/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": f"{PREFIX}/static/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
)


def add_routes(
    server: MCPServer,
    settings: Settings,
    provider: ChompAuthProvider | None,
    fdc: FDCClient | None = None,
) -> None:
    """Register the dashboard. provider is None in local-only mode (no sign-in). With fdc
    configured, Add food can also search USDA FoodData Central."""
    tz = settings.tz
    sessions = Sessions(settings.db_path)
    secure = bool(settings.public_url)
    css = (STATIC_DIR / "app.css").read_text()
    js = (STATIC_DIR / "app.js").read_text()
    usda = fdc is not None and fdc.enabled

    def connect():
        return closing(db.connect(settings.db_path))

    def today() -> date:
        return datetime.now(ZoneInfo(tz)).date()

    def signed_in(request: Request) -> bool:
        return provider is None or sessions.subject(request.cookies.get(COOKIE, "")) is not None

    def to_login(request: Request) -> RedirectResponse:
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        url = f"{PREFIX}/login?{urlencode({'next': path})}"
        return RedirectResponse(url, status_code=303, headers=SECURITY_HEADERS)

    def same_origin(request: Request) -> bool:
        """Form posts must come from the dashboard itself (CSRF), checked by Origin, falling
        back to Referer. Local-only mode has no session to ride on, so it skips the check."""
        if provider is None:
            return True
        origin = request.headers.get("origin")
        if origin is None and (referer := request.headers.get("referer")):
            parts = urlsplit(referer)
            origin = f"{parts.scheme}://{parts.netloc}"
        return origin == settings.public_url

    def refused(request: Request, what: str) -> Response | None:
        """The response for a post that isn't allowed, or None to go ahead."""
        if not signed_in(request):
            return to_login(request)
        if not same_origin(request):
            return message("Not allowed", f"{what} from the dashboard.", 403)
        return None

    def go(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303, headers=SECURITY_HEADERS)

    def day_url(day: date) -> str:
        return f"{PREFIX}?date={day.isoformat()}"

    def back_to_day(day: date, anchor: str = "", **params: str | int) -> RedirectResponse:
        return go(with_params(day_url(day), anchor, **params))

    def meal_title(meal) -> str:
        when = clock(logic.local_iso(meal["eaten_at"], tz))
        return f"{when} · {capitalized(meal['label'])}" if meal["label"] else when

    def meal_day(meal) -> date:
        return logic.local_date(meal["eaten_at"], tz)

    def load_day(conn, day: date):
        """A day's meals with their entries, totals, targets, and the context its forms use."""
        start, end = logic.local_day_bounds(day, tz)
        meals = [
            (m, store.meal_entries(conn, m["id"])) for m in store.meals_between(conn, start, end)
        ]
        targets = store.get_targets(conn)
        totals = logic.sum_nutrients(x.nutrients for _, entries in meals for x in entries)
        ctx = DayContext(
            day=day,
            today=today(),
            meals=[(m["id"], meal_title(m)) for m, _ in meals],
            kcal=totals["kcal"] or 0.0,
            goal=targets["kcal"],
        )
        return meals, totals, targets, ctx

    def parse_day(value: str | None) -> date:
        """A date from a page or form: up to today, in a sane year range."""
        day = logic.parse_local_date(value, tz)
        if not MIN_YEAR <= day.year <= MAX_YEAR or day > today():
            raise ValueError(day)
        return day

    def parse_when(day_text: str, time_text: str) -> str:
        """A meal time from a date and an HH:MM box. Raises ValueError with a PROBLEMS key."""
        try:
            day = parse_day(day_text)
        except ValueError:
            raise ValueError("when") from None
        m = TIME_RE.fullmatch(time_text.strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError("when")
        try:
            return logic.parse_eaten_at(f"{day.isoformat()}T{m.group(1)}:{m.group(2)}", tz)
        except ValueError:
            raise ValueError("gap") from None

    def notice(conn, params, back: str) -> str:
        """The banner after an edit: what changed and an Undo button, or why it failed."""
        if (undo_id := int_param(params.get("done"))) is not None:
            row = store.get_undo(conn, undo_id)
            if row is None or time.time() - row["created_at"] > UNDO_SHOW_S:
                return ""
            button = ""
            if not row["used"]:
                button = (
                    f'<form method="post" action="{PREFIX}/undo/{undo_id}">'
                    f'<input type="hidden" name="back" value="{e(back)}">'
                    '<button class="undo" type="submit">Undo</button></form>'
                )
            return toast(f"<span>{e(row['summary'])}.</span>{button}", "status")
        if (undo_id := int_param(params.get("undone"))) is not None:
            row = store.get_undo(conn, undo_id)
            if row is not None and row["used"]:
                summary = row["summary"][:1].lower() + row["summary"][1:]
                return toast(f"<span>Undone: {e(summary)}.</span>", "status")
        if problem := PROBLEMS.get(params.get("problem", "")):
            return toast(f"<span>{e(problem)}</span>", "alert", " bad")
        return ""

    # --- static files (no sign-in: the home-screen installer fetches these) ---

    @server.custom_route(f"{PREFIX}/static/app.css", methods=["GET"])
    async def stylesheet(request: Request) -> Response:
        return Response(css, media_type="text/css", headers={"Cache-Control": "max-age=3600"})

    @server.custom_route(f"{PREFIX}/static/app.js", methods=["GET"])
    async def script(request: Request) -> Response:
        return Response(js, media_type="text/javascript", headers={"Cache-Control": "max-age=3600"})

    @server.custom_route(PREFIX + "/static/{name}", methods=["GET"])
    async def icon(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in ICONS:
            return Response("Not found", status_code=404)
        return Response(
            (STATIC_DIR / name).read_bytes(),
            media_type=ICONS[name],
            headers={"Cache-Control": "max-age=86400"},
        )

    @server.custom_route(f"{PREFIX}/manifest.webmanifest", methods=["GET"])
    async def manifest(request: Request) -> Response:
        return Response(MANIFEST, media_type="application/manifest+json")

    # --- sign-in ---

    @server.custom_route(f"{PREFIX}/login", methods=["GET", "POST"])
    async def login(request: Request) -> Response:
        if provider is None:
            return RedirectResponse(PREFIX, status_code=303)
        if request.method == "GET":
            return login_page(safe_next(request.query_params.get("next", PREFIX)))
        form = await request.form()
        next_url = safe_next(str(form.get("next", PREFIX)))
        if not same_origin(request):
            return login_page(next_url, "Sign in from this page.", 403)
        locked = "Too many wrong passwords. Wait 15 minutes, then try again."
        if provider.locked_out():
            return login_page(next_url, locked, 429)
        if not provider.check_password(str(form.get("password", ""))):
            log.warning("dashboard: wrong password")
            if provider.locked_out():
                return login_page(next_url, locked, 429)
            return login_page(next_url, "Wrong password.", 401)
        log.info("dashboard: signed in")
        response = RedirectResponse(next_url, status_code=303, headers=SECURITY_HEADERS)
        response.set_cookie(
            COOKIE,
            sessions.create(),
            max_age=SESSION_TTL_S,
            path=PREFIX,
            secure=secure,
            httponly=True,
            samesite="lax",
        )
        return response

    @server.custom_route(f"{PREFIX}/logout", methods=["POST"])
    async def logout(request: Request) -> Response:
        if not same_origin(request):
            return message("Not allowed", "Sign out from the dashboard.", 403)
        if token := request.cookies.get(COOKIE):
            sessions.delete(token)
        response = RedirectResponse(f"{PREFIX}/login", status_code=303, headers=SECURITY_HEADERS)
        response.delete_cookie(COOKIE, path=PREFIX, secure=secure, httponly=True, samesite="lax")
        return response

    # --- Today (any day) ---

    def meal_tools(meal, entries: list[store.EntryRow], ctx: DayContext) -> str:
        """Under each meal: add to it, and (folded away) edit, combine or delete it."""
        local = logic.local_iso(meal["eaten_at"], tz)
        others = "".join(
            f'<option value="{mid}">{e(title)}</option>'
            for mid, title in ctx.meals
            if mid != meal["id"]
        )
        combine = ""
        if others:
            combine = (
                '<details class="sub"><summary>Combine with another meal…</summary>'
                f'<form class="stack" method="post" action="{PREFIX}/meals/{meal["id"]}/merge">'
                f'<label>Move everything in this meal into<select name="into">{others}</select>'
                '</label><button type="submit">Combine</button></form></details>'
            )
        items = "1 item" if len(entries) == 1 else f"{len(entries)} items"
        add = urlencode({"date": ctx.day.isoformat(), "meal": meal["id"]})
        return (
            '<div class="mealtools">'
            f'<a class="button ghost" href="{PREFIX}/add?{add}">+ Add to this meal</a>'
            '<details class="sub mealedit"><summary>Edit meal…</summary>'
            f'<form class="stack" method="post" action="{PREFIX}/meals/{meal["id"]}">'
            f'<label>Name<input name="label" value="{e(meal["label"] or "")}"'
            f' maxlength="{LABEL_MAX}" placeholder="Optional, like Post-workout"></label>'
            '<div class="pair">'
            f'<label>Time<input type="time" name="time" value="{hhmm(local)}" required></label>'
            f'<label>Date<input type="date" name="date" value="{local[:10]}"'
            f' max="{ctx.today.isoformat()}" required></label></div>'
            + time_nudges()
            + f'<label>Note<input name="note" value="{e(meal["note"] or "")}"'
            f' maxlength="{NOTE_MAX}" placeholder="Optional"></label>'
            '<button type="submit">Save meal</button></form>'
            + combine
            + '<details class="confirm"><summary>Delete meal…</summary>'
            f'<form method="post" action="{PREFIX}/meals/{meal["id"]}/delete">'
            f'<button class="danger" type="submit">Yes, delete this meal ({items})</button>'
            "</form></details></details></div>"
        )

    @server.custom_route(PREFIX, methods=["GET"])
    async def day_view(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        now = today()
        params = request.query_params
        try:
            day = logic.parse_local_date(params.get("date"), tz)
            if not MIN_YEAR <= day.year <= MAX_YEAR:  # keeps day ± 1 and its bounds in range
                raise ValueError(day)
        except ValueError:
            return message("Unknown date", "Dates look like 2026-10-06.", 400, signed_in=True)
        page_url = day_url(day)
        with connect() as conn:
            meals, totals, targets, ctx = load_day(conn, day)
            foods = store.get_foods(conn, (x.food_id for _, entries in meals for x in entries))
            banner = notice(conn, params, page_url)
        progress = logic.progress(totals, targets)
        shown = logic.round_for_display(totals)

        # After a rejected grams edit: the typed value back on its entry, with the reason
        error: tuple[int, str, str] | None = None
        if (failed := int_param(params.get("error"))) is not None:
            raw = params.get("grams", "")[:20]
            try:
                parse_grams(raw)
            except ValueError as err:
                error = (failed, raw, str(err))

        prev_day, next_day = day - timedelta(days=1), day + timedelta(days=1)
        forward = (
            f'<a class="step" href="{day_url(next_day)}" aria-label="Next day">&rsaquo;</a>'
            if day < now
            else '<span class="step off" aria-hidden="true">&rsaquo;</span>'
        )
        head = [
            '<header class="daynav">',
            f'<a class="step" href="{day_url(prev_day)}" aria-label="Previous day">&lsaquo;</a>',
            f'<div class="daytitle"><h1>{e(day_title(day, now))}</h1>'
            f'<p class="muted small">{e(long_date(day, now))}</p></div>',
            forward,
            "</header>",
            week_strip(day, now),
            f'<div class="jump"><form method="get" action="{PREFIX}">'
            f'<input type="date" name="date" value="{day.isoformat()}" max="{now.isoformat()}"'
            ' aria-label="Pick a date" required><button type="submit">Go</button></form>',
            f'<a href="{PREFIX}">Back to today</a>' if day != now else "",
            "</div>",
            banner,
        ]

        goals_url = f"{PREFIX}/targets?{urlencode({'back': page_url})}"
        summary = [
            f'<section class="card summary" aria-label="Totals for {e(day_title(day, now))}">'
        ]
        kcal = progress.get("kcal")
        summary.append(
            f'<div class="row"><p class="hero"><span class="big kcal">{num(shown["kcal"])}</span>'
            f' kcal logged</p><a class="small" href="{goals_url}">Edit goals</a></div>'
        )
        if kcal:
            left = round(kcal["remaining"])
            state = (
                f"{num(left)} remaining"
                if left >= 0
                else f'<span class="warn">{num(-left)} over</span>'
            )
            summary.append(
                meter(kcal["percent"], "kcal", warn_over=True)
                + f'<p class="muted small">{state} · Goal {num(kcal["target"])}</p>'
            )
        macros = []
        for key, cls, _ in MACROS:
            p = progress.get(key)
            eaten = num(shown[key], logic.DISPLAY_PLACES[key])
            if p:
                left = round(p["remaining"], logic.DISPLAY_PLACES[key])
                note = f"{amount(key, abs(left))} {'left' if left >= 0 else 'over'}"
                macros.append(
                    f'<div class="macro"><div class="small {cls}">{NUTRIENT_LABELS[key]}</div>'
                    f"<div><strong>{eaten}</strong>"
                    f' <span class="muted target">/ {amount(key, p["target"])}</span></div>'
                    f"{meter(p['percent'], key)}"
                    f'<div class="muted small">{note}</div></div>'
                )
            else:
                macros.append(
                    f'<div class="macro"><div class="small {cls}">{NUTRIENT_LABELS[key]}</div>'
                    f"<div><strong>{eaten}</strong> g</div></div>"
                )
        summary.append(f'<div class="macros">{"".join(macros)}</div>')
        if extras := extras_line(shown):
            summary.append(
                '<details class="extras"><summary class="small">Fiber, sugar and sodium</summary>'
                f'<p class="muted small">{extras}</p></details>'
            )
        if not progress:
            summary.append(
                f'<p class="muted small">No daily goals yet. <a href="{goals_url}">Set them'
                "</a>, or tell Claude something like “my protein target is 180 g”.</p>"
            )
        summary.append("</section>")
        if day <= now:
            summary.append(
                f'<a class="button add" href="{PREFIX}/add?date={day.isoformat()}">+ Add food</a>'
            )

        cards = []
        for meal, entries in meals:
            single = len(entries) == 1
            meal_totals = logic.round_for_display(logic.sum_nutrients(x.nutrients for x in entries))
            time_value = hhmm(logic.local_iso(meal["eaten_at"], tz))
            items = "".join(
                entry_item(x, foods[x.food_id], single, error, ctx, time_value) for x in entries
            )
            when = clock(logic.local_iso(meal["eaten_at"], tz))
            label = meal["label"] or ""
            label_html = f' <span class="label">{e(capitalized(label))}</span>'
            note = f'<p class="muted small">{e(meal["note"])}</p>' if meal["note"] else ""
            total = (
                ""
                if single
                else '<div class="row small muted total"><span>Meal total</span>'
                f"<span>{macros_line(meal_totals)}</span></div>"
            )
            cards.append(
                f'<section class="card meal" id="meal-{meal["id"]}"><div class="row mealhead">'
                f"<h2>{when}{label_html if label else ''}</h2>"
                f'<span class="kcal">{num(meal_totals["kcal"])}{NBSP}kcal</span></div>{note}'
                f'<ul class="entries">{items}</ul>{total}{meal_tools(meal, entries, ctx)}'
                "</section>"
            )
        if not cards:
            cards.append(
                '<section class="card empty"><p>Nothing logged on this day.</p>'
                '<p class="muted small">Add food above, or tell Claude what you ate.</p>'
                "</section>"
            )
        loaded = clock(datetime.now(ZoneInfo(tz)).isoformat())
        cards.append(
            f'<p class="fresh muted small">Page loaded at {loaded}.'
            f' <a href="{e(page_url)}">Refresh</a></p>'
        )
        return render(
            day_title(day, now),
            "".join(head) + "".join(summary) + "".join(cards),
            "today",
            signed_in=provider is not None,
        )

    # --- Add food ---

    def add_url(day: date, meal_id: int | None, q: str, **params: str | int) -> str:
        query: dict[str, str | int] = {"date": day.isoformat()}
        if meal_id is not None:
            query["meal"] = meal_id
        if q:
            query["q"] = q
        return f"{PREFIX}/add?{urlencode({**query, **params})}"

    @server.custom_route(f"{PREFIX}/add", methods=["GET"])
    async def add_food(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        params = request.query_params
        try:
            day = parse_day(params.get("date"))
        except ValueError:
            return message("Unknown date", "Pick a day up to today.", 400, signed_in=True)
        q = params.get("q", "").strip()[:100]
        meal_id = int_param(params.get("meal"))
        want_usda = bool(params.get("usda")) and bool(q) and usda
        usda_rows, usda_note = [], ""
        with connect() as conn:
            meals, _, _, ctx = load_day(conn, day)
            meal = next((m for m, _ in meals if m["id"] == meal_id), None)
            if meal is None:  # gone, or on another day: log a new meal instead
                meal_id = None
            if q:
                aliases = store.search_aliases(conn, q, ADD_LIMIT)
                rows = store.search_foods(conn, q, ADD_LIMIT)
                names = set(store.templates_matching(conn, q))
                templates = [t for t in store.list_templates(conn) if t[0]["name"] in names]
            else:
                aliases = []
                rows = store.list_used_foods(conn, ADD_LIMIT)
                templates = store.list_templates(conn)
            seen = {r["id"] for r in rows} | {a["id"] for a in aliases}
            if want_usda:
                try:
                    records = await fdc.search_ranked(q, USDA_LIMIT)
                except SourceError as err:
                    log.warning("dashboard: USDA search failed: %s", err)
                    usda_note = "USDA search isn't reachable right now. Try again in a minute."
                    records = []
                for rec in records:
                    try:
                        row, _ = store.cache_record(conn, rec)
                    except ValueError:  # one bad upstream record mustn't sink the search
                        continue
                    if row["id"] not in seen:
                        seen.add(row["id"])
                        usda_rows.append(row)
            back = add_url(day, meal_id, q)
            banner = notice(conn, params, back)

        now_local = datetime.now(ZoneInfo(tz))
        default_time = now_local.strftime("%H:%M") if day == now_local.date() else "12:00"
        target = f"Adding to {meal_title(meal)}" if meal is not None else "Logging a new meal"
        hidden = f'<input type="hidden" name="date" value="{day.isoformat()}">' + (
            f'<input type="hidden" name="meal" value="{meal_id}">' if meal_id is not None else ""
        )
        clear = f'<a class="clear" href="{e(add_url(day, meal_id, ""))}">Clear</a>' if q else ""
        parts = [
            '<header class="daynav">',
            f'<a class="step" href="{day_url(day)}" aria-label="Back to'
            f' {e(day_title(day, ctx.today))}">&lsaquo;</a>',
            f'<div class="daytitle"><h1>Add food</h1><p class="muted small">'
            f"{e(day_title(day, ctx.today))} · {e(target)}</p></div>",
            '<span class="step off" aria-hidden="true"></span></header>',
            banner,
            f'<form class="search" method="get" action="{PREFIX}/add">{hidden}'
            f'<input type="search" name="q" value="{e(q)}" placeholder="Search your foods"'
            ' aria-label="Search foods, nicknames and saved meals">'
            f'<button type="submit">Search</button>{clear}</form>',
        ]
        if templates:
            lis = "".join(
                template_pick(t, items, ctx, meal_id, default_time) for t, items in templates
            )
            parts.append(
                '<section class="card"><h2 class="section">Saved meals</h2>'
                f'<ul class="entries">{lis}</ul></section>'
            )
        picks = [pick_item(a, ctx, meal_id, q, default_time) for a in aliases]
        picks += [
            pick_item(r, ctx, meal_id, q, default_time)
            for r in rows
            if r["id"] not in {a["id"] for a in aliases}
        ]
        if picks:
            heading = "Your matches" if q else "Your foods"
            note = "per 100 g" if q else "most logged first"
            parts.append(
                f'<section class="card"><h2 class="section">{heading}<small>{note}</small></h2>'
                f'<ul class="entries">{"".join(picks)}</ul></section>'
            )
        elif q and not templates:
            parts.append(
                f'<section class="card empty"><p>None of your foods match “{e(q)}”.</p></section>'
            )
        elif not q:
            parts.append(
                '<section class="card empty"><p>No saved foods yet.</p><p class="muted small">'
                "Search for a food to find it in USDA FoodData Central.</p></section>"
            )
        if usda_rows or usda_note:
            lis = "".join(pick_item(r, ctx, meal_id, q, default_time) for r in usda_rows)
            body = f'<ul class="entries">{lis}</ul>' if lis else ""
            if not usda_rows and not usda_note:
                usda_note = "No USDA matches."
            parts.append(
                '<section class="card"><h2 class="section">USDA FoodData Central'
                "<small>per 100 g</small></h2>"
                + (f'<p class="muted small">{e(usda_note)}</p>' if usda_note else "")
                + f"{body}</section>"
            )
        elif want_usda:
            parts.append('<section class="card empty"><p>No USDA matches.</p></section>')
        elif q and usda:
            parts.append(
                f'<a class="button ghost wide" href="{e(add_url(day, meal_id, q, usda=1))}">'
                f"Search USDA for “{e(q)}”</a>"
            )
        elif q:
            parts.append(
                '<p class="muted small center">To find new foods, ask Claude, or set'
                " FDC_API_KEY to search USDA here.</p>"
            )
        return render("Add food", "".join(parts), "today", signed_in=provider is not None)

    @server.custom_route(f"{PREFIX}/log", methods=["POST"])
    async def log_food(request: Request) -> Response:
        if (no := refused(request, "Log food")) is not None:
            return no
        form = await request.form()
        meal_id = int_param(str(form.get("meal", "")))
        q = str(form.get("q", ""))[:100]
        try:
            day = parse_day(str(form.get("date", "")))
        except ValueError:
            day = today()
        retry = add_url(day, meal_id, q)
        try:
            grams = parse_grams(str(form.get("grams", "")).replace(",", "").strip())
        except ValueError:
            return go(with_params(retry, problem="grams"))
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect() as conn, db.transaction(conn):
            try:
                food = store.get_food(conn, int_param(str(form.get("food_id", ""))) or 0)
                into = meal_id if meal_id is not None else int_param(str(form.get("to", "")))
                if into is not None:
                    store.get_meal(conn, into)
            except store.NotFound:
                return go(with_params(retry, problem="gone"))
            items = [(food["id"], grams)]
            if into is not None:
                before = store.capture(conn, meals=[into])
                store.add_to_meal(conn, into, items)
                created = []
            else:
                try:
                    when = parse_when(str(form.get("date", "")), str(form.get("time", "")))
                except ValueError as err:
                    return go(with_params(retry, problem=str(err)))
                label = str(form.get("label", "")).strip()[:LABEL_MAX] or None
                before = store.capture(conn)
                into = store.log_meal(conn, items, when, label)
                created = [into]
            summary = f"Added {num(grams, 1)} g {food['name']}"
            undo_id = store.record_undo(conn, summary, before, meals=created)
            meal = store.get_meal(conn, into)
        log.info("dashboard: logged food %d into meal %d", food["id"], into)
        return back_to_day(meal_day(meal), f"meal-{into}", done=undo_id)

    @server.custom_route(f"{PREFIX}/log-template", methods=["POST"])
    async def log_template(request: Request) -> Response:
        if (no := refused(request, "Log meals")) is not None:
            return no
        form = await request.form()
        meal_id = int_param(str(form.get("meal", "")))
        try:
            day = parse_day(str(form.get("date", "")))
        except ValueError:
            day = today()
        retry = add_url(day, meal_id, "")
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect() as conn, db.transaction(conn):
            try:
                template, rows = store.get_template_by_id(
                    conn, int_param(str(form.get("template_id", ""))) or 0
                )
                into = meal_id if meal_id is not None else int_param(str(form.get("to", "")))
                if into is not None:
                    store.get_meal(conn, into)
            except store.NotFound:
                return go(with_params(retry, problem="gone"))
            items = [(r["food_id"], r["grams"]) for r in rows]
            if into is not None:
                before = store.capture(conn, meals=[into])
                store.add_to_meal(conn, into, items)
                created = []
            else:
                try:
                    when = parse_when(str(form.get("date", "")), str(form.get("time", "")))
                except ValueError as err:
                    return go(with_params(retry, problem=str(err)))
                before = store.capture(conn)
                into = store.log_meal(conn, items, when, template["name"])
                created = [into]
            summary = f"Logged {capitalized(template['name'])}"
            undo_id = store.record_undo(conn, summary, before, meals=created)
            meal = store.get_meal(conn, into)
        log.info("dashboard: logged template %d into meal %d", template["id"], into)
        return back_to_day(meal_day(meal), f"meal-{into}", done=undo_id)

    # --- Entry edits ---

    @server.custom_route(PREFIX + "/entries/{entry_id:int}", methods=["POST"])
    async def edit_entry(request: Request) -> Response:
        if (no := refused(request, "Edit entries")) is not None:
            return no
        form = await request.form()
        raw = str(form.get("grams", "")).replace(",", "").strip()
        entry_id = request.path_params["entry_id"]
        anchor = f"entry-{entry_id}"
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect() as conn, db.transaction(conn):
            try:
                entry = store.get_entry(conn, entry_id)
                day = meal_day(store.get_meal(conn, entry.meal_id))
            except store.NotFound:
                return message("Not found", "That entry no longer exists.", 404, signed_in=True)
            try:
                grams = parse_grams(raw)
            except ValueError:
                return back_to_day(day, anchor, error=entry_id, grams=raw[:20])
            before = store.capture(conn, entries=[entry_id])
            store.update_entry(conn, entry_id, grams, None)
            summary = f"Changed {entry.name} from {num(entry.grams, 1)} g to {num(grams, 1)} g"
            undo_id = store.record_undo(conn, summary, before)
        log.info("dashboard: updated entry %d", entry_id)
        return back_to_day(day, anchor, done=undo_id)

    @server.custom_route(PREFIX + "/entries/{entry_id:int}/delete", methods=["POST"])
    async def remove_entry(request: Request) -> Response:
        if (no := refused(request, "Delete entries")) is not None:
            return no
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect() as conn, db.transaction(conn):
            try:
                entry = store.get_entry(conn, request.path_params["entry_id"])
                day = meal_day(store.get_meal(conn, entry.meal_id))
                before = store.capture(conn, meals=[entry.meal_id])
                _, meal_gone = store.delete_entry(conn, entry.id)
            except store.NotFound:
                return message("Not found", "That entry no longer exists.", 404, signed_in=True)
            undo_id = store.record_undo(conn, f"Deleted {entry.name}", before)
        log.info("dashboard: deleted entry %d", entry.id)
        anchor = "" if meal_gone else f"meal-{entry.meal_id}"
        return back_to_day(day, anchor, done=undo_id)

    @server.custom_route(PREFIX + "/entries/{entry_id:int}/move", methods=["POST"])
    async def move_entry(request: Request) -> Response:
        if (no := refused(request, "Move entries")) is not None:
            return no
        form = await request.form()
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect() as conn, db.transaction(conn):
            try:
                entry = store.get_entry(conn, request.path_params["entry_id"])
                old = store.get_meal(conn, entry.meal_id)
                to = int_param(str(form.get("to", "")))
                if to is not None:
                    store.get_meal(conn, to)
            except store.NotFound:
                gone = "That entry or meal no longer exists."
                return message("Not found", gone, 404, signed_in=True)
            day = meal_day(old)
            if to == old["id"]:
                return back_to_day(day, f"entry-{entry.id}")
            when = None
            if to is None:
                try:
                    when = parse_when(day.isoformat(), str(form.get("time", "")))
                except ValueError as err:
                    return back_to_day(day, f"entry-{entry.id}", problem=str(err))
            before = store.capture(conn, meals=[old["id"], *([to] if to else [])])
            new_meal, _ = store.move_entry(conn, entry.id, to, when)
            meal = store.get_meal(conn, new_meal)
            summary = f"Moved {entry.name} to {meal_title(meal)}"
            undo_id = store.record_undo(
                conn, summary, before, meals=[new_meal] if to is None else []
            )
        log.info("dashboard: moved entry %d to meal %d", entry.id, new_meal)
        return back_to_day(meal_day(meal), f"entry-{entry.id}", done=undo_id)

    # --- Meal edits ---

    @server.custom_route(PREFIX + "/meals/{meal_id:int}", methods=["POST"])
    async def edit_meal(request: Request) -> Response:
        if (no := refused(request, "Edit meals")) is not None:
            return no
        form = await request.form()
        meal_id = request.path_params["meal_id"]
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect() as conn, db.transaction(conn):
            try:
                meal = store.get_meal(conn, meal_id)
            except store.NotFound:
                return message("Not found", "That meal no longer exists.", 404, signed_in=True)
            try:
                when = parse_when(str(form.get("date", "")), str(form.get("time", "")))
            except ValueError as err:
                return back_to_day(meal_day(meal), f"meal-{meal_id}", problem=str(err))
            before = store.capture(conn, meals=[meal_id])
            label = str(form.get("label", ""))[:LABEL_MAX]
            note = str(form.get("note", ""))[:NOTE_MAX]
            after = store.update_meal(conn, meal_id, when, label, note)
            undo_id = store.record_undo(conn, f"Updated the {meal_title(meal)} meal", before)
        log.info("dashboard: updated meal %d", meal_id)
        return back_to_day(meal_day(after), f"meal-{meal_id}", done=undo_id)

    @server.custom_route(PREFIX + "/meals/{meal_id:int}/merge", methods=["POST"])
    async def merge_meal(request: Request) -> Response:
        if (no := refused(request, "Combine meals")) is not None:
            return no
        form = await request.form()
        meal_id = request.path_params["meal_id"]
        into = int_param(str(form.get("into", "")))
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect() as conn, db.transaction(conn):
            try:
                meal = store.get_meal(conn, meal_id)
                target = store.get_meal(conn, into if into is not None else 0)
            except store.NotFound:
                return message("Not found", "That meal no longer exists.", 404, signed_in=True)
            if target["id"] == meal_id:
                return back_to_day(meal_day(meal), f"meal-{meal_id}")
            before = store.capture(conn, meals=[meal_id, target["id"]])
            store.merge_meals(conn, meal_id, target["id"])
            summary = f"Combined {meal_title(meal)} into {meal_title(target)}"
            undo_id = store.record_undo(conn, summary, before)
        log.info("dashboard: merged meal %d into %d", meal_id, target["id"])
        return back_to_day(meal_day(target), f"meal-{target['id']}", done=undo_id)

    @server.custom_route(PREFIX + "/meals/{meal_id:int}/delete", methods=["POST"])
    async def remove_meal(request: Request) -> Response:
        if (no := refused(request, "Delete meals")) is not None:
            return no
        meal_id = request.path_params["meal_id"]
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect() as conn, db.transaction(conn):
            try:
                meal = store.get_meal(conn, meal_id)
            except store.NotFound:
                return message("Not found", "That meal no longer exists.", 404, signed_in=True)
            before = store.capture(conn, meals=[meal_id])
            store.delete_meal(conn, meal_id)
            undo_id = store.record_undo(conn, f"Deleted the {meal_title(meal)} meal", before)
        log.info("dashboard: deleted meal %d", meal_id)
        return back_to_day(meal_day(meal), done=undo_id)

    # --- Goals ---

    @server.custom_route(f"{PREFIX}/targets", methods=["GET", "POST"])
    async def goals(request: Request) -> Response:
        if request.method == "GET":
            if not signed_in(request):
                return to_login(request)
            back = safe_next(request.query_params.get("back", PREFIX))
            with connect() as conn:
                current = store.get_targets(conn)
            values = {
                k: num(v, 1).replace(",", "") if v is not None else "" for k, v in current.items()
            }
            return goals_page(values, back)
        if (no := refused(request, "Change goals")) is not None:
            return no
        form = await request.form()
        back = safe_next(str(form.get("back", PREFIX)))
        values = {k: str(form.get(k, "")).replace(",", "").strip()[:12] for k, *_ in GOALS}
        changes: dict[str, float | None] = {}
        for key, label, unit, *_, most in GOALS:
            raw = values[key]
            if not raw:
                changes[key] = None
                continue
            try:
                value = float(raw)
                if not math.isfinite(value) or value <= 0 or value > most:
                    raise ValueError(value)
            except ValueError:
                problem = f"{label} must be a number more than 0 and at most {most:,} {unit}."
                return goals_page(values, back, problem, 400)
            changes[key] = value
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect() as conn, db.transaction(conn):
            before = store.capture(conn, targets=True)
            store.set_targets(conn, changes)
            undo_id = store.record_undo(conn, "Updated your daily goals", before)
        log.info("dashboard: updated targets")
        return go(with_params(back, done=undo_id))

    def goals_page(values: dict[str, str], back: str, error: str = "", status: int = 200):
        body = (
            '<header class="daynav">'
            f'<a class="step" href="{e(back)}" aria-label="Back">&lsaquo;</a>'
            '<div class="daytitle"><h1>Daily goals</h1></div>'
            '<span class="step off" aria-hidden="true"></span></header>'
            + goals_form(values, back, error)
        )
        return render("Daily goals", body, "today", signed_in=provider is not None, status=status)

    # --- Undo ---

    @server.custom_route(PREFIX + "/undo/{undo_id:int}", methods=["POST"])
    async def undo(request: Request) -> Response:
        if (no := refused(request, "Undo")) is not None:
            return no
        form = await request.form()
        back = safe_next(str(form.get("back", PREFIX)))
        undo_id = request.path_params["undo_id"]
        with connect() as conn:
            try:
                store.undo(conn, undo_id)
            except store.NotFound:
                return go(with_params(back))
            except store.UndoConflict:
                return go(with_params(back, problem="undo"))
        log.info("dashboard: undid action %d", undo_id)
        return go(with_params(back, undone=undo_id))

    # --- Food names ---

    @server.custom_route(PREFIX + "/foods/{food_id:int}/rename", methods=["POST"])
    async def rename_food(request: Request) -> Response:
        if (no := refused(request, "Rename foods")) is not None:
            return no
        form = await request.form()
        back = safe_next(str(form.get("back", f"{PREFIX}/foods")))
        name = " ".join(str(form.get("name", "")).split())[:FOOD_NAME_MAX]
        food_id = request.path_params["food_id"]
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect() as conn, db.transaction(conn):
            try:
                old = store.get_food(conn, food_id)
            except store.NotFound:
                return message("Not found", "That food no longer exists.", 404, signed_in=True)
            if not name:
                return go(with_params(back, problem="name"))
            if name == old["name"]:
                return go(with_params(back))
            before = store.capture(conn, foods=[food_id])
            store.rename_food(conn, food_id, name)
            undo_id = store.record_undo(conn, f"Renamed {old['name']} to {name}", before)
        log.info("dashboard: renamed food %d", food_id)
        return go(with_params(back, done=undo_id))

    # --- History ---

    @server.custom_route(f"{PREFIX}/history", methods=["GET"])
    async def history(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        try:
            span = int(request.query_params.get("days", "7"))
        except ValueError:
            span = 7
        if span not in HISTORY_RANGES:
            span = 7
        last = today()
        first = last - timedelta(days=span - 1)
        days = logic.date_range(first, last)
        lo, _ = logic.local_day_bounds(first, tz)
        _, hi = logic.local_day_bounds(last, tz)
        with connect() as conn:
            by_day = logic.totals_by_day(store.entries_between(conn, lo, hi), tz)
            targets = store.get_targets(conn)
        totals = logic.sum_nutrients(by_day.values())
        average = logic.round_for_display(logic.average(totals, len(by_day))) if by_day else None

        tabs = "".join(
            f'<a href="{PREFIX}/history?days={n}"{" aria-current=page" if n == span else ""}>'
            f"{n} days</a>"
            for n in HISTORY_RANGES
        )
        parts = [f'<h1>History</h1><nav class="segment">{tabs}</nav>']
        if average is None:
            parts.append(
                '<section class="card empty"><p>Nothing logged in this period.</p></section>'
            )
        else:
            stats = "".join(
                f'<div><div class="big {k}">{num(average[k], logic.DISPLAY_PLACES[k])}</div>'
                f'<div class="muted small">{NUTRIENT_LABELS[k]}'
                f"{' kcal' if k == 'kcal' else ' g'}</div></div>"
                for k in logic.CORE
            )
            parts.append(
                f'<section class="card"><h2>Daily average</h2><div class="grid4">{stats}</div>'
                f'<p class="muted small">Over the {len(by_day)} of {span} days with entries.</p>'
                "</section>"
            )
            for key in ("kcal", "protein_g"):
                points = [(d, (by_day.get(d) or {}).get(key)) for d in days]
                title = f"{NUTRIENT_LABELS[key]} per day"
                parts.append(
                    f'<section class="card"><h2>{title}</h2>'
                    f"{bar_chart(points, targets.get(key), key, title)}</section>"
                )
            rows = []
            for d in reversed(days):
                n = by_day.get(d)
                if n is None:
                    continue
                shown = logic.round_for_display(n)
                rows.append(
                    f'<li><a class="row" href="{PREFIX}?date={d.isoformat()}">'
                    f"<span>{e(day_title(d, last))}</span>"
                    f'<span><span class="kcal">{num(shown["kcal"])}</span>'
                    f' <span class="small muted">{macros_line(shown)}</span></span></a></li>'
                )
            parts.append(
                f'<section class="card"><h2>Days</h2><ul class="days">{"".join(rows)}'
                "</ul></section>"
            )
        return render("History", "".join(parts), "history", signed_in=provider is not None)

    # --- Foods, nicknames and templates ---

    @server.custom_route(f"{PREFIX}/foods", methods=["GET"])
    async def foods(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        q = request.query_params.get("q", "").strip()[:100]
        back = f"{PREFIX}/foods" + (f"?{urlencode({'q': q})}" if q else "")
        with connect() as conn:
            banner = notice(conn, request.query_params, back)
            if q:
                rows = store.search_foods(conn, q, FOODS_LIMIT)
                nicknames = store.search_aliases(conn, q, FOODS_LIMIT)
                names = set(store.templates_matching(conn, q))
                templates = [t for t in store.list_templates(conn) if t[0]["name"] in names]
            else:
                rows = store.list_used_foods(conn, FOODS_LIMIT)
                nicknames = store.list_aliases(conn)
                templates = store.list_templates(conn)

        clear = f'<a class="clear" href="{PREFIX}/foods">Clear</a>' if q else ""
        parts = [
            "<h1>Foods</h1>",
            banner,
            f'<form class="search" method="get" action="{PREFIX}/foods">'
            f'<input type="search" name="q" value="{e(q)}" placeholder="Foods, nicknames, meals"'
            ' aria-label="Search foods, nicknames and saved meals">'
            f'<button type="submit">Search</button>{clear}</form>',
        ]
        if q and not (rows or nicknames or templates):
            parts.append(f'<section class="card empty"><p>Nothing matches “{e(q)}”.</p></section>')

        # Reusable meals and nicknames first, so they're reachable without scrolling every food
        if templates:
            cards = []
            for t, t_items in templates:
                snaps = [logic.snapshot(store.nutrients_of(i), i["grams"]) for i in t_items]
                t_totals = logic.round_for_display(logic.sum_nutrients(snaps))
                lis = "".join(
                    f'<li class="row small"><span class="name">{e(i["name"])}</span>'
                    f'<span class="muted">{num(i["grams"], 1)}{NBSP}g</span></li>'
                    for i in t_items
                )
                cards.append(
                    f'<div class="template"><div class="row">'
                    f"<strong>{e(capitalized(t['name']))}</strong>"
                    f'<span class="kcal">{num(t_totals["kcal"])}{NBSP}kcal</span></div>'
                    f'<ul class="plain">{lis}</ul><div class="small muted">'
                    f"{macros_line(t_totals)}</div></div>"
                )
            parts.append(
                '<section class="card"><h2 class="section">Saved meals</h2>'
                f"{''.join(cards)}</section>"
            )
        if nicknames:
            lis = "".join(
                f'<li class="row"><span>“{e(a["alias"])}”</span><span class="muted name">'
                f"{e(a['name'])}{grams_note(a['default_grams'])}</span></li>"
                for a in nicknames
            )
            parts.append(
                '<section class="card"><h2 class="section">Nicknames</h2>'
                f'<ul class="plain">{lis}</ul></section>'
            )
        if rows:
            heading = "Foods" if q else "Your foods"
            note = "most logged first, " if not q else ""
            parts.append(
                f'<section class="card"><h2 class="section">{heading}'
                f"<small>{note}per 100 g</small></h2>"
                f'<ul class="entries">{"".join(food_item(r, back) for r in rows)}</ul></section>'
            )
        elif not q:
            parts.append('<section class="card empty"><p>No saved foods yet.</p></section>')
        return render("Foods", "".join(parts), "foods", signed_in=provider is not None)
