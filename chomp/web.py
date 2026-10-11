"""Web dashboard at /app: today's meals against targets, history charts, and saved foods.

Server-rendered HTML on the same Starlette app as /mcp: charts are inline SVG and every
edit (log food, change grams, move an entry, edit or combine meals, rename a food, set
goals, undo) is a plain form post that goes through store.py. A small script served from
here (static/app.js, no third-party code) adds sliders, quick picks and live previews; the
pages work without it. New food (#59) also loads static/scan.js and the vendored ZXing
reader for camera barcode scans, and reads label photos with tesseract (chomp/label.py).
Phones can add it to the home screen (web app manifest at /app/manifest.webmanifest).

Undo: each edit stores the rows it touched before and after (undo_actions, migration 0005)
and the next page offers Undo, which only applies while those rows are unchanged.

Sign-in: with PUBLIC_URL set, a password page (each user's own password, sharing the consent
page's lockout; see chomp/users.py) starts a 30-day session for that user, and every page
shows only their log. The cookie holds a random token; only its SHA-256 hash is stored
(web_sessions). POSTs must come from the dashboard's own origin. In local-only mode the
dashboard needs no sign-in, like /mcp, and shows the owner's log.

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

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from chomp import db, logic, store, users
from chomp import label as label_reader
from chomp.auth import ChompAuthProvider
from chomp.barcode import lookup as barcode_lookup
from chomp.config import Settings
from chomp.sources import SourceError
from chomp.sources import gtin as gtins
from chomp.sources.fdc import FDCClient
from chomp.sources.off import OFFClient

log = logging.getLogger("chomp.web")

PREFIX = "/app"
COOKIE = "chomp_session"
SESSION_TTL_S = 30 * 86400
HISTORY_RANGES = (7, 30, 90)
FOODS_LIMIT = 200
MAX_GRAMS = 10_000
LABEL_MAX, NOTE_MAX, FOOD_NAME_MAX = 60, 200, 120
PORTION_NAME_MAX = 40
ADD_LIMIT = 30  # foods listed on the Add food page
USDA_LIMIT = 10
UNDO_SHOW_S = 30 * 60  # how long a confirmation keeps its Undo button
TIME_RE = re.compile(r"(\d{2}):(\d{2})(:\d{2})?")
MIN_YEAR, MAX_YEAR = 1900, 2999
STATIC_DIR = Path(__file__).resolve().parent / "static"
# Served as is from static/. zxing.min.js is the vendored ZXing barcode reader
# (@zxing/library 0.21.3, Apache-2.0, LICENSE-zxing.txt), used by scan.js on New food.
STATIC = {
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "scan.js": "text/javascript",
    "zxing.min.js": "text/javascript",
}
THEME = "#1f7a4d"
# In the stylesheet and script URLs, so a phone's cached copy is replaced as soon as they change
CSS_VERSION = hashlib.sha256((STATIC_DIR / "app.css").read_bytes()).hexdigest()[:10]
JS_VERSION = hashlib.sha256((STATIC_DIR / "app.js").read_bytes()).hexdigest()[:10]
STATIC_VERSIONS = {
    name: hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:10] for name in STATIC
}

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self';"
        " manifest-src 'self'; media-src 'self' blob: mediastream:;"
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
    "sat_fat_g": "Saturated fat",
    "trans_fat_g": "Trans fat",
    "poly_fat_g": "Polyunsaturated fat",
    "mono_fat_g": "Monounsaturated fat",
    "cholesterol_mg": "Cholesterol",
    "added_sugar_g": "Added sugars",
    "sugar_alcohol_g": "Sugar alcohols",
    "vitamin_d_mcg": "Vitamin D",
    "calcium_mg": "Calcium",
    "iron_mg": "Iron",
    "potassium_mg": "Potassium",
    "vitamin_a_mcg": "Vitamin A",
    "vitamin_c_mg": "Vitamin C",
    "vitamin_e_mg": "Vitamin E",
    "vitamin_k_mcg": "Vitamin K",
    "thiamin_mg": "Thiamin (B1)",
    "riboflavin_mg": "Riboflavin (B2)",
    "niacin_mg": "Niacin (B3)",
    "vitamin_b6_mg": "Vitamin B6",
    "folate_mcg": "Folate",
    "vitamin_b12_mcg": "Vitamin B12",
    "phosphorus_mg": "Phosphorus",
    "magnesium_mg": "Magnesium",
    "zinc_mg": "Zinc",
}
UNITS = {
    "kcal": "kcal",
    "protein_g": "g",
    "carbs_g": "g",
    "fat_g": "g",
    "fiber_g": "g",
    "sugar_g": "g",
    "sodium_mg": "mg",
    **{k: logic.unit_of(k) for k in logic.DETAILS + logic.MICROS},
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

    def create(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._connect() as conn, db.transaction(conn):
            conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
            conn.execute(
                "INSERT INTO web_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (_hash(token), user_id, now + SESSION_TTL_S),
            )
        return token

    def user(self, token: str) -> int | None:
        """The user a session cookie belongs to, or None if it is unknown or expired."""
        if not token:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM web_sessions WHERE token_hash = ? AND expires_at > ?",
                (_hash(token), int(time.time())),
            ).fetchone()
        return row["user_id"] if row else None

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


def micros_line(n: logic.Nutrients) -> str:
    """Vitamins and minerals, where known: 'Vitamin D 2 mcg · Iron 1.8 mg'."""
    return " · ".join(
        f"{NUTRIENT_LABELS[k]} {amount(k, n[k])}" for k in logic.MICROS if n.get(k) is not None
    )


def micros_table(n: logic.Nutrients, partial: list[str]) -> str:
    """A day's vitamins and minerals with their share of the Daily Value. Ones only some
    foods report are starred: the real amount is higher."""
    rows = []
    for k in logic.MICROS:
        if n.get(k) is None:
            continue
        star = '<span aria-hidden="true">*</span><span class="sr"> (partial)</span>'
        rows.append(
            f'<tr><th scope="row">{NUTRIENT_LABELS[k]}</th>'
            f"<td>{amount(k, n[k])}{star if k in partial else ''}</td>"
            f'<td class="muted">{num(logic.percent_dv(k, n[k]))}%</td></tr>'
        )
    if not rows:
        return ""
    note = (
        '<p class="muted small">* Only some of these foods list it, so the real amount is'
        " higher.</p>"
        if partial
        else ""
    )
    return (
        '<details class="extras"><summary class="small">Vitamins and minerals</summary>'
        '<table class="micros small"><thead><tr><th scope="col">Nutrient</th>'
        '<th scope="col">Amount</th><th scope="col">Daily Value</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>{note}</details>"
    )


# The day breakdown's nutrition facts, in label order: (key, indented under the row above)
FACTS = (
    ("kcal", False),
    ("fat_g", False),
    ("sat_fat_g", True),
    ("trans_fat_g", True),
    ("poly_fat_g", True),
    ("mono_fat_g", True),
    ("cholesterol_mg", False),
    ("sodium_mg", False),
    ("carbs_g", False),
    ("fiber_g", True),
    ("sugar_g", True),
    ("added_sugar_g", True),
    ("sugar_alcohol_g", True),
    ("net_carbs", True),
    ("protein_g", False),
)
MACRO_CLASS = {key: cls for key, cls, _ in MACROS}


def top_list(top: list[tuple[str, bool, float]], key: str) -> str:
    """A numbered list of the foods that gave the most of `key`: (name, confirmed, amount).
    Unconfirmed foods get an amber dot."""
    items = "".join(
        '<li><span class="name">'
        + (
            ""
            if confirmed
            else '<span class="dot unsure" aria-hidden="true"></span>'
            '<span class="sr">Unconfirmed: </span>'
        )
        + f"{e(name)}</span><span>{amount(key, value)}</span></li>"
        for name, confirmed, value in top
    )
    return f'<ol class="top">{items}</ol>'


def meter(percent: float | None, kind: str, *, warn_over: bool = False, tone: str = "") -> str:
    """A progress bar. Only warn_over bars turn red past 100% (going over a protein target
    isn't a mistake); tone ("caution" for amber) overrides that color."""
    width = 0.0 if percent is None else max(0.0, min(percent, 100.0))
    over = " over" if warn_over and percent is not None and percent > 100 else ""
    if tone:
        over = f" {tone}"
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
<script src="{p}/static/app.js?v={js_version}" defer></script>{scripts}
<title>{title}</title></head><body{body_class}>"""

NAV = (("today", "", "Today"), ("history", "/history", "History"), ("foods", "/foods", "Foods"))


def render(
    title: str,
    body: str,
    active: str | None = None,
    *,
    signed_in: bool = False,
    user: str = "",
    status: int = 200,
    scripts: tuple[str, ...] = (),
) -> HTMLResponse:
    """A dashboard page. `scripts` are extra static scripts (STATIC) this page loads."""
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
            + (f'<span class="muted">{e(user)} · </span>' if user else "")
            + '<button class="link" type="submit">Sign out</button></form></footer>'
        )
    page = (
        HEAD.format(
            theme=THEME,
            p=PREFIX,
            css_version=CSS_VERSION,
            js_version=JS_VERSION,
            title=e(f"{title} · Chomp"),
            body_class=' class="has-tabs"' if nav else "",
            scripts="".join(
                f'<script src="{PREFIX}/static/{name}?v={STATIC_VERSIONS[name]}" defer></script>'
                for name in scripts
            ),
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
<p class="muted">Your own Chomp password, the one you use to connect Claude.</p></div>"""


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


def parse_grams(value: str) -> float:
    try:
        grams = float(value)
    except ValueError:
        raise ValueError("Enter the grams as a number, like 150.") from None
    if not math.isfinite(grams) or grams <= 0 or grams > MAX_GRAMS:
        raise ValueError(f"Grams must be more than 0 and at most {MAX_GRAMS:,}.")
    return grams


def parse_servings(value: str, serving_g: float) -> float:
    """A number of servings (#80), as the grams it comes to."""
    try:
        servings = float(value)
    except ValueError:
        raise ValueError("Enter the servings as a number, like 2 or 1.5.") from None
    if not math.isfinite(servings) or servings <= 0 or servings * serving_g > MAX_GRAMS:
        raise ValueError(f"Servings must be more than 0 and at most {MAX_GRAMS:,} g in all.")
    return servings * serving_g


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


def whole_servings(grams: float, serving_g: float) -> float | None:
    """Grams as a number of servings when that's a whole or half number (341.6 g of an
    85.4 g serving is 4), else None."""
    count = round(grams / serving_g * 2) / 2
    return count if count >= 0.5 and abs(count * serving_g - grams) < 0.05 else None


def count_text(count: float) -> str:
    """A number of servings: up to 2 decimals, no trailing zeros (4, 1.5, 1.76)."""
    return num(count, 2).rstrip("0").rstrip(".")


def servings_text(count: float) -> str:
    return f"{count_text(count)} serving" + ("" if count_text(count) == "1" else "s")


def amount_picker(food, grams: float, top: int, *, by: str = "", typed: str = "") -> str:
    """How much to log: the grams box, and for a food with a serving size a Servings / Grams
    toggle (#80) with a minus / plus stepper in half servings. Servings is picked when the amount is
    a whole or half number of servings. Both boxes are submitted and the toggle says which
    counts; the script keeps them showing the same amount. by and typed put a rejected value
    back the way it was typed."""
    value = num(grams, 1).replace(",", "")
    serving = food["serving_g"]
    if not serving:
        return amount_field("grams", typed if typed else value, label="Grams", unit="g", top=top)
    count = whole_servings(grams, serving)
    by = by if by in ("servings", "grams") else "servings" if count else "grams"
    servings = count_text(count or grams / serving).replace(",", "")
    if by == "servings" and typed:
        servings = typed
    elif typed:
        value = typed

    def option(name: str, label: str) -> str:
        checked = " checked" if by == name else ""
        return f'<label><input type="radio" name="by" value="{name}"{checked}>{label}</label>'

    size = repr(float(serving))  # unrounded: the script's math must match the server's
    return (
        '<div class="by" role="radiogroup" aria-label="Amount in">'
        + option("servings", "Servings")
        + option("grams", "Grams")
        + "</div>"
        f'<div class="amount servings" data-serving="{size}">'
        '<span class="lead">Servings</span><span class="field">'
        '<button type="button" class="stepper js-only" data-step="-1"'
        ' aria-label="Half a serving less">&minus;</button>'
        f'<input name="servings" data-servings inputmode="decimal" autocomplete="off"'
        f' aria-label="Servings" value="{e(servings)}">'
        '<button type="button" class="stepper js-only" data-step="1"'
        ' aria-label="Half a serving more">+</button>'
        f'<span class="unit">&times; {num(serving, 1)}{NBSP}g</span></span></div>'
        + amount_field("grams", value, label="Grams", unit="g", top=top, required=False)
    )


def chips(options: list[tuple[str, float] | tuple[str, float, bool]]) -> str:
    """Quick picks that fill in the grams box (script only). A pick marked estimated (an
    estimated portion, #75) logs its entry as estimated."""
    if not options:
        return ""
    seen, buttons = set(), []
    for label, grams, *estimated in options:
        value = num(grams, 1).replace(",", "")
        if value in seen:
            continue
        seen.add(value)
        flag = ' data-estimated="1"' if any(estimated) else ""
        buttons.append(
            f'<button type="button" class="chip" data-set="{value}"{flag}>{label}</button>'
        )
    return f'<div class="chips js-only">{"".join(buttons)}</div>'


def grams_text(grams: float, estimated: bool) -> str:
    """Grams as shown on an entry or portion; an estimated amount (#75) gets a muted "~"."""
    if not estimated:
        return f"{num(grams, 1)}{NBSP}g"
    return (
        f'<span class="approx" title="Estimated amount">~</span>{num(grams, 1)}{NBSP}g'
        '<span class="sr"> (estimated)</span>'
    )


def grams_chips(food, portions=(), current: float | None = None) -> str:
    options = []
    if current:
        options += [(f"½ · {num(current / 2, 1)}{NBSP}g", current / 2)]
        options += [(f"2&times; · {num(current * 2, 1)}{NBSP}g", current * 2)]
    for p in portions:
        approx = "~" if p["estimated"] else ""
        label = f"1 {e(p['name'])} · {approx}{num(p['grams'], 1)}{NBSP}g"
        options.append((label, p["grams"], bool(p["estimated"])))
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
    by, typed, problem, is_open = "", "", "", ""
    if error and error[0] == x.id:
        by, typed, is_open = error[1], error[2], " open"
        try:
            if by == "servings" and food["serving_g"]:
                parse_servings(typed, food["serving_g"])
            else:
                parse_grams(typed)
        except ValueError as err:
            problem = f'<p class="error" role="alert">{e(str(err))}</p>'
    extras = " · ".join(filter(None, (extras_line(n), micros_line(n))))
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
        f'<div class="row small muted"><span>{grams_text(x.grams, x.estimated)}</span>'
        f"<span>{macros_line(n)}</span></div></summary>"
        f'<div class="edit">{problem}'
        + (f'<p class="muted small">{e(x.name)}</p>' if len(x.name) > LONG_NAME else "")
        + f'<form class="stack" method="post" action="{PREFIX}/entries/{x.id}"'
        f' data-per100="{per100_attr(per_100g)}"{day_attrs(ctx, base)}>'
        + amount_picker(food, x.grams, slider_top(x.grams), by=by, typed=typed)
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


# Foods page filters: view -> chip label, and the note beside the food list's heading
FOOD_VIEWS = {
    "most": ("Most used", "most logged first"),
    "recent": ("Recent", "last logged first"),
    "new": ("Newest", "newest added first"),
    "unconfirmed": ("Unconfirmed", "not yet checked against a label"),
    "confirmed": ("Confirmed", "checked against a label or scanned"),
}


def fold(name: str, heading: str, body: str, note: str = "", *, opened: bool = True) -> str:
    """A card whose contents fold away under its heading. The script remembers which ones
    were folded or opened (data-fold); without it each starts as `opened` says."""
    small = f"<small>{note}</small>" if note else ""
    state = " open" if opened else ""
    return (
        f'<section class="card"><details class="fold" data-fold="{name}"{state}><summary>'
        f'<h2 class="section">{heading}{small}</h2></summary>{body}</details></section>'
    )


def portion_item(food_id: int, p, back: str) -> str:
    """One of the person's portions of a food (#75), in the food's details: swipe it left to
    delete it, press and hold it to change its name or grams. Without the script a tap opens
    it, Delete included."""
    action = f"{PREFIX}/foods/{food_id}/portions"
    hidden = (
        f'<input type="hidden" name="back" value="{e(back)}">'
        f'<input type="hidden" name="old" value="{e(p["name"])}">'
    )
    delete = (
        f'<form class="swipe-action js-only" method="post" action="{action}/delete">{hidden}'
        f'<button type="submit" aria-label="Delete the {e(p["name"])} portion">Delete</button>'
        "</form>"
    )
    hint = (
        '<p class="small muted">Estimated, not weighed. Saving a weight here makes it exact.</p>'
        if p["estimated"]
        else ""
    )
    return (
        f'<li class="swipe">{delete}<div class="swipe-body" data-swipe><details><summary>'
        f'<div class="row"><span class="name">{e(p["name"])}</span>'
        f'<span class="muted">{grams_text(p["grams"], p["estimated"])}</span></div></summary>'
        f'<form class="stack" method="post" action="{action}">{hidden}{hint}'
        f'<label>Name<input name="name" value="{e(p["name"])}" required'
        f' maxlength="{PORTION_NAME_MAX}"></label>'
        f'<label>Grams in one<input name="grams" type="number" inputmode="decimal"'
        f' step="any" min="0.1" max="{MAX_GRAMS}" value="{num(p["grams"], 1).replace(",", "")}"'
        " required></label>"
        '<button type="submit">Save portion</button></form>'
        f'<form class="no-js" method="post" action="{action}/delete">{hidden}'
        f'<button class="danger" type="submit">Delete {e(p["name"])}</button></form>'
        "</details></div></li>"
    )


def portions_block(row, portions, back: str) -> str:
    """The person's portions of a food, and a form to add one."""
    lis = "".join(portion_item(row["id"], p, back) for p in portions)
    listed = f'<ul class="entries portions">{lis}</ul>' if lis else ""
    none = "" if lis else '<p class="muted">None yet. Claude learns them as you log.</p>'
    return (
        f'<div class="portions-block"><p><b>Your portions</b></p>{listed}{none}'
        '<details class="sub add-portion"><summary>+ Add portion</summary>'
        f'<form class="stack" method="post" action="{PREFIX}/foods/{row["id"]}/portions">'
        f'<input type="hidden" name="back" value="{e(back)}">'
        f'<label>Name<input name="name" placeholder="handful, slice, scoop" required'
        f' maxlength="{PORTION_NAME_MAX}"></label>'
        f'<label>Grams in one<input name="grams" type="number" inputmode="decimal"'
        f' step="any" min="0.1" max="{MAX_GRAMS}" required></label>'
        '<button type="submit">Add portion</button></form></details></div>'
    )


def food_url(food_id: int, back: str) -> str:
    """A food's details page (#72), with the list to go back to."""
    return f"{PREFIX}/foods/{food_id}?{urlencode({'back': back})}"


def food_item(row, back: str, last_logged: str = "", log_url: str = "") -> str:
    """A saved food: name (two lines at most), kcal and macros per 100 g. A tap opens its
    details page (#72). With the script, press and hold it for a menu (Edit, Rename,
    Confirm, Log it, Delete), swipe it left to delete it, and swipe an unconfirmed one right
    to confirm it. `last_logged` says when it was last logged; `log_url` logs it."""
    n = logic.round_for_display(store.nutrients_of(row))
    meta = []
    if row["brand"]:
        meta.append(e(row["brand"]))
    if last_logged:
        meta.append(f"last {e(last_logged)}")
    meta.append(
        '<span class="badge ok">Confirmed</span>'
        if row["verified"]
        else '<span class="badge unsure">Unconfirmed</span>'
    )
    base = f"{PREFIX}/foods/{row['id']}"
    keep = f'<input type="hidden" name="back" value="{e(back)}">'
    delete = (
        f'<form class="swipe-action js-only" method="post" action="{base}/delete">{keep}'
        f'<button type="submit" aria-label="Delete {e(row["name"])} for everyone">Delete</button>'
        "</form>"
    )
    confirm = (
        ""
        if row["verified"]
        else f'<form class="swipe-action confirm js-only" method="post" action="{base}/confirm">'
        f'{keep}<input type="hidden" name="confirmed" value="1">'
        f'<button type="submit" aria-label="Confirm {e(row["name"])}">Confirm</button></form>'
    )
    return (
        f'<li id="food-{row["id"]}" class="swipe{"" if row["verified"] else " unconfirmed"}"'
        f' data-food="{base}" data-name="{e(row["name"])}" data-back="{e(back)}"'
        f' data-log="{e(log_url)}" data-confirmed="{int(bool(row["verified"]))}">'
        f'{confirm}{delete}<div class="swipe-body" data-swipe>'
        f'<a class="food-link" href="{e(food_url(row["id"], back))}" draggable="false">'
        f'<div class="row"><span class="name clamp">{e(row["name"])}</span>'
        f'<span class="kcal">{num(n["kcal"])} <small>kcal</small></span></div>'
        f'<div class="row small muted"><span>{" · ".join(meta)}</span>'
        f"<span>{macros_line(n)}</span></div></a></div></li>"
    )


def facts_table(per_100g: logic.Nutrients, serving_g: float | None) -> str:
    """A food's nutrition facts in label order: per serving (when it has one) and per 100 g.
    Rows the food has no value for are left out."""
    columns = [logic.scale(per_100g, serving_g)] if serving_g else []
    columns.append(dict(per_100g))
    heads = (["Serving"] if serving_g else []) + ["100 g"]
    rows = []
    for key, indent in FACTS:
        if key == "net_carbs":
            values = [logic.net_carbs(c) for c in columns]
            label, unit_key = "Net carbs", "carbs_g"
        else:
            values = [c.get(key) for c in columns]
            label, unit_key = NUTRIENT_LABELS[key], key
        if values[-1] is None:
            continue
        cells = "".join(f"<td>{amount(unit_key, v)}</td>" for v in values)
        cls = ' class="indent"' if indent else (' class="top"' if key == "kcal" else "")
        rows.append(f'<tr{cls}><th scope="row">{label}</th>{cells}</tr>')
    head = "".join(f'<th scope="col">{h}</th>' for h in heads)
    return (
        '<table class="label-table"><thead><tr><th scope="col"><span class="sr">Nutrient'
        f"</span></th>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def food_micros(per_100g: logic.Nutrients, serving_g: float | None) -> str:
    """A food's vitamins and minerals, per serving (else per 100 g), with % Daily Value."""
    n = logic.scale(per_100g, serving_g) if serving_g else per_100g
    rows = "".join(
        f'<tr><th scope="row">{NUTRIENT_LABELS[k]}</th><td>{amount(k, n[k])}</td>'
        f'<td class="muted">{num(logic.percent_dv(k, n[k]))}%</td></tr>'
        for k in logic.MICROS
        if n.get(k) is not None
    )
    if not rows:
        return ""
    basis = "Per serving" if serving_g else "Per 100 g"
    return (
        f'<table class="micros small"><thead><tr><th scope="col">{basis}</th>'
        '<th scope="col">Amount</th><th scope="col">Daily Value</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def with_params(url: str, anchor: str = "", **params: str | int) -> str:
    """A dashboard URL with its one-shot notices (done, undone, problem, checked) replaced."""
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in ("done", "undone", "problem", "checked", "ok", "portion")
    ]
    query += [(k, str(v)) for k, v in params.items()]
    return parts.path + (f"?{urlencode(query)}" if query else "") + (f"#{anchor}" if anchor else "")


# Messages for ?problem=, shown after a rejected edit
PROBLEMS = {
    "when": "Pick a date up to today and a time.",
    "gap": "That time doesn't exist on that day (the clocks changed). Pick another.",
    "grams": f"Grams must be a number more than 0 and at most {MAX_GRAMS:,}.",
    "servings": f"Servings must be a number more than 0 and at most {MAX_GRAMS:,} g in all.",
    "gone": "That was changed or deleted in the meantime; this is the latest.",
    "undo": "Couldn't undo: those items changed since (maybe from Claude), so nothing changed.",
    "name": "A food needs a name.",
    "portion": f"A portion needs a name and grams more than 0 and at most {MAX_GRAMS:,}.",
}

# --- New food (#59): barcode scan, label photo, or typed in; all fill the same form ----------

# The values a new food's form asks for, per serving, in the order labels print them
FORM_MAIN = (
    "kcal",
    "fat_g",
    "sat_fat_g",
    "trans_fat_g",
    "poly_fat_g",
    "mono_fat_g",
    "cholesterol_mg",
    "sodium_mg",
    "carbs_g",
    "fiber_g",
    "sugar_g",
    "added_sugar_g",
    "sugar_alcohol_g",
    "protein_g",
)
# Rows a label indents under the one above them (fat's and carbs' breakdowns)
FORM_INDENT = frozenset(
    {
        "sat_fat_g",
        "trans_fat_g",
        "poly_fat_g",
        "mono_fat_g",
        "fiber_g",
        "sugar_g",
        "added_sugar_g",
        "sugar_alcohol_g",
    }
)
FORM_KEYS = ("serving_g", *FORM_MAIN, *logic.MICROS)
FORM_LABELS = {**NUTRIENT_LABELS, "serving_g": "Serving size"}
FORM_UNITS = {**UNITS, "serving_g": "g"}
FORM_PLACES = {**logic.DISPLAY_PLACES, "serving_g": 1}
MAX_VALUE = 100_000  # per serving, in any unit: anything bigger is a misread
SERVING_DESC_MAX = 60
PHOTO_MAX = 20 * 1024 * 1024
# Where a barcode was found, for the form's heading
FOUND_IN = {
    "saved": "Already in Chomp",
    "fdc": "Found in USDA FoodData Central",
    "off": "Found on Open Food Facts",
}
MODES = ("barcode", "label", "manual", "edit")  # edit: a saved food's values (#72)


def field_text(key: str, value: float | None) -> str:
    """A value as a form box shows it: plain digits, no thousands separators."""
    if value is None:
        return ""
    text = f"{round(value, FORM_PLACES[key]):.{FORM_PLACES[key]}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def scanned_values(row) -> dict[str, str]:
    """A saved food's values per serving (per 100 g when it has no serving size), as the
    form shows them. Saving compares against these to see what the person changed."""
    basis = row["serving_g"] or 100.0
    per = logic.scale(store.nutrients_of(row), basis)
    return {"serving_g": field_text("serving_g", basis)} | {
        k: field_text(k, per[k]) for k in FORM_KEYS[1:]
    }


def parse_value(key: str, text: str) -> float | None:
    """A form value, or None when blank. Raises ValueError with a message for the person."""
    text = text.replace(",", "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{FORM_LABELS[key]} must be a number.") from None
    if not math.isfinite(value) or value < 0 or value > MAX_VALUE:
        raise ValueError(f"{FORM_LABELS[key]} must be a number from 0 to {MAX_VALUE:,}.")
    if key == "serving_g" and value == 0:
        raise ValueError("Serving size must be more than 0 g.")
    return value


def value_row(key: str, value: str, *, tick: bool | None) -> str:
    """One value: its name, a number box, the unit, and (label photos only) a tick box the
    person checks once the value matches the label."""
    required = key in ("serving_g", *logic.CORE)
    box = (
        f'<input id="f-{key}" name="{key}" type="number" step="any" min="0"'
        f' inputmode="decimal" autocomplete="off" value="{e(value)}"'
        + (" required" if required else "")
        + ">"
    )
    check = ""
    if tick is not None:
        check = (
            f'<label class="tick"><input type="checkbox" name="ok_{key}" value="1"'
            + (" checked" if tick else "")
            + f'><span class="sr">{e(FORM_LABELS[key])} matches the label</span></label>'
        )
    return (
        f'<div class="vrow{" indent" if key in FORM_INDENT else ""}">'
        f'<label for="f-{key}">{e(FORM_LABELS[key])}</label>'
        f'<span class="field">{box}<span class="unit">{FORM_UNITS[key]}</span></span>'
        f"{check}</div>"
    )


@dataclass
class FoodForm:
    """What the New food form shows: its mode, the values (as typed or pre-filled), and
    for a label photo which values are ticked."""

    mode: str  # 'barcode', 'label' or 'manual'
    values: dict[str, str]
    ticks: frozenset[str] = frozenset()
    name: str = ""
    brand: str = ""
    barcode: str = ""
    serving_desc: str = ""
    food_id: int | None = None
    verified: bool = False  # typed in: the person ticked "Verified"
    heading: str = ""
    note: str = ""  # shown above the values (e.g. what to check)
    error: str = ""
    ocr_text: str = ""
    back: str = ""  # edit: the page to go back to


MODE_HELP = {
    "barcode": "Check the values against the package and fix anything that's off. A scanned"
    " food counts as confirmed.",
    "label": "Fix any value Chomp misread, then tick each one once it matches the label."
    " Only a food with every value ticked counts as confirmed.",
    "manual": "Enter the values for one serving. Tick Verified if you trust the numbers;"
    " otherwise the food stays unconfirmed (shown in amber) until you confirm it.",
    "edit": "Values are for one serving. Meals already logged keep their numbers. Tick"
    " Verified if you trust them; otherwise the food is unconfirmed (shown in amber).",
}


def food_form(f: FoodForm) -> str:
    ticking = f.mode == "label"

    def row(key: str) -> str:
        return value_row(key, f.values.get(key, ""), tick=(key in f.ticks) if ticking else None)

    open_micros = any(f.values.get(k) for k in logic.MICROS)
    barcode = (
        ""
        if f.mode == "edit"
        else f'<input type="hidden" name="barcode" value="{e(f.barcode)}">'
        f'<p class="small muted">Barcode {e(f.barcode)}</p>'
        if f.mode == "barcode"
        else "<label>Barcode <small>(optional, so a scan finds it)</small><input"
        f' name="barcode" inputmode="numeric" autocomplete="off" value="{e(f.barcode)}"></label>'
    )
    parts = [
        f'<form class="stack newfood" method="post" action="{PREFIX}/foods/new"'
        + (" data-ticks" if ticking else "")
        + ">",
        f'<input type="hidden" name="mode" value="{f.mode}">',
        f'<input type="hidden" name="food_id" value="{f.food_id}">' if f.food_id else "",
        f'<input type="hidden" name="back" value="{e(f.back)}">' if f.back else "",
        f'<p class="error" role="alert">{e(f.error)}</p>' if f.error else "",
        f'<label>Name<input name="name" value="{e(f.name)}" required'
        f' maxlength="{FOOD_NAME_MAX}" autocomplete="off"></label>',
        f'<label>Brand <small>(optional)</small><input name="brand" value="{e(f.brand)}"'
        f' maxlength="{FOOD_NAME_MAX}" autocomplete="off"></label>',
        barcode,
        f'<p class="small muted">{e(MODE_HELP[f.mode])}</p>',
        f'<p class="small warn">{e(f.note)}</p>' if f.note else "",
        '<div class="values">'
        + (
            '<div class="vhead small muted"><span>Per serving</span><span>On label?</span></div>'
            if ticking
            else '<div class="vhead small muted"><span>Per serving</span></div>'
        ),
        row("serving_g"),
        f'<label class="desc">Serving as printed <small>(optional)</small><input'
        f' name="serving_desc" value="{e(f.serving_desc)}" maxlength="{SERVING_DESC_MAX}"'
        ' placeholder="2/3 cup (55g)" autocomplete="off"></label>',
        *(row(k) for k in FORM_MAIN),
        "</div>",
        f'<details class="extras"{" open" if open_micros else ""}><summary>Vitamins and'
        ' minerals <small class="muted">(optional)</small></summary><div class="values">'
        + "".join(row(k) for k in logic.MICROS)
        + "</div></details>",
    ]
    if ticking:
        parts.append('<p class="small muted js-only" data-tick-count></p>')
    if f.mode in ("manual", "edit"):
        parts.append(
            '<label class="checkline"><input type="checkbox" name="verified" value="1"'
            + (" checked" if f.verified else "")
            + "> Verified <small>(count it as confirmed)</small></label>"
        )
    label = {"barcode": "Save as confirmed", "edit": "Save changes"}.get(f.mode, "Save food")
    parts.append(f'<button type="submit" data-save>{label}</button>')
    if f.ocr_text:
        parts.append(
            '<details class="sub"><summary>What Chomp read on the photo</summary>'
            f'<pre class="ocr small">{e(f.ocr_text)}</pre></details>'
        )
    parts.append("</form>")
    return "".join(parts)


# Daily goals: key, name, unit, most allowed. A macro may take all of the most calories
# (the calorie bar can drag one that far), so its limit is those calories in grams.
GOALS = (
    ("kcal", "Calories", "kcal", 20_000),
    ("protein_g", "Protein", "g", 5_000),
    ("carbs_g", "Carbs", "g", 5_000),
    ("fat_g", "Fat", "g", 2_500),
)


def toast(body: str, role: str, kind: str = "") -> str:
    """A banner pinned above the tab bar, so it shows wherever the page lands."""
    close = (
        '<button type="button" class="dismiss js-only" data-dismiss aria-label="Dismiss">'
        "&times;</button>"
    )
    return f'<div class="flash toast{kind}" role="{role}">{body}{close}</div>'


def goal_start(values: dict[str, str]) -> tuple[dict[str, float], float | None]:
    """The calories and macro grams the goals page starts from. No calorie goal yet means
    2,000 kcal; macros missing, or saved ones that don't add up to the calories, are fitted to
    them (even thirds, or keeping their shares). Also returns what saved macros came to when
    they were refitted, else None."""
    parsed: dict[str, float | None] = {}
    for key, *_ in GOALS:
        try:
            value = float(values[key].replace(",", "")) if values[key] else None
        except ValueError:
            value = None
        parsed[key] = value if value is not None and math.isfinite(value) and value > 0 else None
    kcal = parsed["kcal"] or logic.DEFAULT_KCAL
    mk = logic.macro_kcal(parsed)
    if not mk:
        return {"kcal": kcal, **logic.fit_macros(kcal)}, None
    if abs(mk["total"] - kcal) < 25:
        return {"kcal": kcal, **{k: parsed[k] for k in logic.KCAL_PER_G}}, None
    shares = {k: mk[k] / mk["total"] for k in logic.KCAL_PER_G}
    return {"kcal": kcal, **logic.fit_macros(kcal, shares)}, mk["total"]


def calorie_bar(start: dict[str, float]) -> str:
    """The calorie bar: one slice per macro, sized by its share of the calories (4/4/9 kcal
    per g). The script adds the draggable borders and the grams and percent in each slice;
    without it the macro boxes under the bar are typed instead."""
    mk = logic.macro_kcal(start)
    total = mk["total"] if mk else 0
    rects, labels, handles, x = [], [], [], 0.0
    for i, (key, cls, _) in enumerate(MACROS):
        share = 100 * mk[key] / total if total > 0 else 100 / 3
        rects.append(
            f'<rect data-k="{key}" class="fill {key}" x="{x:.2f}" y="0" width="{share:.2f}"'
            ' height="10"/>'
        )
        labels.append(
            f'<span class="lab {cls}" data-lab="{key}"><span class="txt">'
            f"<b data-g>{num(start[key])}</b>{NBSP}g · <span data-pct>{round(share)}</span>%"
            "</span></span>"
        )
        x += share
        if i < 2:
            between = f"{NUTRIENT_LABELS[key]} and {NUTRIENT_LABELS[MACROS[i + 1][0]]}".lower()
            handles.append(
                f'<button type="button" class="handle" data-handle="{i}" role="slider"'
                f' aria-label="Border between {between}"></button>'
            )
    return (
        '<div class="calbar" data-calbar><svg class="splitbar" viewBox="0 0 100 10"'
        f' preserveAspectRatio="none" aria-hidden="true">{"".join(rects)}</svg>'
        f'<div class="labs js-only" aria-hidden="true">{"".join(labels)}</div>'
        f'<div class="js-only">{"".join(handles)}</div></div>'
    )


def goals_form(values: dict[str, str], back: str, error: str = "") -> str:
    start, saved_total = goal_start(values)
    # A rejected post shows what was typed; otherwise the numbers the bar starts from
    shown = values if error else {k: num(v, 1).replace(",", "") for k, v in start.items()}
    kcal = (
        '<div class="goal kcal amount"><label>Calories<span class="field"><input name="kcal"'
        f' data-kcal inputmode="numeric" autocomplete="off" required value="{e(shown["kcal"])}">'
        '<span class="unit">kcal</span></span></label></div>'
    )
    boxes = "".join(
        f'<div class="goal {cls} amount"><label>{NUTRIENT_LABELS[key]}<span class="field">'
        f'<input name="{key}" inputmode="decimal" autocomplete="off" required'
        f' value="{e(shown[key])}"><span class="unit">g</span></span></label></div>'
        for key, cls, _ in MACROS
    )
    refit = ""
    if saved_total is not None and not error:
        refit = (
            '<p class="small muted" data-refit>Your saved protein, carbs and fat added up to'
            f" {num(saved_total)}{NBSP}kcal, so the bar fits them to your calorie goal."
            " Save to keep it.</p>"
        )
    problem = f'<p class="error" role="alert">{e(error)}</p>' if error else ""
    return (
        f'<form class="card goals stack" method="post" action="{PREFIX}/targets">{problem}'
        f'<input type="hidden" name="back" value="{e(back)}">{kcal}'
        + calorie_bar(start)
        + '<p class="small muted js-only">Drag the borders to split your calories between'
        " protein, carbs and fat. A new calorie number starts them at even thirds.</p>"
        + refit
        + f'<div class="no-js stack">{boxes}</div>'
        + '<p class="muted small">Progress bars on Today follow these, and Claude sees the same'
        " goals.</p>"
        '<button type="submit">Save goals</button></form>'
    )


def pick_item(
    row, ctx: DayContext, meal_id: int | None, q: str, default_time: str, portions=()
) -> str:
    """A food on the Add food page: tap it for a grams slider, a live preview and where to
    log it."""
    keys = row.keys()
    per_100g = store.nutrients_of(row)
    n = logic.round_for_display(per_100g)
    usual = row["portion_grams"] if "portion_grams" in keys else None
    grams = usual or row["serving_g"] or 100.0
    estimated = bool(usual and row["portion_estimated"])
    meta = []
    if "portion" in keys and row["portion"]:
        meta.append(f"“{e(row['portion'])}”")
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
        f'<input type="hidden" name="estimated" value="{int(estimated)}">'
        f'<input type="hidden" name="date" value="{ctx.day.isoformat()}">'
        f'<input type="hidden" name="q" value="{e(q)}">'
        + (f'<p class="muted small">{e(row["name"])}</p>' if len(row["name"]) > LONG_NAME else "")
        + amount_picker(row, grams, slider_top(grams, row["serving_g"]))
        + grams_chips(row, portions)
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
    off: OFFClient | None = None,
) -> None:
    """Register the dashboard. provider is None in local-only mode (no sign-in). With fdc
    configured, Add food can also search USDA FoodData Central. New food looks barcodes up
    through fdc and off."""
    tz = settings.tz
    sessions = Sessions(settings.db_path)
    secure = bool(settings.public_url)
    css = (STATIC_DIR / "app.css").read_text()
    js = (STATIC_DIR / "app.js").read_text()
    usda = fdc is not None and fdc.enabled
    fdc = fdc or FDCClient(settings.fdc_api_key)
    off = off or OFFClient(settings.off_user_agent)
    ocr_limit = anyio.CapacityLimiter(1)  # one label photo at a time; OCR is CPU-heavy

    def user_of(request: Request) -> int | None:
        """The signed-in user (#46), or None. Local-only mode is always the owner."""
        if provider is None:
            return users.OWNER_ID
        if not hasattr(request.state, "user_id"):
            request.state.user_id = sessions.user(request.cookies.get(COOKIE, ""))
        return request.state.user_id

    def connect(request: Request):
        """A connection for the signed-in user's log. Routes check signed_in() first."""
        user_id = user_of(request)
        if user_id is None:
            raise RuntimeError("not signed in")
        return closing(db.connect(settings.db_path, user_id))

    def account(request: Request) -> str:
        """The signed-in user's name, for the page footer."""
        if provider is None:
            return ""
        with connect(request) as conn:
            return users.name_of(conn, store.user_of(conn)) or ""

    def today() -> date:
        return datetime.now(ZoneInfo(tz)).date()

    def signed_in(request: Request) -> bool:
        return user_of(request) is not None

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

    def breakdown_url(day: date, anchor: str = "", back: str = "") -> str:
        query = {"date": day.isoformat()} | ({"back": back} if back else {})
        return f"{PREFIX}/breakdown?{urlencode(query)}" + (f"#{anchor}" if anchor else "")

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
        if name not in STATIC:
            return Response("Not found", status_code=404)
        return Response(
            (STATIC_DIR / name).read_bytes(),
            media_type=STATIC[name],
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
        user_id = provider.check_password(str(form.get("password", "")))
        if user_id is None:
            log.warning("dashboard: wrong password")
            if provider.locked_out():
                return login_page(next_url, locked, 429)
            return login_page(next_url, "Wrong password.", 401)
        log.info("dashboard: user %d signed in", user_id)
        response = RedirectResponse(next_url, status_code=303, headers=SECURITY_HEADERS)
        response.set_cookie(
            COOKIE,
            sessions.create(user_id),
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
        with connect(request) as conn:
            meals, totals, targets, ctx = load_day(conn, day)
            rows = [x.nutrients for _, entries in meals for x in entries]
            foods = store.get_foods(conn, (x.food_id for _, entries in meals for x in entries))
            banner = notice(conn, params, page_url)
        # Goals not set count as the defaults here (the sliders and History keep the stored ones)
        progress = logic.progress(totals, logic.with_default_targets(targets))
        shown = logic.round_for_display(totals)

        # After a rejected amount: the typed value back on its entry, which says what's wrong
        error: tuple[int, str, str] | None = None
        if (failed := int_param(params.get("error"))) is not None:
            error = (failed, params.get("by", ""), params.get("amount", "")[:20])

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
        kcal = progress["kcal"]
        left = round(kcal["remaining"])
        # Over the goal: amber up to 100 kcal over, red past that
        tone = "" if left >= 0 else "over" if left < -100 else "caution"
        cls = f"big kcal {tone}".rstrip()
        summary.append(
            f'<div class="row"><a class="hero" href="{breakdown_url(day)}">'
            f'<span class="{cls}">{num(abs(left))}</span>'
            f" {'remaining' if left >= 0 else 'over'}"
            f' <span class="more-link small">Breakdown&nbsp;&rsaquo;</span></a>'
            f'<a class="small" href="{goals_url}">Edit goals</a></div>'
            + meter(kcal["percent"], "kcal", tone=tone)
            + f'<p class="muted small">Goal {num(kcal["target"])} kcal</p>'
        )
        macros = []
        for key, cls, _ in MACROS:
            p = progress.get(key)
            eaten = num(shown[key], logic.DISPLAY_PLACES[key])
            if p:
                left = round(p["remaining"], logic.DISPLAY_PLACES[key])
                note = f"{amount(key, abs(left))} {'left' if left >= 0 else 'over'}"
                macros.append(
                    f'<a class="macro" href="{breakdown_url(day, f"top-{key}")}">'
                    f'<div class="small {cls}">{NUTRIENT_LABELS[key]}</div>'
                    f"<div><strong>{eaten}</strong>"
                    f' <span class="muted target">/ {amount(key, p["target"])}</span></div>'
                    f"{meter(p['percent'], key)}"
                    f'<div class="muted small">{note}</div></a>'
                )
            else:
                macros.append(
                    f'<a class="macro" href="{breakdown_url(day, f"top-{key}")}">'
                    f'<div class="small {cls}">{NUTRIENT_LABELS[key]}</div>'
                    f"<div><strong>{eaten}</strong> g</div></a>"
                )
        summary.append(f'<div class="macros">{"".join(macros)}</div>')
        if extras := extras_line(shown):
            summary.append(
                '<details class="extras"><summary class="small">Fiber, sugar and sodium</summary>'
                f'<p class="muted small">{extras}</p></details>'
            )
        summary.append(micros_table(shown, logic.partial_micros(rows)))
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
            user=account(request),
        )

    # --- Day breakdown (#63) ---

    @server.custom_route(f"{PREFIX}/breakdown", methods=["GET"])
    async def breakdown(request: Request) -> Response:
        """A day's nutrients: the macro split, calories by meal, the foods that gave the most
        of each, and the full nutrition facts. A food with no value for a nutrient counts as
        none of it (Will's call, #63); a nutrient no food that day reports isn't shown."""
        if not signed_in(request):
            return to_login(request)
        now = today()
        params = request.query_params
        try:
            day = logic.parse_local_date(params.get("date"), tz)
            if not MIN_YEAR <= day.year <= MAX_YEAR:
                raise ValueError(day)
        except ValueError:
            return message("Unknown date", "Dates look like 2026-10-06.", 400, signed_in=True)
        back = safe_next(params.get("back") or day_url(day))
        with connect(request) as conn:
            meals, totals, _, _ = load_day(conn, day)
            entries = [x for _, xs in meals for x in xs]
            foods = store.get_foods(conn, (x.food_id for x in entries))
        shown = logic.round_for_display(totals)
        items = [(x.food_id, x.nutrients) for x in entries]

        def top(key: str) -> list[tuple[str, bool, float]]:
            return [
                (foods[f]["name"], bool(foods[f]["verified"]), v)
                for f, v in logic.top_foods(items, key)
            ]

        title = day_title(day, now)
        parts = [
            '<header class="daynav">',
            f'<a class="step" href="{e(back)}" aria-label="Back">&lsaquo;</a>',
            '<div class="daytitle"><h1>Breakdown</h1>'
            f'<p class="muted small">{e(long_date(day, now))}</p></div>',
            '<span class="step off" aria-hidden="true"></span></header>',
        ]
        if not entries:
            parts.append(
                '<section class="card empty"><p>Nothing logged on this day.</p>'
                f'<p class="muted small"><a href="{day_url(day)}">Back to {e(title)}</a></p>'
                "</section>"
            )
            return render(
                "Breakdown",
                "".join(parts),
                "today",
                signed_in=provider is not None,
                user=account(request),
            )

        # Calories and the macro split
        split = logic.macro_split(totals)
        rects, tiles, x = [], [], 0.0
        for key, cls, _ in MACROS:
            share = split[key] if split else 0.0
            rects.append(
                f'<rect class="fill {key}" x="{x:.1f}" y="0" width="{share:.1f}" height="10"/>'
            )
            x += share
            tiles.append(
                f'<a class="macro" href="#top-{key}"><div class="small {cls}">'
                f"{NUTRIENT_LABELS[key]}</div><div><strong>{round(share)}%</strong></div>"
                f'<div class="muted small">{amount(key, shown[key])}</div></a>'
            )
        parts.append(
            f'<section class="card summary" aria-label="Totals for {e(title)}">'
            f'<p class="hero"><span class="big kcal">{num(shown["kcal"])}</span> kcal</p>'
            f'<svg class="splitbar" viewBox="0 0 100 10" preserveAspectRatio="none"'
            f' aria-hidden="true">{"".join(rects)}</svg>'
            f'<div class="macros">{"".join(tiles)}</div>'
            '<p class="muted small">Share of the calories from protein, carbs and fat.</p>'
            "</section>"
        )

        # Calories by meal
        day_kcal = totals["kcal"] or 0.0
        rows = []
        for meal, xs in meals:
            kcal = logic.sum_nutrients(x.nutrients for x in xs)["kcal"] or 0.0
            pct = 100.0 * kcal / day_kcal if day_kcal > 0 else 0.0
            rows.append(
                f'<li><div class="row"><a href="{day_url(day)}#meal-{meal["id"]}">'
                f'{e(meal_title(meal))}</a><span><span class="kcal">{num(kcal)}</span>'
                f'{NBSP}kcal <span class="muted small">{round(pct)}%</span></span></div>'
                f"{meter(pct, 'kcal')}</li>"
            )
        parts.append(
            fold("bd-meals", "Calories by meal", f'<ul class="bymeal">{"".join(rows)}</ul>')
        )

        # Top foods per macro
        cards = []
        for key, cls, _ in MACROS:
            best = top(key)
            body = top_list(best, key) if best else '<p class="muted small">None this day.</p>'
            cards.append(
                f'<div class="topcard" id="top-{key}"><h3 class="{cls}">'
                f"{NUTRIENT_LABELS[key]}</h3>{body}</div>"
            )
        parts.append(fold("bd-top", "Top foods", "".join(cards)))

        # Nutrition facts: tap a row for the foods that gave the most of it
        def fact(key: str, label: str, value: str, indent: bool) -> str:
            cls = "fact indent" if indent else "fact"
            head = f"<span>{label}</span><span>{value}</span>"
            best = top(key) if key in logic.NUTRIENTS else []
            if not best:
                return f'<li class="{cls}"><div class="row">{head}</div></li>'
            return (
                f'<li class="{cls}"><details><summary class="row">{head}</summary>'
                f"{top_list(best, key)}</details></li>"
            )

        facts = []
        for key, indent in FACTS:
            if key == "net_carbs":
                if (net := logic.net_carbs(totals)) is not None:
                    facts.append(fact(key, "Net carbs", amount("carbs_g", net), indent))
            elif shown[key] is not None:
                facts.append(fact(key, NUTRIENT_LABELS[key], amount(key, shown[key]), indent))
        parts.append(
            fold(
                "bd-facts",
                "Nutrition facts",
                f'<ul class="facts">{"".join(facts)}</ul>'
                '<p class="muted small hint">Tap a nutrient to see which foods gave the most.'
                " Foods with no value for one count as none.</p>",
            )
        )

        # Vitamins and minerals, folded until opened
        micros = [
            fact(
                k,
                NUTRIENT_LABELS[k],
                f'{amount(k, shown[k])} <span class="muted small">'
                f"{num(logic.percent_dv(k, shown[k]))}%{NBSP}DV</span>",
                False,
            )
            for k in logic.MICROS
            if shown[k] is not None
        ]
        if micros:
            parts.append(
                fold(
                    "bd-micros",
                    "Vitamins and minerals",
                    f'<ul class="facts">{"".join(micros)}</ul>',
                    opened=False,
                )
            )
        return render(
            "Breakdown",
            "".join(parts),
            "today",
            signed_in=provider is not None,
            user=account(request),
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
        with connect(request) as conn:
            meals, _, _, ctx = load_day(conn, day)
            meal = next((m for m, _ in meals if m["id"] == meal_id), None)
            if meal is None:  # gone, or on another day: log a new meal instead
                meal_id = None
            if q:
                portioned = store.search_portions(conn, q, ADD_LIMIT)
                rows = store.search_foods(conn, q, ADD_LIMIT)
                names = set(store.templates_matching(conn, q))
                templates = [t for t in store.list_templates(conn) if t[0]["name"] in names]
            else:
                portioned = []
                rows = store.list_used_foods(conn, ADD_LIMIT)
                templates = store.list_templates(conn)
            seen = {r["id"] for r in rows} | {a["id"] for a in portioned}
            portions = store.portions_of(conn, seen)
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
            ' aria-label="Search foods and saved meals">'
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
        picks = [pick_item(a, ctx, meal_id, q, default_time, portions[a["id"]]) for a in portioned]
        picks += [
            pick_item(r, ctx, meal_id, q, default_time, portions[r["id"]])
            for r in rows
            if r["id"] not in {a["id"] for a in portioned}
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
        parts.append(
            f'<p class="muted small center">Not here? <a href="{PREFIX}/foods/new">Add a new'
            " food</a> by barcode, label photo or by hand.</p>"
        )
        return render(
            "Add food",
            "".join(parts),
            "today",
            signed_in=provider is not None,
            user=account(request),
        )

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
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect(request) as conn, db.transaction(conn):
            try:
                food = store.get_food(conn, int_param(str(form.get("food_id", ""))) or 0, live=True)
                into = meal_id if meal_id is not None else int_param(str(form.get("to", "")))
                if into is not None:
                    store.get_meal(conn, into)
            except store.NotFound:
                return go(with_params(retry, problem="gone"))
            by_servings = form.get("by") == "servings" and food["serving_g"]
            try:
                if by_servings:
                    raw = str(form.get("servings", "")).replace(",", "").strip()
                    grams = parse_servings(raw, food["serving_g"])
                else:
                    grams = parse_grams(str(form.get("grams", "")).replace(",", "").strip())
            except ValueError:
                return go(with_params(retry, problem="servings" if by_servings else "grams"))
            # Servings are counted by hand, so they're exact even when a portion was usual
            estimated = not by_servings and form.get("estimated") == "1"
            items = [(food["id"], grams, estimated)]
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
            if by_servings:
                count = grams / food["serving_g"]
                summary = f"Added {servings_text(count)} of {food['name']} ({num(grams, 1)} g)"
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
        with connect(request) as conn, db.transaction(conn):
            try:
                template, rows = store.get_template_by_id(
                    conn, int_param(str(form.get("template_id", ""))) or 0
                )
                into = meal_id if meal_id is not None else int_param(str(form.get("to", "")))
                if into is not None:
                    store.get_meal(conn, into)
            except store.NotFound:
                return go(with_params(retry, problem="gone"))
            items = [(r["food_id"], r["grams"], bool(r["estimated"])) for r in rows]
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
        entry_id = request.path_params["entry_id"]
        anchor = f"entry-{entry_id}"
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect(request) as conn, db.transaction(conn):
            try:
                entry = store.get_entry(conn, entry_id)
                day = meal_day(store.get_meal(conn, entry.meal_id))
            except store.NotFound:
                return message("Not found", "That entry no longer exists.", 404, signed_in=True)
            food = store.get_food(conn, entry.food_id)
            by = "servings" if form.get("by") == "servings" and food["serving_g"] else "grams"
            raw = str(form.get(by, "")).replace(",", "").strip()
            try:
                if by == "servings":
                    grams = parse_servings(raw, food["serving_g"])
                else:
                    grams = parse_grams(raw)
            except ValueError:
                return back_to_day(day, anchor, error=entry_id, by=by, amount=raw[:20])
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
        with connect(request) as conn, db.transaction(conn):
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
        with connect(request) as conn, db.transaction(conn):
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
        with connect(request) as conn, db.transaction(conn):
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
        with connect(request) as conn, db.transaction(conn):
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
        with connect(request) as conn, db.transaction(conn):
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
            with connect(request) as conn:
                current = store.get_targets(conn)
            values = {
                k: num(v, 1).replace(",", "") if v is not None else "" for k, v in current.items()
            }
            return goals_page(request, values, back)
        if (no := refused(request, "Change goals")) is not None:
            return no
        form = await request.form()
        back = safe_next(str(form.get("back", PREFIX)))
        values = {k: str(form.get(k, "")).replace(",", "").strip()[:12] for k, *_ in GOALS}
        changes: dict[str, float | None] = {}
        for key, label, unit, most in GOALS:
            raw = values[key]
            try:
                value = float(raw)
                if not math.isfinite(value) or value <= 0 or value > most:
                    raise ValueError(value)
            except ValueError:
                problem = f"{label} must be a number more than 0 and at most {most:,} {unit}."
                return goals_page(request, values, back, problem, 400)
            changes[key] = value
        # One transaction: an edit from elsewhere can't land between the change and its
        # undo record
        with connect(request) as conn, db.transaction(conn):
            before = store.capture(conn, targets=True)
            store.set_targets(conn, changes)
            undo_id = store.record_undo(conn, "Updated your daily goals", before)
        log.info("dashboard: updated targets")
        return go(with_params(back, done=undo_id))

    def goals_page(
        request: Request, values: dict[str, str], back: str, error: str = "", status: int = 200
    ):
        body = (
            '<header class="daynav">'
            f'<a class="step" href="{e(back)}" aria-label="Back">&lsaquo;</a>'
            '<div class="daytitle"><h1>Daily goals</h1></div>'
            '<span class="step off" aria-hidden="true"></span></header>'
            + goals_form(values, back, error)
        )
        return render(
            "Daily goals",
            body,
            "today",
            signed_in=provider is not None,
            user=account(request),
            status=status,
        )

    # --- Undo ---

    @server.custom_route(PREFIX + "/undo/{undo_id:int}", methods=["POST"])
    async def undo(request: Request) -> Response:
        if (no := refused(request, "Undo")) is not None:
            return no
        form = await request.form()
        back = safe_next(str(form.get("back", PREFIX)))
        undo_id = request.path_params["undo_id"]
        with connect(request) as conn:
            try:
                store.undo(conn, undo_id)
            except store.NotFound:
                return go(with_params(back))
            except store.UndoConflict:
                return go(with_params(back, problem="undo"))
        log.info("dashboard: undid action %d", undo_id)
        return go(with_params(back, undone=undo_id))

    # --- New food (#59) ---

    def new_food_page(
        request: Request,
        body: str,
        status: int = 200,
        *,
        title: str = "New food",
        back: str = f"{PREFIX}/foods",
    ) -> Response:
        header = (
            '<header class="daynav">'
            f'<a class="step" href="{e(back)}" aria-label="Back">&lsaquo;</a>'
            f'<div class="daytitle"><h1>{e(title)}</h1></div>'
            '<span class="step off" aria-hidden="true"></span></header>'
        )
        return render(
            title,
            header + body,
            "foods",
            signed_in=provider is not None,
            user=account(request),
            status=status,
            scripts=("zxing.min.js", "scan.js"),
        )

    def new_food_url(**params: str) -> str:
        query = urlencode({k: v for k, v in params.items() if v})
        return f"{PREFIX}/foods/new" + (f"?{query}" if query else "")

    def barcode_card(code: str = "", error: str = "") -> str:
        return (
            '<section class="card"><h2>Scan a barcode</h2>'
            '<div class="scanner" data-scanner hidden><video muted playsinline'
            ' aria-label="Camera view"></video><p class="small muted" data-scan-status></p>'
            '<button type="button" class="link" data-scan-stop>Stop scanning</button></div>'
            '<p class="small error" data-scan-error hidden></p>'
            '<button type="button" class="js-only wide" data-scan-start>Scan with camera</button>'
            f'<form class="stack" method="get" action="{PREFIX}/foods/new" data-barcode-form>'
            + (f'<p class="error" role="alert">{e(error)}</p>' if error else "")
            + '<label>Or type the number under the barcode<input name="barcode"'
            f' inputmode="numeric" autocomplete="off" value="{e(code)}" required></label>'
            '<button type="submit">Look it up</button></form>'
            '<p class="small muted">Chomp looks it up in USDA FoodData Central and Open Food'
            " Facts. A scanned food counts as confirmed.</p></section>"
        )

    def label_card(code: str = "", error: str = "") -> str:
        hidden = f'<input type="hidden" name="barcode" value="{e(code)}">' if code else ""
        unavailable = (
            ""
            if label_reader.available()
            else '<p class="small warn">Label reading isn\'t installed on this server, so this'
            " won't fill anything in. Type the values instead.</p>"
        )
        return (
            '<section class="card"><h2>Read a nutrition label</h2>'
            '<p class="small muted">Take a photo of the Nutrition Facts panel, straight on'
            " and filling the frame. Chomp fills in what it can read; you check each value."
            f"</p>{unavailable}"
            f'<form class="stack" method="post" action="{PREFIX}/foods/label"'
            f' enctype="multipart/form-data">{hidden}'
            + (f'<p class="error" role="alert">{e(error)}</p>' if error else "")
            + '<input type="file" name="photo" accept="image/*" required'
            ' aria-label="Label photo"><button type="submit">Read label</button></form></section>'
        )

    def type_card(code: str = "") -> str:
        return (
            '<section class="card"><h2>Type it in</h2><p class="small muted">For a restaurant'
            " dish, home cooking or anything without a label. Tick Verified to count it as"
            " confirmed.</p>"
            f'<a class="button ghost wide" href="{e(new_food_url(how="type", barcode=code))}">'
            "Type in a food</a></section>"
        )

    @server.custom_route(f"{PREFIX}/foods/new", methods=["GET", "POST"])
    async def new_food(request: Request) -> Response:
        if request.method == "POST":
            return await save_new_food(request)
        if not signed_in(request):
            return to_login(request)
        params = request.query_params
        typed = params.get("barcode", "").strip()[:40]
        if params.get("how") == "type":
            form = FoodForm("manual", {}, barcode=typed)
            return new_food_page(request, f'<section class="card">{food_form(form)}</section>')
        if not typed:
            return new_food_page(request, barcode_card() + label_card() + type_card())
        try:
            code = gtins.normalize(typed)
        except gtins.InvalidBarcode:
            problem = "That isn't a barcode number. Check the digits and try again."
            body = barcode_card(typed, problem) + label_card() + type_card()
            return new_food_page(request, body, 400)
        with connect(request) as conn:
            hit = await barcode_lookup(conn, code, fdc, off)
        if hit.row is None:
            if hit.unreachable:
                text = (
                    "USDA and Open Food Facts can't be reached right now. Try again in a"
                    " minute, or read the label instead."
                )
            else:
                text = (
                    f"Nothing found for {code.lstrip('0') or code} in USDA or Open Food"
                    " Facts. Read the nutrition label, or type it in; the barcode is kept"
                    " so the next scan finds it."
                )
            body = (
                f'<section class="card empty"><p>{e(text)}</p></section>'
                + label_card(code)
                + type_card(code)
                + barcode_card()
            )
            return new_food_page(request, body)
        row = hit.row
        # Saving here confirms the food, so OFF's generic "unverified" note doesn't apply
        note = "; ".join(f for f in hit.flags if not f.startswith("unverified"))
        if not row["serving_g"]:
            note = "; ".join(
                filter(None, [note, "No serving size on record, so these values are per 100 g."])
            )
        form = FoodForm(
            "barcode",
            scanned_values(row),
            name=row["name"],
            brand=row["brand"] or "",
            barcode=code,
            serving_desc=row["serving_desc"] or "",
            food_id=row["id"],
            note=note,
        )
        status = "Confirmed" if row["verified"] else "Not confirmed yet"
        head = f'<p class="muted small">{e(FOUND_IN[hit.found_in])} · {status}</p>'
        return new_food_page(request, f'<section class="card">{head}{food_form(form)}</section>')

    @server.custom_route(f"{PREFIX}/foods/label", methods=["POST"])
    async def read_label(request: Request) -> Response:
        if (no := refused(request, "Read labels")) is not None:
            return no
        size = int_param(request.headers.get("content-length"))
        if size is not None and size > PHOTO_MAX:
            body = label_card(error="That photo is too big. Try one under 20 MB.")
            return new_food_page(request, body, 413)
        async with request.form(max_files=1, max_fields=5) as form:
            code = str(form.get("barcode", "")).strip()[:40]
            photo = form.get("photo")
            data = await photo.read() if hasattr(photo, "read") else b""
        if not data or len(data) > PHOTO_MAX:
            body = label_card(code, "Choose a photo of the label.")
            return new_food_page(request, body, 400)
        try:
            read = await anyio.to_thread.run_sync(label_reader.read_photo, data, limiter=ocr_limit)
        except label_reader.LabelError as err:
            return new_food_page(request, label_card(code, str(err)) + type_card(code), 400)
        found = {k: field_text(k, v) for k, v in read.values.items()}
        if read.serving_g is not None:
            found["serving_g"] = field_text("serving_g", read.serving_g)
        notes = []
        if read.serving_ml:
            notes.append("The serving is printed in mL; enter its weight in grams.")
        missing = [FORM_LABELS[k] for k in ("serving_g", *logic.CORE) if k not in found]
        if missing:
            notes.append(f"Couldn't read: {', '.join(missing)}. Type them in from the label.")
        log.info("dashboard: label photo read, %d values", len(found))
        form = FoodForm(
            "label",
            found,
            barcode=code,
            serving_desc=read.serving_desc or "",
            note=" ".join(notes),
            ocr_text=read.text,
        )
        return new_food_page(request, f'<section class="card">{food_form(form)}</section>')

    async def save_new_food(request: Request) -> Response:
        if (no := refused(request, "Add foods")) is not None:
            return no
        form = await request.form()
        mode = str(form.get("mode", ""))
        if mode not in MODES:
            mode = "manual"
        values = {k: str(form.get(k, "")).strip()[:20] for k in FORM_KEYS}
        f = FoodForm(
            mode,
            values,
            ticks=frozenset(k for k in FORM_KEYS if form.get(f"ok_{k}")),
            name=" ".join(str(form.get("name", "")).split())[:FOOD_NAME_MAX],
            brand=" ".join(str(form.get("brand", "")).split())[:FOOD_NAME_MAX],
            barcode=str(form.get("barcode", "")).strip()[:40],
            serving_desc=" ".join(str(form.get("serving_desc", "")).split())[:SERVING_DESC_MAX],
            food_id=(
                int_param(str(form.get("food_id", ""))) if mode in ("barcode", "edit") else None
            ),
            verified=mode in ("manual", "edit") and form.get("verified") == "1",
            back=safe_next(str(form.get("back", ""))) if mode == "edit" else "",
        )

        def again(error: str) -> Response:
            f.error = error
            body = f'<section class="card">{food_form(f)}</section>'
            if mode == "edit":
                return new_food_page(request, body, 400, title="Edit food", back=f.back)
            return new_food_page(request, body, 400)

        try:
            parsed = {k: parse_value(k, v) for k, v in values.items()}
        except ValueError as err:
            return again(str(err))
        if not f.name:
            return again("A food needs a name.")
        missing = [FORM_LABELS[k] for k in ("serving_g", *logic.CORE) if parsed[k] is None]
        if missing:
            return again(f"Fill in {', '.join(missing)} from the label.")
        serving = parsed.pop("serving_g")
        if sum(parsed[k] for k in ("protein_g", "carbs_g", "fat_g")) > serving * 1.05:
            return again(
                "Protein, carbs and fat add up to more than the serving weighs. Check the"
                " serving size and those values."
            )
        if mode == "edit":
            return await save_edited_food(request, f, parsed, serving)
        try:
            gtin = gtins.normalize(f.barcode) if f.barcode else None
        except gtins.InvalidBarcode:
            return again("That barcode isn't valid. Check the digits, or leave it empty.")
        details = {
            "name": f.name,
            "brand": f.brand or None,
            "serving_desc": f.serving_desc or None,
        }
        with connect(request) as conn:
            if mode == "barcode":
                try:
                    row = store.get_food(conn, f.food_id or 0)
                except store.NotFound:
                    return go(new_food_url(barcode=f.barcode))
                corrections, restated = changed_values(row, parsed, serving)
                if restated or row["serving_g"]:
                    details["serving_g"] = serving
                row, _ = store.confirm_food(conn, row["id"], corrections, details)
                confirmed = True
            else:
                confirmed = f.verified or (
                    mode == "label"
                    and {
                        "serving_g",
                        *(k for k, v in parsed.items() if v is not None),
                    }
                    <= f.ticks
                )
                try:
                    per_100g = logic.per_serving_to_per_100g(parsed, serving)
                except ValueError as err:
                    return again(str(err))
                row, _ = store.save_label_food(
                    conn,
                    name=f.name,
                    brand=f.brand or None,
                    gtin=gtin,
                    per_100g=per_100g,
                    serving_g=serving,
                    serving_desc=f.serving_desc or None,
                    source="label" if mode == "label" else "manual",
                    verified=confirmed,
                )
        log.info("dashboard: added food %d (%s, confirmed=%s)", row["id"], mode, confirmed)
        return go(f"{PREFIX}/foods?{urlencode({'q': row['name'], 'added': row['id']})}")

    # --- Food details and edits (#72) ---

    def changed_values(row, parsed: dict, serving: float) -> tuple[dict, bool]:
        """Per-100 g corrections for the values the person changed on a food's form (the rest
        stay as stored, unrounded), and whether they gave a different serving size, which
        restates every value."""
        shown = scanned_values(row)
        restated = field_text("serving_g", serving) != shown["serving_g"]
        changed = {k: v for k, v in parsed.items() if restated or field_text(k, v) != shown[k]}
        return {k: None if v is None else v * 100.0 / serving for k, v in changed.items()}, (
            restated
        )

    async def save_edited_food(request: Request, f: FoodForm, parsed: dict, serving: float):
        food_id = f.food_id or 0
        back = f.back or food_url(food_id, f"{PREFIX}/foods")
        with connect(request) as conn, db.transaction(conn):
            try:
                row = store.get_food(conn, food_id, live=True)
            except store.NotFound:
                return message("Not found", "That food no longer exists.", 404, signed_in=True)
            corrections, restated = changed_values(row, parsed, serving)
            details: dict[str, str | float | None] = {
                "name": f.name,
                "brand": f.brand or None,
                "serving_desc": f.serving_desc or None,
            }
            # A food with no serving size shows 100 g; describing that serving keeps it
            if restated or row["serving_g"] or f.serving_desc:
                details["serving_g"] = serving
            before = store.capture(conn, foods=[food_id])
            store.confirm_food(conn, food_id, corrections, details, confirmed=f.verified)
            undo_id = store.record_undo(conn, f"Edited {f.name}", before)
        log.info("dashboard: edited food %d (confirmed=%s)", food_id, f.verified)
        return go(with_params(back, done=undo_id))

    @server.custom_route(PREFIX + "/foods/{food_id:int}/edit", methods=["GET"])
    async def edit_food(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        food_id = request.path_params["food_id"]
        back = safe_next(
            request.query_params.get("back", "") or food_url(food_id, f"{PREFIX}/foods")
        )
        with connect(request) as conn:
            try:
                row = store.get_food(conn, food_id, live=True)
            except store.NotFound:
                return message("Not found", "That food no longer exists.", 404, signed_in=True)
        f = FoodForm(
            "edit",
            scanned_values(row),
            name=row["name"],
            brand=row["brand"] or "",
            serving_desc=row["serving_desc"] or "",
            food_id=food_id,
            verified=bool(row["verified"]),
            back=back,
        )
        body = f'<section class="card">{food_form(f)}</section>'
        return new_food_page(request, body, title="Edit food", back=back)

    def food_toasts(params) -> str:
        """The banners after confirming, deleting or changing a portion of a food."""
        out = ""
        if checked := params.get("checked", "")[:FOOD_NAME_MAX]:
            state = "confirmed" if params.get("ok") == "1" else "unconfirmed"
            out += toast(f"<span>Marked {e(checked)} as {state}.</span>", "status")
        if removed := params.get("removed", "")[:FOOD_NAME_MAX]:
            meals = int_param(params.get("meals")) or 0
            left = f" It left {meals} saved meal{'s' if meals != 1 else ''}." if meals else ""
            out += toast(f"<span>Deleted {e(removed)}.{e(left)}</span>", "status")
        if said := params.get("portion", "")[: PORTION_NAME_MAX + 40]:
            out += toast(f"<span>{e(said)}</span>", "status")
        return out

    @server.custom_route(PREFIX + "/foods/{food_id:int}", methods=["GET"])
    async def food_page(request: Request) -> Response:
        """One food (#72): name, brand and calories up top, protein, carbs and fat, the
        nutrition facts per serving and per 100 g, vitamins and minerals folded away, the
        person's portions, then how often they've logged it and where it came from."""
        if not signed_in(request):
            return to_login(request)
        food_id = request.path_params["food_id"]
        back = safe_next(request.query_params.get("back", "") or f"{PREFIX}/foods")
        here = food_url(food_id, back)
        with connect(request) as conn:
            try:
                row = store.get_food(conn, food_id, live=True)
            except store.NotFound:
                return message("Not found", "That food no longer exists.", 404, signed_in=True)
            uses, last_used = store.food_usage(conn, food_id)
            portions = store.portions_of(conn, [food_id])[food_id]
            banner = notice(conn, request.query_params, here)
        banner += food_toasts(request.query_params)
        per_100g = store.nutrients_of(row)
        serving_g = row["serving_g"]
        basis = logic.scale(per_100g, serving_g) if serving_g else per_100g
        if serving_g:
            grams = f"{num(serving_g, 1)}{NBSP}g"
            desc = row["serving_desc"]
            per = f"per serving · {e(desc)}" if desc and desc != grams else f"per {grams} serving"
            if desc and grams.replace(NBSP, "") not in desc.replace(" ", ""):
                per += f" ({grams})"
        else:
            per = "per 100 g"
        badge = (
            '<span class="badge ok">Confirmed</span>'
            if row["verified"]
            else '<span class="badge unsure">Unconfirmed</span>'
        )
        split = logic.macro_split(basis)
        rects, tiles, x = [], [], 0.0
        for key, cls, _ in MACROS:
            share = split[key] if split else 0.0
            rects.append(
                f'<rect class="fill {key}" x="{x:.1f}" y="0" width="{share:.1f}" height="10"/>'
            )
            x += share
            tiles.append(
                f'<div class="macro"><div class="small {cls}">{NUTRIENT_LABELS[key]}</div>'
                f"<div><strong>{amount(key, basis[key])}</strong></div>"
                f'<div class="muted small">{round(share)}% of kcal</div></div>'
            )
        parts = [
            '<header class="daynav">'
            f'<a class="step" href="{e(back)}" aria-label="Back to Foods">&lsaquo;</a>'
            '<span class="step off" aria-hidden="true"></span></header>',
            banner,
            f'<section class="card summary food-hero{"" if row["verified"] else " unconfirmed"}">'
            f'<h1 class="food-name">{e(row["name"])}</h1>'
            + (f'<p class="muted brand">{e(row["brand"])}</p>' if row["brand"] else "")
            + f'<p class="badges">{badge}</p>'
            f'<p class="hero"><span class="big kcal">{num(basis["kcal"])}</span> kcal'
            f' <span class="muted small">{per}</span></p>'
            f'<svg class="splitbar" viewBox="0 0 100 10" preserveAspectRatio="none"'
            f' aria-hidden="true">{"".join(rects)}</svg>'
            f'<div class="macros">{"".join(tiles)}</div></section>',
            fold("food-facts", "Nutrition facts", facts_table(per_100g, serving_g)),
        ]
        if micros := food_micros(per_100g, serving_g):
            parts.append(fold("food-micros", "Vitamins and minerals", micros, opened=False))
        parts.append(
            f'<section class="card" id="portions">{portions_block(row, portions, here)}</section>'
        )
        base = f"{PREFIX}/foods/{food_id}"
        keep = f'<input type="hidden" name="back" value="{e(here)}">'
        confirm = (
            f'<form method="post" action="{base}/confirm">{keep}'
            '<input type="hidden" name="confirmed" value="0">'
            '<button class="button ghost" type="submit">Mark unconfirmed</button></form>'
            if row["verified"]
            else f'<form method="post" action="{base}/confirm">{keep}'
            '<input type="hidden" name="confirmed" value="1">'
            '<button type="submit">Confirm</button></form>'
        )
        log_it = add_url(today(), None, row["name"])
        parts.append(
            '<section class="card actions">'
            f'<a class="button" href="{e(log_it)}">Log it</a>{confirm}'
            f'<a class="button ghost" href="{e(f"{base}/edit?" + urlencode({"back": here}))}">'
            "Edit</a>"
            '<details class="sub rename"><summary>Rename…</summary>'
            f'<form class="stack" method="post" action="{base}/rename">{keep}'
            f'<label>Name<input name="name" value="{e(row["name"])}" required'
            f' maxlength="{FOOD_NAME_MAX}"></label>'
            '<button type="submit">Save name</button></form></details>'
            '<details class="sub confirm"><summary>Delete…</summary>'
            f'<form class="stack" method="post" action="{base}/delete">'
            f'<input type="hidden" name="back" value="{e(back)}">'
            '<p class="small muted">Deletes it for everyone. Meals already logged keep their'
            " numbers; portions of it go, and it leaves any saved meal.</p>"
            f'<button class="danger" type="submit">Yes, delete {e(row["name"])}</button>'
            "</form></details></section>"
        )
        foot = []
        if uses:
            when = day_title(logic.local_date(last_used, tz), today())
            when = when.lower() if when in ("Today", "Yesterday") else when
            foot.append(f"Logged {uses} time{'s' if uses != 1 else ''}, last {e(when)}")
        else:
            foot.append("Not logged yet")
        foot.append(f"Source: {SOURCES[row['source']]}")
        parts.append(f'<p class="muted small center">{" · ".join(foot)}</p>')
        return render(
            row["name"],
            "".join(parts),
            "foods",
            signed_in=provider is not None,
            user=account(request),
        )

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
        with connect(request) as conn, db.transaction(conn):
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

    @server.custom_route(PREFIX + "/foods/{food_id:int}/confirm", methods=["POST"])
    async def confirm_food(request: Request) -> Response:
        """Confirm a food by hand (restaurant, home cooked, anything the person vouches
        for), or mark it unconfirmed again."""
        if (no := refused(request, "Confirm foods")) is not None:
            return no
        form = await request.form()
        back = safe_next(str(form.get("back", f"{PREFIX}/foods")))
        confirmed = form.get("confirmed") == "1"
        food_id = request.path_params["food_id"]
        with connect(request) as conn:
            try:
                row = store.set_food_confirmed(conn, food_id, confirmed)
            except store.NotFound:
                return message("Not found", "That food no longer exists.", 404, signed_in=True)
        log.info("dashboard: food %d confirmed=%s", food_id, confirmed)
        return go(with_params(back, checked=row["name"][:FOOD_NAME_MAX], ok=int(confirmed)))

    @server.custom_route(PREFIX + "/foods/{food_id:int}/delete", methods=["POST"])
    async def delete_food(request: Request) -> Response:
        if (no := refused(request, "Delete foods")) is not None:
            return no
        form = await request.form()
        back = safe_next(str(form.get("back", f"{PREFIX}/foods")))
        food_id = request.path_params["food_id"]
        with connect(request) as conn:
            try:
                food, meals = store.delete_food(conn, food_id)
            except store.NotFound:
                return message("Not found", "That food no longer exists.", 404, signed_in=True)
        log.info("dashboard: deleted food %d", food_id)
        return go(with_params(back, removed=food["name"][:FOOD_NAME_MAX], meals=meals))

    # --- Portions (#75) ---

    @server.custom_route(PREFIX + "/foods/{food_id:int}/portions", methods=["POST"])
    async def save_portion(request: Request) -> Response:
        """Add a portion, or change one (`old` names it) from the food's details. A weight
        typed here is exact, so it clears an estimate."""
        if (no := refused(request, "Change portions")) is not None:
            return no
        form = await request.form()
        back = safe_next(str(form.get("back", f"{PREFIX}/foods")))
        food_id = request.path_params["food_id"]
        old = str(form.get("old", ""))
        name = " ".join(str(form.get("name", "")).split())[:PORTION_NAME_MAX]
        try:
            grams = parse_grams(str(form.get("grams", "")))
            name = store.portion_name(name)
        except ValueError:
            return go(with_params(back, "portions", problem="portion"))
        with connect(request) as conn, db.transaction(conn):
            try:
                if old and store.portion_name(old).lower() != name.lower():
                    store.delete_portion(conn, food_id, old)
                row, _ = store.save_portion(conn, food_id, name, grams)
            except (store.NotFound, ValueError):
                return message("Not found", "That food or portion is gone.", 404, signed_in=True)
        log.info("dashboard: saved a portion of food %d", food_id)
        said = f"Saved 1 {row['name']} = {num(row['grams'], 1)} g."
        return go(with_params(back, "portions", portion=said))

    @server.custom_route(PREFIX + "/foods/{food_id:int}/portions/delete", methods=["POST"])
    async def delete_portion(request: Request) -> Response:
        if (no := refused(request, "Change portions")) is not None:
            return no
        form = await request.form()
        back = safe_next(str(form.get("back", f"{PREFIX}/foods")))
        food_id = request.path_params["food_id"]
        with connect(request) as conn:
            try:
                row = store.delete_portion(conn, food_id, str(form.get("old", "")))
            except (store.NotFound, ValueError):
                return message("Not found", "That portion is already gone.", 404, signed_in=True)
        log.info("dashboard: deleted a portion of food %d", food_id)
        said = f"Deleted the {row['name']} portion."
        return go(with_params(back, "portions", portion=said))

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
        with connect(request) as conn:
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
                here = f"{PREFIX}/history?days={span}"
                rows.append(
                    f'<li><a class="row" href="{PREFIX}?date={d.isoformat()}">'
                    f"<span>{e(day_title(d, last))}</span>"
                    f'<span><span class="kcal">{num(shown["kcal"])}</span>'
                    f' <span class="small muted">{macros_line(shown)}</span></span></a>'
                    f'<a class="small bd" href="{e(breakdown_url(d, back=here))}">'
                    f"Breakdown&nbsp;&rsaquo;</a></li>"
                )
            parts.append(
                f'<section class="card"><h2>Days</h2><ul class="days">{"".join(rows)}'
                "</ul></section>"
            )
        return render(
            "History",
            "".join(parts),
            "history",
            signed_in=provider is not None,
            user=account(request),
        )

    # --- Foods and templates ---

    def added_notice(conn, food_id: int) -> str:
        """The banner after New food saves one, with a link to log it."""
        try:
            row = store.get_food(conn, food_id)
        except store.NotFound:
            return ""
        state = "confirmed" if row["verified"] else "unconfirmed"
        log_it = add_url(today(), None, row["name"])
        return toast(
            f"<span>Saved {e(row['name'])} ({state}).</span>"
            f'<a class="undo" href="{e(log_it)}">Log it</a>',
            "status",
        )

    @server.custom_route(f"{PREFIX}/foods", methods=["GET"])
    async def foods(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        q = request.query_params.get("q", "").strip()[:100]
        view = request.query_params.get("view", "most")
        if view not in FOOD_VIEWS:
            view = "most"
        query = {k: v for k, v in (("q", q), ("view", view)) if v and v != "most"}
        back = f"{PREFIX}/foods" + (f"?{urlencode(query)}" if query else "")
        everything = view == "most"  # a filter narrows the list to foods only
        with connect(request) as conn:
            banner = notice(conn, request.query_params, back)
            if (added := int_param(request.query_params.get("added"))) is not None:
                banner += added_notice(conn, added)
        banner += food_toasts(request.query_params)
        with connect(request) as conn:
            if q:
                rows = store.search_foods(conn, q, FOODS_LIMIT, view)
                names = set(store.templates_matching(conn, q)) if everything else set()
                templates = [t for t in store.list_templates(conn) if t[0]["name"] in names]
            else:
                rows = store.list_used_foods(conn, FOODS_LIMIT, view)
                templates = store.list_templates(conn) if everything else []

        def view_url(v: str) -> str:
            params = {k: val for k, val in (("q", q), ("view", v)) if val and val != "most"}
            return f"{PREFIX}/foods" + (f"?{urlencode(params)}" if params else "")

        clear = f'<a class="clear" href="{e(view_url(view))}">Clear</a>' if q else ""
        keep_view = f'<input type="hidden" name="view" value="{view}">' if view != "most" else ""
        chips = "".join(
            f'<a href="{e(view_url(v))}"{" aria-current=page" if v == view else ""}>{label}</a>'
            for v, (label, _) in FOOD_VIEWS.items()
        )
        parts = [
            "<h1>Foods</h1>",
            banner,
            f'<form class="search" method="get" action="{PREFIX}/foods">{keep_view}'
            f'<input type="search" name="q" value="{e(q)}" placeholder="Foods and meals"'
            ' aria-label="Search foods and saved meals">'
            f'<button type="submit">Search</button>{clear}</form>',
            f'<nav class="segment filters" aria-label="Show">{chips}</nav>',
            f'<a class="button ghost wide" href="{PREFIX}/foods/new">Add a new food</a>',
        ]
        if q and not (rows or templates):
            parts.append(f'<section class="card empty"><p>Nothing matches “{e(q)}”.</p></section>')

        # Reusable meals first, so they're reachable without scrolling every food
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
            parts.append(fold("meals", "Saved meals", "".join(cards)))
        if rows:
            heading = "Foods" if q or not everything else "Your foods"
            note = f"{FOOD_VIEWS[view][1]}, " if not q or not everything else ""
            day = today()

            def last(row) -> str:
                if view != "recent" or not row["last_used"]:
                    return ""
                title = day_title(logic.local_date(row["last_used"], tz), day)
                return title.lower() if title in ("Today", "Yesterday") else title

            items = "".join(
                food_item(r, back, last(r), add_url(day, None, r["name"])) for r in rows
            )
            hint = (
                '<p class="small muted js-only hint">Tap a food to see it; press and hold it'
                " for more. Swipe left to delete, or right to confirm an amber one.</p>"
            )
            parts.append(
                fold(
                    "foods", heading, f'{hint}<ul class="entries">{items}</ul>', f"{note}per 100 g"
                )
            )
        elif not q:
            empty = "No saved foods yet." if everything else "No foods here."
            parts.append(f'<section class="card empty"><p>{empty}</p></section>')
        return render(
            "Foods", "".join(parts), "foods", signed_in=provider is not None, user=account(request)
        )
