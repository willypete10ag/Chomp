"""Web dashboard at /app: today's meals against targets, history charts, and saved foods.

Server-rendered HTML on the same Starlette app as /mcp: charts are inline SVG and every
edit (log food, change grams, move an entry, edit or combine meals, rename a food, set
goals, undo) is a plain form post that goes through store.py. A small script served from
here (static/app.js, no third-party code) adds sliders, quick picks, live previews and bottom
sheets (#105); the pages work without it. New food (#59) also loads static/scan.js and the
vendored ZXing reader for camera barcode scans, and reads label photos with tesseract
(chomp/label.py).
Phones can add it to the home screen (web app manifest at /app/manifest.webmanifest).

Undo: each edit stores the rows it touched before and after (undo_actions, migration 0005)
and the next page offers Undo, which only applies while those rows are unchanged.

Sign-in: with PUBLIC_URL set, a password page (each user's own password, sharing the consent
page's lockout; see chomp/users.py) starts a 30-day session for that user, and every page
shows only their log. The cookie holds a random token; only its SHA-256 hash is stored
(web_sessions). POSTs must come from the dashboard's own origin. In local-only mode the
dashboard needs no sign-in, like /mcp, and shows the owner's log.

Cloudflare Access may sit in front of /app (docs/setup.md, step 7), but never in front of /mcp
or the OAuth routes: Claude's servers can't complete a browser login.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import logging
import math
import re
import secrets
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from zoneinfo import ZoneInfo

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from chomp import (
    __version__,
    charts,
    db,
    export,
    health,
    health_export,
    hevy,
    hevy_sync,
    logic,
    serving_fill,
    store,
    users,
)
from chomp import label as label_reader
from chomp.auth import CONNECTOR_CALLBACKS, ChompAuthProvider
from chomp.barcode import lookup as barcode_lookup
from chomp.config import Settings
from chomp.photos import PhotoError, Photos
from chomp.sources import SourceError, rank
from chomp.sources import gtin as gtins
from chomp.sources.fdc import FDCClient
from chomp.sources.off import OFFClient

log = logging.getLogger("chomp.web")

PREFIX = "/app"
COOKIE = "chomp_session"
SESSION_TTL_S = 30 * 86400
# One range control on every Progress screen (#105): days, short label, what it reads as.
# 0 is All: since the first record of that kind.
RANGES = (
    (1, "Today", "today"),
    (7, "7D", "7 days"),
    (30, "30D", "30 days"),
    (182, "6M", "6 months"),
    (365, "1Y", "1 year"),
    (0, "All", "all time"),
)
PROGRESS = f"{PREFIX}/progress"
NUTRITION_PAGE = f"{PROGRESS}/nutrition"  # was /app/history
WEIGHT_PAGE = f"{PROGRESS}/weight"  # was /app/weight
ACTIVITY_PAGE = f"{PROGRESS}/activity"  # was /app/activity
TRAINING_PAGE = f"{PROGRESS}/training"  # was /app/workouts
YOU = f"{PREFIX}/you"
INTEGRATIONS = f"{YOU}/integrations"
GOALS_PAGE = f"{PREFIX}/targets"  # the daily nutrition goals
BODY_GOALS = f"{YOU}/body-goals"  # goal weight and step goal (#105)
GYM_GOAL = f"{YOU}/gym-goal"  # gym days a week
PEOPLE = f"{YOU}/people"  # the owner adds and removes people (#109)
INVITE = f"{PREFIX}/invite"  # an invite link: pick your password (#109)
SHORTCUT_LINK = "health_shortcut_url"  # app_settings key: the shared Send to Chomp Shortcut
SHORTCUT_LINK_RE = re.compile(r"https://www\.icloud\.com/shortcuts/[0-9a-f]{32}/?")
HEVY_KEY_RE = re.compile(r"[A-Za-z0-9_-]{8,100}")
ACTIONS = f"{PREFIX}/actions"  # the + sheet
SHORTCUTS = f"{PREFIX}/shortcuts"
# Keys app.js answers (#105), shown on the shortcuts sheet
SHORTCUT_KEYS = (
    (("/",), "Search foods, or log food on Today"),
    (("n",), "Log something"),
    (("\u2190", "\u2192"), "Day before or after, on Today"),
    (("g", "t"), "Go to Today"),
    (("g", "p"), "Go to Progress"),
    (("g", "f"), "Go to Foods"),
    (("g", "y"), "Go to You"),
    (("?",), "Show these shortcuts"),
    (("Esc",), "Close a sheet"),
)
WEIGHT = f"{PREFIX}/weight"  # posts: add a weigh-in, delete one
WEIGHT_SOURCES = {"health": "Apple Health", "chat": "Claude", "app": "Added here"}
WEIGHT_SOURCE_ICONS = {"health": "heart-pulse", "chat": "bot", "app": "pencil"}
WORKOUTS = f"{PREFIX}/workouts"  # Workouts page (#94)
HEVY_MAX = 20 * 1024 * 1024
# Health's export.zip; Cloudflare's tunnel takes 100 MB a request at most
HEALTH_MAX = 95 * 1024 * 1024
SET_MARKS = {"warmup": "W", "failure": "F", "dropset": "D"}
ACTIVITY = f"{PREFIX}/activity"  # Activity page (#94)
FOODS_LIMIT = 200
MAX_GRAMS = 10_000
LABEL_MAX, NOTE_MAX, FOOD_NAME_MAX = 60, 200, 120
PORTION_NAME_MAX = 40
ADD_LIMIT = 30  # foods listed on the Log food page
# Log food's pills (#105) when nothing is searched: ?view= -> its name
ADD_VIEWS = {
    "recent": "Recent",
    "frequent": "Frequent",
    "meals": "Saved meals",
    "recipes": "Recipes",
}
USDA_LIMIT = 10
AUTO_USDA_BELOW = 3  # Add food searches USDA by itself when fewer of your foods match (#88)
FOUND_LIMIT = 8  # results per source in New food's search (#88)
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
    "theme.js": "text/javascript",
}
# Lucide icons (lucide-static 0.460.0, ISC, LICENSE-lucide.txt), drawn inline by icon()
ICONS = dict(
    re.findall(
        r'<symbol id="([\w-]+)" viewBox="0 0 24 24">(.*?)</symbol>',
        (STATIC_DIR / "icons.svg").read_text(),
    )
)
THEME_LIGHT, THEME_DARK = "#f3f6f4", "#0b0e0d"
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
        " connect-src 'self'; manifest-src 'self'; media-src 'self' blob: mediastream:;"
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
MINUS = "\u2212"  # a real minus sign, for text set by the script (no HTML entities there)
MACROS = (("protein_g", "p", "P"), ("carbs_g", "c", "C"), ("fat_g", "f", "F"))
# Where a New food result's saved food came from, when another source found it (#97)
PROVENANCE = {
    "fdc": "from USDA",
    "off": "from Open Food Facts",
    "label": "from a label",
    "manual": "typed in",
}
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


def top_list(top: list[tuple], key: str) -> str:
    """A numbered list of the foods that gave the most of `key`: (name, confirmed, amount),
    or with a fourth item, the food's photo URL ("" for none) for a thumb (#105).
    Unconfirmed foods get an amber dot."""
    items = "".join(
        "<li>"
        + (thumb(t[3], t[0]) if len(t) > 3 else "")
        + '<span class="name">'
        + (
            ""
            if t[1]
            else '<span class="dot unsure" aria-hidden="true"></span>'
            '<span class="sr">Unconfirmed: </span>'
        )
        + f"{e(t[0])}</span><span>{amount(key, t[2])}</span></li>"
        for t in top
    )
    return f'<ol class="top{" thumbs" if top and len(top[0]) > 3 else ""}">{items}</ol>'


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


def delta_chip(pct: float | None, words: str, good: str = "") -> str:
    """ "▲ 12% vs prev 30D" (#105). good says which way is good ("up" or "down") so the
    chip turns green or amber; without it (weight, volume) the chip stays neutral."""
    if pct is None:
        return ""
    if abs(pct) < 0.5:
        return f'<span class="delta">no change <span>vs {e(words)}</span></span>'
    up = pct > 0
    tone = "" if not good else (" good" if up == (good == "up") else " bad")
    arrow = "▲" if up else "▼"
    return (
        f'<span class="delta{tone}"><span aria-hidden="true">{arrow}</span>'
        f'<span class="sr">{"up" if up else "down"}</span> {abs(pct):.0f}%'
        f" <span>vs {e(words)}</span></span>"
    )


def tile(value: str, label: str, sub: str = "", spark: str = "", tone: str = "") -> str:
    """A stat tile: caption, value, then a delta or context line, optionally a sparkline."""
    toned = f" tone-{tone}" if tone else ""
    under = f'<div class="sub">{sub}</div>' if sub else ""
    return (
        f'<div class="tile{toned}"><div class="cap">{e(label)}</div>'
        f'<div class="val">{value}</div>{under}{spark}</div>'
    )


def tiles(*items: str) -> str:
    return f'<div class="tiles">{"".join(items)}</div>'


def hero(value: str, unit: str, context: str, chips: str = "", extra: str = "") -> str:
    """The screen's one big number on a glow of its hue, with what it is and how it moved."""
    unit_html = f'<span class="unit">{e(unit)}</span>' if unit else ""
    chip_row = f'<div class="chips-row">{chips}</div>' if chips else ""
    return (
        f'<section class="card summary hero-metric"><div class="display">{value}{unit_html}'
        f'</div><p class="context">{context}</p>{chip_row}{extra}</section>'
    )


RING_R = 52  # the calorie ring's radius in its 120 x 120 box


def energy_ring(percent: float, tone: str = "") -> str:
    """Today's calorie ring (#105): eaten against the goal. Past the goal it closes and
    turns amber, then red (`tone`), like the number in it."""
    length = 2 * math.pi * RING_R
    shown = max(0.0, min(percent, 100.0)) / 100
    cls = f"arc {tone}".rstrip()
    return (
        '<svg class="ring" viewBox="0 0 120 120" aria-hidden="true">'
        f'<circle class="track" cx="60" cy="60" r="{RING_R}"/>'
        f'<circle class="{cls}" cx="60" cy="60" r="{RING_R}"'
        f' stroke-dasharray="{length * shown:.1f} {length:.1f}" transform="rotate(-90 60 60)"/>'
        "</svg>"
    )


def split_ring(totals: Mapping[str, float | None], kcal: str) -> str:
    """Breakdown's macro split as a ring (#105): protein, carbs and fat arcs by their share
    of the calories, the day's calories in the middle."""
    split = logic.macro_split(totals) or {}
    length = 2 * math.pi * RING_R
    arcs, start = [], 0.0
    for key, _, _ in MACROS:
        seg = length * split.get(key, 0.0) / 100
        if seg > 0:
            arcs.append(
                f'<circle class="seg {key}" cx="60" cy="60" r="{RING_R}"'
                f' stroke-dasharray="{max(seg - 3, 0.5):.1f} {length:.1f}"'
                f' stroke-dashoffset="{-start:.1f}" transform="rotate(-90 60 60)"/>'
            )
        start += seg
    return (
        '<div class="ring-box split-ring"><svg class="ring" viewBox="0 0 120 120"'
        f' aria-hidden="true"><circle class="track" cx="60" cy="60" r="{RING_R}"/>'
        f'{"".join(arcs)}</svg><div class="ring-num"><b>{kcal}</b><span>kcal</span></div></div>'
    )


def day_tile(
    href: str, glyph: str, tone: str, label: str, value: str, sub: str = "", *, sheet: bool = False
) -> str:
    """A Your day tile on Today (#105): a tinted tile with its metric's icon, opening that
    metric on Progress (or, with `sheet`, its link in a sheet)."""
    under = f'<div class="sub">{sub}</div>' if sub else ""
    opens = " data-sheet" if sheet else ""
    return (
        f'<a class="tile day-tile tone-{tone}" href="{e(href)}"{opens}><div class="cap">'
        f'{icon(glyph)}{e(label)}</div><div class="val">{value}</div>{under}</a>'
    )


def ago_text(day: date, today: date) -> str:
    """ "today", "yesterday", "3 days ago", or a date further back."""
    days = (today - day).days
    if days <= 1:
        return short_date(day, today)
    return f"{days} days ago" if days < 14 else short_date(day, today)


def prev_words(span: int) -> str:
    """ "prev 30D" for a range's previous period, "yesterday" for Today's."""
    if span == 1:
        return "yesterday"
    return "prev " + next(short for n, short, _ in RANGES if n == span)


def split_bar(totals: Mapping[str, float | None]) -> str:
    """A slim protein / carbs / fat bar: each one's share of the macro calories."""
    split = logic.macro_split(totals)
    if not split:
        return ""
    rects, x = [], 0.0
    for key, _, _ in MACROS:
        rects.append(
            f'<rect class="fill {key}" x="{x:.1f}" y="0" width="{split[key]:.1f}" height="10"/>'
        )
        x += split[key]
    return (
        '<svg class="splitbar slim" viewBox="0 0 100 10" preserveAspectRatio="none"'
        f' aria-hidden="true">{"".join(rects)}</svg>'
    )


def lb_text(kg: float) -> str:
    """A weight in lb, always to 0.1 (a scale reading of 183.0 shows as 183.0)."""
    return f"{logic.weight_out(kg)['lb']:,.1f}{NBSP}lb"


def change_text(kg: float) -> str:
    """A weight change in lb with its sign (HTML): "&minus;1.2 lb", "+0.4 lb", "no change"."""
    lb = logic.weight_out(abs(kg))["lb"]
    if lb == 0:
        return "no change"
    return f"{'&minus;' if kg < 0 else '+'}{num(lb, 1)}{NBSP}lb"


def weight_item(row, tz: str, back: str) -> str:
    """One weigh-in in the Weight page's list: time, where it came from, the weight. A tap
    opens its edit sheet (#105); with the script, swipe it left to delete it (after asking)."""
    local = logic.local_iso(row["measured_at"], tz)
    when = datetime.fromisoformat(local)
    label = f"{when.strftime('%a %b')} {when.day}, {clock(local)}"
    url = f"{WEIGHT}/{row['id']}"
    edit = f"{url}?{urlencode({'back': back})}"
    glyph = WEIGHT_SOURCE_ICONS.get(row["source"])
    source_icon = icon(glyph, f"icon src-{row['source']}") if glyph else ""
    delete = (
        f'<form class="swipe-action js-only" method="post" action="{url}/delete"'
        f' data-confirm="Delete the weigh-in from {e(label)}?">'
        f'<input type="hidden" name="back" value="{e(back)}">'
        f'<button type="submit" aria-label="Delete the weigh-in from {e(label)}">Delete'
        "</button></form>"
    )
    return (
        f'<li id="weight-{row["id"]}" class="swipe" data-sheet-hold="{e(edit)}">{delete}'
        f'<div class="swipe-body" data-swipe><a class="row food-link" href="{e(edit)}" data-sheet>'
        f'<span><span class="sr">Edit the weigh-in from </span>{e(label)}<br>'
        f'<span class="small muted source">{source_icon}'
        f"{e(WEIGHT_SOURCES.get(row['source'], ''))}</span></span>"
        f'<span class="kcal">{lb_text(row["kg"])}</span></a></div></li>'
    )


def duration_text(row) -> str:
    """A workout's length, "1 h 48 min" or "52 min", or "" without an end time."""
    if not row["ended_at"]:
        return ""
    delta = logic.parse_utc(row["ended_at"]) - logic.parse_utc(row["started_at"])
    minutes = max(round(delta.total_seconds() / 60), 0)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def set_text(s) -> str:
    """One set as people say it: "185 lb &times; 6", "12 reps", "1.25 mi · 20:15"."""
    parts = []
    lifted = (
        None
        if s["weight_kg"] is None
        else f"{num(logic.weight_out(s['weight_kg'])['lb'], 1)}{NBSP}lb"
    )
    if s["weight_kg"] is not None and s["reps"] is not None:
        parts.append(f"{lifted} &times; {s['reps']}")
    elif s["weight_kg"] is not None:
        parts.append(lifted)
    elif s["reps"] is not None:
        parts.append(f"{s['reps']} rep{'' if s['reps'] == 1 else 's'}")
    if s["distance_m"] is not None:
        parts.append(f"{s['distance_m'] / 1609.344:.2f}{NBSP}mi")
    if s["duration_s"] is not None:
        total = round(s["duration_s"])
        parts.append(f"{total // 60}:{total % 60:02d}")
    return " · ".join(parts) or "&ndash;"


def workout_summary(row, sets) -> str:
    exercises = len({x["exercise"] for x in sets})
    bits = [duration_text(row), f"{exercises} exercise{'' if exercises == 1 else 's'}"]
    if volume := logic.workout_volume_kg(sets):
        bits.append(f"{num(logic.weight_out(volume)['lb'])}{NBSP}lb lifted")
    return " · ".join(b for b in bits if b)


def workout_item(row, sets, tz: str) -> str:
    """A workout in the Workouts list: date, title and a summary. Tap opens it; with the
    script, swipe it left to delete it (after asking)."""
    local = logic.local_iso(row["started_at"], tz)
    when = datetime.fromisoformat(local)
    url = f"{WORKOUTS}/{row['id']}"
    title = row["title"]
    delete = (
        f'<form class="swipe-action js-only" method="post" action="{url}/delete"'
        f' data-confirm="Delete the workout {e(title)}? Importing an export that still has it'
        ' brings it back.">'
        f'<button type="submit" aria-label="Delete {e(title)}">Delete</button></form>'
    )
    return (
        f'<li id="workout-{row["id"]}" class="swipe">{delete}'
        f'<div class="swipe-body" data-swipe><a class="food-link" href="{url}"'
        ' draggable="false">'
        f'<div class="row"><span class="name clamp">{e(title)}</span>'
        f'<span class="small muted">{e(when.strftime("%a %b"))} {when.day}</span></div>'
        f'<div class="small muted">{workout_summary(row, sets)}</div></a></div></li>'
    )


def exercise_card(
    name: str, sets: list, records: set[int] = frozenset(), last: logic.LastTime | None = None
) -> str:
    """One exercise of a workout (#105): a table of its sets (warm-up, failure and drop sets
    marked, a PR badge on a record), and how it went last time. `sets` are (index, set)
    pairs; `records` the indexes that set a record."""
    notes = next((x["exercise_notes"] for _, x in sets if x["exercise_notes"]), None)
    rows = []
    number = 0
    with_rpe = any(x["rpe"] for _, x in sets)
    for i, x in sets:
        mark = SET_MARKS.get(x["set_type"])
        if mark is None:
            number += 1
        label = mark or str(number)
        pr = ' <span class="badge pr">PR</span>' if i in records else ""
        rpe = f'<td class="num">{num(x["rpe"], 1) if x["rpe"] else ""}</td>' if with_rpe else ""
        rows.append(
            f'<tr><td><span class="setno {x["set_type"]}" title="{e(x["set_type"])}">{label}'
            f"</span></td><td>{set_text(x)}{pr}</td>{rpe}</tr>"
        )
    note = f'<p class="muted small">{e(notes)}</p>' if notes else ""
    head = (
        '<thead><tr><th scope="col">Set</th><th scope="col">Weight &times; reps</th>'
        + ('<th scope="col" class="num">RPE</th>' if with_rpe else "")
        + "</tr></thead>"
    )
    then = ""
    if last is not None:
        bits = [f"{last.sets} set{'' if last.sets == 1 else 's'}"]
        if last.best is not None:
            bits.append(f"best {set_text(last.best)}")
        now_kg = logic.workout_volume_kg([x for _, x in sets])
        chip = ""
        if last.volume_kg and now_kg:
            chip = delta_chip((now_kg - last.volume_kg) / last.volume_kg * 100, "last time")
        then = (
            f'<p class="lasttime small"><span class="muted">Last time,'
            f" {e(last.day.strftime('%b'))} {last.day.day}:</span> {' · '.join(bits)}{chip}</p>"
        )
    return (
        f'<section class="card exercise"><h2>{e(name)}</h2>{note}'
        f'<table class="settable">{head}<tbody>{"".join(rows)}</tbody></table>{then}</section>'
    )


def hevy_sync_form() -> str:
    return (
        f'<form method="post" action="{WORKOUTS}/sync" data-busy="Syncing with Hevy">'
        '<button type="submit">Sync now</button></form>'
    )


def hevy_import() -> str:
    return (
        f'<form class="stack" method="post" action="{WORKOUTS}/import"'
        ' enctype="multipart/form-data" data-busy="Importing your workouts">'
        '<p class="muted small">In Hevy: Profile &rsaquo; Settings &rsaquo; Export &amp;'
        " Import Data &rsaquo; Export Workouts, then pick the CSV here. Importing again"
        " later only adds what's new.</p>"
        '<input type="file" name="csv" accept=".csv,text/csv" required>'
        '<button type="submit">Import</button></form>'
    )


def health_import() -> str:
    return (
        f'<form class="stack" method="post" action="{ACTIVITY}/import"'
        ' enctype="multipart/form-data" data-busy="Importing your Health data">'
        '<p class="muted small">Brings in your past weigh-ins and each day&rsquo;s activity.'
        " In Health: tap your picture &rsaquo; Export All Health Data &rsaquo; Save to Files,"
        " then pick export.zip here. Days your phone already sent stay as they are. A big"
        " export takes a minute.</p>"
        '<input type="file" name="export" accept=".zip,.xml,application/zip" required>'
        '<button type="submit">Import</button></form>'
    )


def short_date(day: date, today: date) -> str:
    """ "today", "yesterday", "Oct 14", or "Oct 14, 2024" in another year."""
    if day == today:
        return "today"
    if day == today - timedelta(days=1):
        return "yesterday"
    text = f"{day.strftime('%b')} {day.day}"
    return text if day.year == today.year else f"{text}, {day.year}"


def fresh_tag(href: str, glyph: str, source: str, when: str) -> str:
    """Where a page's data comes from and when it last came (#105); it opens Integrations."""
    return f'<a class="fresh-tag" href="{href}">{icon(glyph)}<b>{e(source)}</b> · {e(when)}</a>'


ACTIVITY_LIST_MAX = 100


SUMMARY_METRICS = {"steps", "active_kcal", "resting_kcal", "exercise_min", "distance_m"}


def steps_text(day: Mapping[str, float]) -> str:
    return f"{num(day['steps'])}{NBSP}steps" if "steps" in day else "&ndash;"


@dataclass(frozen=True)
class ActivityChart:
    """One of Activity's metrics for its switcher (#105): what it's called, the stored metric,
    its bar color, and how a value reads. "burned" is active + resting (logic.burned_kcal)."""

    label: str
    metric: str
    kind: str
    unit: str
    places: int = 0

    def value(self, day: Mapping[str, float]) -> float | None:
        if self.metric == "burned":
            return logic.burned_kcal(day)
        v = day.get(self.metric)
        if v is None:
            return None
        return logic.miles(v) if self.metric == "distance_m" else v

    def text(self, v: float) -> str:
        return f"{num(v, self.places)}{NBSP}{self.unit}"


ACTIVITY_CHARTS = {
    "steps": ActivityChart("Steps", "steps", "steps", "steps"),
    "energy": ActivityChart("Burned", "burned", "burn", "kcal"),
    "exercise": ActivityChart("Exercise", "exercise_min", "exercise", "min"),
    "distance": ActivityChart("Distance", "distance_m", "distance", "mi", 1),
}


def activity_row(day: date, values: Mapping[str, float], today: date, href: str) -> str:
    """A day in Activity's list: the date and steps, and one line of energy and exercise. Tap
    opens the day's numbers in a sheet."""
    bits = []
    if (burned := logic.burned_kcal(values)) is not None:
        bits.append(f"{num(burned)}{NBSP}kcal burned")
    if "exercise_min" in values:
        bits.append(f"{num(values['exercise_min'])}{NBSP}min exercise")
    sub = f"<small>{' · '.join(bits)}</small>" if bits else ""
    return (
        f'<li><a class="item" href="{e(href)}" data-sheet><span class="grow">'
        f"{e(day_title(day, today))}{sub}</span>"
        f'<span class="kcal">{steps_text(values)}</span>'
        f"{icon('chevron-right', 'icon chev')}</a></li>"
    )


# --- Pages ----------------------------------------------------------------------------------

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="{light}" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="{dark}" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Chomp">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="manifest" href="{p}/manifest.webmanifest" crossorigin="use-credentials">
<link rel="apple-touch-icon" href="{p}/static/apple-touch-icon.png">
<link rel="icon" href="{p}/static/icon-192.png">
<script src="{p}/static/theme.js?v={theme_version}"></script>
<link rel="stylesheet" href="{p}/static/app.css?v={css_version}">
<script src="{p}/static/app.js?v={js_version}" defer></script>{scripts}
<title>{title}</title></head><body{body_class}{hue}>"""

# The tab bar (#105): four places and, in the middle, Log (the + sheet)
NAV = (
    ("today", "", "Today", "sun"),
    ("progress", "/progress", "Progress", "chart-no-axes-column-increasing"),
    ("foods", "/foods", "Foods", "apple"),
    ("you", "/you", "You", "circle-user"),
)
PROGRESS_AREAS = (
    ("overview", PROGRESS, "Overview"),
    ("nutrition", NUTRITION_PAGE, "Nutrition"),
    ("weight", WEIGHT_PAGE, "Weight"),
    ("activity", ACTIVITY_PAGE, "Activity"),
    ("training", TRAINING_PAGE, "Training"),
)


def icon(name: str, cls: str = "icon") -> str:
    return f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'


def page_head(title: str, actions: str = "") -> str:
    """A screen's large title (#105); the script shows it small in a bar once it scrolls
    away. `actions` are icon buttons on its right."""
    side = f'<div class="actions">{actions}</div>' if actions else ""
    return f'<header class="pagehead"><h1 data-title>{e(title)}</h1>{side}</header>'


def progress_pills(active: str) -> str:
    links = "".join(
        f'<a href="{url}"{" aria-current=page" if key == active else ""}>{label}</a>'
        for key, url, label in PROGRESS_AREAS
    )
    return f'<nav class="pills" aria-label="Progress">{links}</nav>'


def range_control(url: str, span: int) -> str:
    """Today 7D 30D 6M 1Y All, the same on every Progress screen (#105)."""
    links = "".join(
        f'<a href="{url}?days={n}" aria-label="{words}"'
        f"{' aria-current=page' if n == span else ''}>{short}</a>"
        for n, short, words in RANGES
    )
    return f'<nav class="segment" aria-label="Time range">{links}</nav>'


def pick_range(value: str | None, default: int) -> int:
    span = int_param(value)
    return span if span in {n for n, *_ in RANGES} else default


def range_words(span: int, first: date) -> str:
    """ "over 30 days", or for All "since Mar 2025"."""
    if span == 0:
        return f"since {first.strftime('%b')} {first.year}"
    if span == 1:
        return "today"
    return "over " + next(words for n, _, words in RANGES if n == span)


def item(href: str, glyph: str, tone: str, title: str, sub: str = "", **attrs: str) -> str:
    """A row in a grouped list (You, the + sheet): a tinted icon, a title and a line under
    it, and a chevron."""
    extra = "".join(f' {k.replace("_", "-")}="{e(v)}"' for k, v in attrs.items())
    note = f"<small>{sub}</small>" if sub else ""
    return (
        f'<a class="item" href="{e(href)}"{extra}><span class="badge-icon tone-{tone}">'
        f'{icon(glyph)}</span><span class="grow">{e(title)}{note}</span>'
        f"{icon('chevron-right', 'icon chev')}</a>"
    )


# Two columns on a wide screen (#105): a page marks where its main column starts (after
# the header, which spans both) and where the side column starts; render() wraps them. On a
# phone the two stack in the same order.
MAIN_COL, SIDE_COL = "<!--cols:main-->", "<!--cols:side-->"


def columns(body: str, cls: str = "") -> str:
    """A page in two columns past 64rem (#105): what comes before MAIN_COL spans both, then
    the main column up to SIDE_COL and the side column after it. One column on a phone."""
    if SIDE_COL not in body:
        return body.replace(MAIN_COL, "")
    head, _, rest = body.partition(MAIN_COL) if MAIN_COL in body else ("", "", body)
    main, _, side = rest.partition(SIDE_COL)
    return (
        f'{head}<div class="cols{" " + cls if cls else ""}"><div class="col-main">{main}</div>'
        f'<div class="col-side">{side.replace(SIDE_COL, "")}</div></div>'
    )


def subnav(key: str) -> str:
    """The areas under Progress and Foods, nested in the sidebar on a wide screen (#105);
    the script marks the one open. The phone's tab bar leaves them out."""
    areas = {
        "progress": [(url, label) for _, url, label in PROGRESS_AREAS],
        "foods": [(url, label) for _, url, label, _, _ in FOODS_SIDES],
    }.get(key)
    if not areas:
        return ""
    links = "".join(f'<a href="{url}">{label}</a>' for url, label in areas)
    return f'<div class="subnav">{links}</div>'


def render(
    title: str,
    body: str,
    active: str | None = None,
    *,
    signed_in: bool = False,
    user: str = "",
    status: int = 200,
    scripts: tuple[str, ...] = (),
    hue: str = "",
    log_date: date | None = None,
    refresh: bool = False,
) -> HTMLResponse:
    """A dashboard page. `scripts` are extra static scripts (STATIC) this page loads; `hue`
    tints the page with a metric's color (steps, train, weight, you); `log_date` is the day
    the + sheet logs to, when not today; `refresh` lets the script reload the page when
    the app comes back after a while away (#105). Signing out lives on You (#105)."""
    nav = ""
    if active is not None:
        links = [
            f'<a href="{PREFIX}{path}"{" aria-current=page" if key == active else ""}'
            f' aria-keyshortcuts="g {key[0]}">{icon(glyph)}<span>{label}</span></a>' + subnav(key)
            for key, path, label, glyph in NAV
        ]
        query = f"?{urlencode({'date': log_date.isoformat()})}" if log_date else ""
        links.insert(
            2,
            f'<a class="log" href="{ACTIONS}{query}" data-sheet aria-label="Log">'
            f"{icon('plus')}<span>Log</span></a>",
        )
        nav = (
            f'<nav class="tabs" aria-label="Chomp" data-shortcuts="{SHORTCUTS}">'
            f"{''.join(links)}</nav>"
        )
    page = (
        HEAD.format(
            light=THEME_LIGHT,
            dark=THEME_DARK,
            p=PREFIX,
            css_version=CSS_VERSION,
            js_version=JS_VERSION,
            theme_version=STATIC_VERSIONS["theme.js"],
            title=e(f"{title} · Chomp"),
            body_class=' class="has-tabs"' if nav else "",
            hue=(f' data-hue="{hue}"' if hue else "") + (" data-refresh" if refresh else ""),
            scripts="".join(
                f'<script src="{PREFIX}/static/{name}?v={STATIC_VERSIONS[name]}" defer></script>'
                for name in scripts
            ),
        )
        + '<a class="skip" href="#main">Skip to content</a>'
        + f'<main id="main" tabindex="-1">{columns(body)}</main>{nav}</body></html>'
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


def month_start(day: date) -> date:
    return day.replace(day=1)


def add_months(month: date, n: int) -> date:
    """The first of the month n months after `month`'s."""
    i = month.year * 12 + month.month - 1 + n
    return date(i // 12, i % 12 + 1, 1)


def calendar_url(month: date, shown: date) -> str:
    return f"{PREFIX}/calendar?{urlencode({'month': month.strftime('%Y-%m'), 'date': shown})}"


def month_calendar(
    month: date,
    shown: date,
    today: date,
    first: date,
    by_day: dict[date, logic.Nutrients],
    targets: dict[str, float | None],
) -> str:
    """The month view (#71): the month between arrows, a year box, then the days. A dot under
    days with anything logged, and a ring around days within STAR_WITHIN of every goal
    (#105). Months run from the first logged one to this one; future days can't be picked."""
    last = month_start(today)
    prev_month, next_month = add_months(month, -1), add_months(month, 1)
    prev = (
        f'<a class="step" href="{calendar_url(prev_month, shown)}" data-cal'
        ' aria-label="Previous month">&lsaquo;</a>'
        if prev_month >= first
        else '<span class="step off" aria-hidden="true">&lsaquo;</span>'
    )
    forward = (
        f'<a class="step" href="{calendar_url(next_month, shown)}" data-cal'
        ' aria-label="Next month">&rsaquo;</a>'
        if next_month <= last
        else '<span class="step off" aria-hidden="true">&rsaquo;</span>'
    )
    years = []
    for y in range(last.year, first.year - 1, -1):
        target = min(max(month.replace(year=y), first), last)
        current = ' aria-current="true"' if y == month.year else ""
        years.append(f'<a href="{calendar_url(target, shown)}" data-cal{current}>{y}</a>')
    year_box = (
        f'<details class="years"><summary aria-label="Pick a year">{month.year}'
        f' <span aria-hidden="true">&#9662;</span></summary>'
        f'<div class="yearlist">{"".join(years)}</div></details>'
    )
    head = f'<div class="calhead">{prev}<h2>{month.strftime("%B")}</h2>{forward}{year_box}</div>'
    names = "".join(f'<span aria-hidden="true">{d}</span>' for d in "SMTWTFS")
    cells = ['<span class="pad"></span>'] * ((month.weekday() + 1) % 7)  # Sunday first
    d = month
    while d.month == month.month:
        n = by_day.get(d)
        star = n is not None and logic.near_goals(n, targets)
        mark = '<i class="dot" aria-hidden="true"></i>' if n is not None else ""
        said = long_date(d, today) + (
            ", near your goals" if star else ", logged" if n is not None else ""
        )
        names_ = [c for c, on in (("is-today", d == today), ("met", star)) if on]
        cls = f' class="{" ".join(names_)}"' if names_ else ""
        current = ' aria-current="date"' if d == shown else ""
        if d <= today:
            cells.append(
                f'<a href="{PREFIX}?date={d.isoformat()}"{cls}{current}'
                f' aria-label="{e(said)}"><b>{d.day}</b>{mark}</a>'
            )
        else:
            cells.append(f'<span class="future" aria-hidden="true"><b>{d.day}</b></span>')
        d += timedelta(days=1)
    within = round(logic.STAR_WITHIN * 100)
    return (
        f'<div class="calendar" data-month="{month.strftime("%Y-%m")}">{head}'
        f'<div class="calgrid"><div class="calnames">{names}</div>'
        f'<div class="caldays">{"".join(cells)}</div></div>'
        '<div class="calfoot"><p class="muted small"><i class="dot" aria-hidden="true"></i>'
        f' Logged <i class="ring" aria-hidden="true"></i> Within {within}% of every goal'
        f'</p><a class="button ghost" href="{PREFIX}">Today</a></div></div>'
    )


@dataclass
class DayContext:
    """What an entry or food form on a day needs to know about that day."""

    day: date
    today: date
    meals: list[tuple[int, str]]  # (meal id, "8:05 AM · Breakfast") for the menus
    kcal: float  # calories logged that day
    goal: float | None  # the calorie target, if set
    meal_sizes: dict[int, int] = field(default_factory=dict)  # meal id -> how many entries


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


def form_field(
    name: str,
    label: str,
    value: str = "",
    *,
    unit: str = "",
    hint: str = "",
    inputmode: str = "decimal",
    attrs: str = "",
    cls: str = "",
) -> str:
    """Field (#105): a caption over a box with its unit inside it ("680 g"), and a hint
    under it. `unit` is HTML (a select can sit there); `attrs` go on the input as written."""
    kind = ' type="search"' if inputmode == "search" else ""
    mode = f' inputmode="{inputmode}"' if inputmode not in ("text", "search") else ""
    suffix = f'<span class="unit">{unit}</span>' if unit else ""
    note = f'<small class="fld-hint">{hint}</small>' if hint else ""
    caption = f'<span class="fld-label">{label}</span>' if label else ""
    return (
        f'<label class="fld{" " + cls if cls else ""}">{caption}'
        f'<span class="fld-box"><input name="{name}" value="{e(value)}"{kind}{mode}'
        f' autocomplete="off"{" " + attrs if attrs else ""}>{suffix}</span>{note}</label>'
    )


def stepper(
    name: str,
    value: str,
    *,
    what: str,
    label: str = "",
    step: float = 1,
    low: float = 0,
    unit: str = "",
    grams_step: float | None = None,
) -> str:
    """Stepper (#105): minus and plus either side of a number box with its unit inside, a `step` at
    a time and never under `low` (`grams_step` instead while a unit select beside it says
    g). The buttons come with the script; the box alone submits the same value. `what`
    names the amount for screen readers ("servings of Banana"); `unit` is HTML."""
    suffix = f'<span class="unit">{unit}</span>' if unit else ""
    caption = f'<span class="fld-label" aria-hidden="true">{label}</span>' if label else ""
    by_grams = f' data-step-g="{grams_step:g}"' if grams_step else ""
    return (
        f'<div class="fld">{caption}'
        f'<span class="steps" data-stepper data-step="{step:g}" data-min="{low:g}"{by_grams}>'
        f'<button type="button" class="stepper js-only" data-dir="-1" aria-label="Less: {e(what)}">'
        f"{icon('minus')}</button>"
        f'<span class="fld-box"><input name="{name}" value="{e(value)}" inputmode="decimal"'
        f' autocomplete="off" aria-label="{e(capitalized(what))}" required>{suffix}</span>'
        f'<button type="button" class="stepper js-only" data-dir="1" aria-label="More: {e(what)}">'
        f"{icon('plus')}</button></span></div>"
    )


def back_head(back: str, where: str, title: str, actions: str = "", sub: str = "") -> str:
    """A pushed page's header (#105): a back chevron to `where` it came from, the large
    title (small in a bar once it scrolls away), a line under it, and icon buttons."""
    side = f'<div class="actions">{actions}</div>' if actions else ""
    under = f'<p class="pagesub">{sub}</p>' if sub else ""
    head = f'<div class="pagetitle"><h1 data-title>{e(title)}</h1>{under}</div>' if title else ""
    return (
        f'<header class="pagehead pushed"><a class="iconbtn back" href="{e(back)}"'
        f' aria-label="Back to {e(where)}">{icon("chevron-left")}</a>{side}{head}</header>'
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
    toggle (#80) with a minus / plus stepper in whole servings. Servings is picked when the amount
    is a whole or half number of servings. Both boxes are submitted and the toggle says which
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
        ' aria-label="One serving less">&minus;</button>'
        f'<input name="servings" data-servings inputmode="decimal" autocomplete="off"'
        f' aria-label="Servings" value="{e(servings)}">'
        '<button type="button" class="stepper js-only" data-step="1"'
        ' aria-label="One serving more">+</button>'
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
        # Only true for that day, so the script hides it while another date is picked
        day = (
            f'<span class="muted" data-today="{ctx.day.isoformat()}"><br>'
            f"{e(day_title(ctx.day, ctx.today))} after this:"
            f" <span data-day{warn}>{num(total)}{goal} kcal</span></span>"
        )
    return (
        f'<p class="preview small" aria-live="polite"><span class="kcal">'
        f'<span data-n="kcal">{num(n["kcal"])}</span>{NBSP}kcal</span> {macros}' + day + "</p>"
    )


def day_attrs(ctx: DayContext | None, base: float) -> str:
    if ctx is None:
        return ""
    goal = f' data-goal="{num(ctx.goal).replace(",", "")}"' if ctx.goal else ""
    return f' data-base="{round(base, 1)}"{goal}'


def meal_menu(
    ctx: DayContext,
    exclude: int | None,
    prompt: str,
    time: str,
    *,
    with_name: bool = False,
    with_date: bool = False,
) -> str:
    """'Add to' / 'Move to': another meal that day, or a new meal at a time (and name, and
    with_date another day up to today)."""
    options = "".join(
        f'<option value="{mid}">{e(title)}</option>' for mid, title in ctx.meals if mid != exclude
    )
    name = (
        f'<label>Meal name<input name="label" maxlength="{LABEL_MAX}"'
        ' placeholder="Optional, like Post-workout"></label>'
        if with_name
        else ""
    )
    when = f'<label>Time<input type="time" name="time" value="{time}"></label>'
    if with_date:
        when = (
            f'<div class="pair">{when}<label>Date<input type="date" name="date"'
            f' value="{ctx.day.isoformat()}" max="{ctx.today.isoformat()}"></label></div>'
        )
    return (
        f'<label>{prompt}<select name="to" data-new-meal>'
        f'<option value="new">A new meal</option>{options}</select></label>'
        f'<div class="new-meal">{when}{name}</div>'
    )


def time_nudges() -> str:
    return (
        '<div class="chips js-only"><button type="button" class="chip" data-nudge="-15">'
        '&minus;15 min</button><button type="button" class="chip" data-nudge="15">+15 min</button>'
        '<button type="button" class="chip" data-nudge="now">Now</button></div>'
    )


def hhmm(local_iso: str) -> str:
    return datetime.fromisoformat(local_iso).strftime("%H:%M")


def initial(name: str) -> str:
    return next((c for c in name if c.isalnum()), "·").upper()


def thumb(src: str, name: str, tone: str = "") -> str:
    """A food's photo as a small square (#74), or its first letter on a tile without one,
    tinted with `tone` (saved meals, recipes)."""
    if src:
        return f'<img class="thumb" src="{e(src)}" alt="" loading="lazy" decoding="async">'
    toned = f" tone-{tone}" if tone else ""
    return f'<span class="thumb none{toned}" aria-hidden="true">{e(initial(name))}</span>'


def with_thumb(src: str, name: str, inner: str, tone: str = "") -> str:
    return f'<div class="with-thumb">{thumb(src, name, tone)}<div class="grow">{inner}</div></div>'


def entry_row(x: store.EntryRow, single: bool, photo: str = "") -> str:
    """One logged food on Today: its photo, name, amount, calories and macros. Tapping it
    opens its edit sheet (#105), or the edit page without the script; with the script, swipe
    it right to show Delete (no asking: Undo brings it back), or press and hold it to edit.

    In a one-item meal the meal header already carries the calories, so the row leaves them out.
    """
    n = logic.round_for_display(x.nutrients)
    brand = f' <span class="muted">{e(x.brand)}</span>' if x.brand else ""
    kcal = "" if single else f'<span class="kcal">{num(n["kcal"])}{NBSP}<small>kcal</small></span>'
    url = f"{PREFIX}/entries/{x.id}"
    delete = (
        f'<form class="swipe-action start js-only" method="post" action="{url}/delete">'
        f'<button type="submit" aria-label="Delete {e(x.name)}">Delete</button></form>'
    )
    return (
        f'<li id="entry-{x.id}" class="swipe" data-sheet-hold="{url}">{delete}'
        f'<div class="swipe-body" data-swipe><a class="entry food-link" href="{url}" data-sheet>'
        + with_thumb(
            photo,
            x.name,
            f'<div class="row"><span class="name clamp"><span class="sr">Edit </span>{e(x.name)}'
            f"{brand}</span>{kcal}</div>"
            f'<div class="row small muted"><span>{grams_text(x.grams, x.estimated)}</span>'
            f"<span>{macros_line(n)}</span></div>",
        )
        + "</a></div></li>"
    )


def entry_editor(
    x: store.EntryRow,
    food,
    ctx: DayContext,
    meal_time: str,
    *,
    by: str = "",
    typed: str | None = None,
) -> str:
    """An entry's edit sheet (#105): its amount by slider, servings or grams with a live
    preview of it and the day, then move it to another meal or delete it (Undo brings it
    back, so it doesn't ask first). `by` and `typed` put back an amount that was refused
    (`typed` is None when nothing was)."""
    n = logic.round_for_display(x.nutrients)
    problem = ""
    if typed is not None:
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
    meal = next((title for mid, title in ctx.meals if mid == x.meal_id), "")
    brand = f"{e(x.brand)} · " if x.brand else ""
    move = ""
    if len(ctx.meals) > 1 or ctx.meal_sizes.get(x.meal_id, 1) > 1:
        move = (
            f'<details class="opt"><summary class="item">'
            f'<span class="badge-icon tone-protein">{icon("arrow-right-left")}</span>'
            '<span class="grow">Move to another meal</span></summary>'
            f'<form class="stack" method="post" action="{PREFIX}/entries/{x.id}/move">'
            + meal_menu(ctx, x.meal_id, "Move to", meal_time)
            + '<button type="submit">Move</button></form></details>'
        )
    return (
        f'<h2 id="entry-title" class="clamp">{e(x.name)}</h2>'
        f'<p class="sheet-sub muted small">{brand}{e(meal)}</p>'
        + problem
        + f'<form class="stack" method="post" action="{PREFIX}/entries/{x.id}"'
        f' data-per100="{per100_attr(per_100g)}"{day_attrs(ctx, base)}>'
        + amount_picker(food, x.grams, slider_top(x.grams), by=by, typed=typed or "")
        + grams_chips(food, current=x.grams)
        + preview(per_100g, x.grams, ctx, base)
        + '<button type="submit">Save</button></form>'
        + (f'<p class="muted small">{extras}</p>' if extras else "")
        + '<div class="group">'
        + move
        + item(
            food_url(x.food_id, day_url_of(ctx.day)),
            "apple",
            "kcal",
            "Open the food",
            "Its nutrition, portions and photo",
        )
        + f'<form method="post" action="{PREFIX}/entries/{x.id}/delete">'
        f'<button class="item danger" type="submit"><span class="badge-icon tone-over">'
        f'{icon("trash-2")}</span><span class="grow">Delete from the log</span></button>'
        "</form></div>"
    )


def day_url_of(day: date) -> str:
    return f"{PREFIX}?date={day.isoformat()}"


# Foods page sorts (#91): sort -> its name in the Sort by menu (store.FOOD_SORTS orders them)
FOOD_SORTS = {
    "most": "Most logged",
    "recent": "Recent",
    "new": "Newest",
    "unconfirmed": "Unconfirmed first",
    "kcal": "Calories",
    "protein": "Protein",
    "fat": "Fat",
    "carbs": "Carbs",
}
# The Foods list's sort, direction and per 100 g / per serving, remembered on this device
FOODS_COOKIE = "chomp_foods"


def foods_order(params: Mapping[str, str], cookie: str) -> tuple[str, bool, bool]:
    """The Foods list's (sort, high to low, per serving): from the page's query when it
    sets any of them, else as last chosen on this device (`cookie`, "sort.dir.per"),
    else most logged, per serving."""
    if not any(k in params for k in ("sort", "dir", "per")):
        sort, _, rest = cookie.partition(".")
        direction, _, per = rest.partition(".")
        params = {"sort": sort, "dir": direction, "per": per}
    sort = params.get("sort", "most")
    if sort not in FOOD_SORTS:
        sort = "most"
    # Per serving unless per 100 g was picked (#105): a serving is what gets eaten
    return sort, params.get("dir") != "up", params.get("per") != "100g"


# How each Foods sort reads high to low, then low to high (#105)
SORT_ORDERS = {
    "most": ("Most logged first", "Least logged first"),
    "recent": ("Latest first", "Earliest first"),
    "new": ("Newest first", "Oldest first"),
    "unconfirmed": ("Unconfirmed first", "Confirmed first"),
}


def foods_list_url(q: str, sort: str, desc: bool, per_serving: bool) -> str:
    query = {"q": q} if q else {}
    query |= {"sort": sort, "dir": "down" if desc else "up"}
    query["per"] = "serving" if per_serving else "100g"
    return f"{PREFIX}/foods?{urlencode(query)}"


def sort_button(q: str, sort: str, desc: bool, per_serving: bool) -> str:
    """The sort button over the Foods list (#105): what it's sorted by and how values
    show; it opens the sort sheet."""
    query = {"q": q} if q else {}
    query |= {"sort": sort, "dir": "down" if desc else "up"}
    query["per"] = "serving" if per_serving else "100g"
    arrow = "&darr;" if desc else "&uarr;"
    told = (SORT_ORDERS.get(sort) or ("Highest first", "Lowest first"))[0 if desc else 1]
    per = "per serving" if per_serving else "per 100 g"
    return (
        f'<a class="chip sortbtn" href="{PREFIX}/foods/sort?{e(urlencode(query))}" data-sheet'
        f' aria-label="Sorted by {FOOD_SORTS[sort]}, {told.lower()}, {per}. Change">'
        f"{icon('arrow-up-down')}<span>{FOOD_SORTS[sort]} {arrow}</span>"
        f'<span class="muted">· {"/serving" if per_serving else "/100 g"}</span></a>'
    )


def choice(href: str, label: str, on: bool, sub: str = "") -> str:
    """A row in an option sheet: the picked one carries a check."""
    note = f"<small>{sub}</small>" if sub else ""
    mark = icon("check", "icon check") if on else ""
    current = ' aria-current="true"' if on else ""
    return (
        f'<a class="item choice" href="{e(href)}"{current}><span class="grow">{label}{note}'
        f"</span>{mark}</a>"
    )


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
    """The person's portions of a food as a list group (#105), and a row to add one."""
    lis = "".join(portion_item(row["id"], p, back) for p in portions)
    listed = f'<ul class="entries portions">{lis}</ul>' if lis else ""
    none = "" if lis else '<p class="body muted small">None yet. Claude learns them as you log.</p>'
    return (
        f'<section class="group" id="portions"><h3 class="group-cap">Your portions</h3>{listed}'
        f'{none}<details class="opt add-portion"><summary class="item"><span class="badge-icon'
        f' tone-kcal">{icon("plus")}</span><span class="grow">Add a portion<small>Like a'
        f" slice, a scoop or a handful</small></span>{icon('chevron-right', 'icon chev')}"
        f'</summary><form class="stack" method="post" action="{PREFIX}/foods/{row["id"]}/portions">'
        f'<input type="hidden" name="back" value="{e(back)}">'
        f'<label>Name<input name="name" placeholder="handful, slice, scoop" required'
        f' maxlength="{PORTION_NAME_MAX}"></label>'
        f'<label>Grams in one<input name="grams" type="number" inputmode="decimal"'
        f' step="any" min="0.1" max="{MAX_GRAMS}" required></label>'
        '<button type="submit">Add portion</button></form></details></section>'
    )


def food_url(food_id: int, back: str) -> str:
    """A food's details page (#72), with the list to go back to."""
    return f"{PREFIX}/foods/{food_id}?{urlencode({'back': back})}"


def more_link(url: str, name: str) -> str:
    """A row's … (#105): every gesture on it has this visible way in. It opens the row's
    menu as a sheet, or its page without the script."""
    return (
        f'<a class="more" href="{e(url)}" data-sheet aria-label="More for {e(name)}">'
        f"{icon('ellipsis')}</a>"
    )


def food_item(
    row,
    back: str,
    last_logged: str = "",
    log_url: str = "",
    photo: str = "",
    per_serving: bool = False,
) -> str:
    """A saved food: photo, name (two lines at most), brand, kcal and a macro line per serving
    (`per_serving`; a food with no serving size keeps per 100 g, tagged) or per 100 g, and
    an amber edge while unconfirmed. A tap opens its page (#72); its … opens its menu
    (#105). With the script, press and hold it for the same menu over the blurred page,
    swipe it left to delete it, and swipe an unconfirmed one right to confirm it.
    `last_logged` says when it was last logged; `log_url` logs it; `photo` is its
    picture's URL, if it has one."""
    n = store.nutrients_of(row)
    if per_serving and row["serving_g"]:
        n = logic.scale(n, row["serving_g"])
    n = logic.round_for_display(n)
    meta = []
    if per_serving and not row["serving_g"]:
        meta.append('<span class="badge">100 g</span>')
    if row["brand"]:
        meta.append(e(row["brand"]))
    if last_logged:
        meta.append(f"last {e(last_logged)}")
    if not row["verified"]:
        meta.append('<span class="unsure-text">Unconfirmed</span>')
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
    menu = f"{base}/menu?{urlencode({'back': back})}"
    return (
        f'<li id="food-{row["id"]}" class="swipe{"" if row["verified"] else " unconfirmed"}"'
        f' data-food="{base}" data-name="{e(row["name"])}" data-back="{e(back)}"'
        f' data-log="{e(log_url)}" data-confirmed="{int(bool(row["verified"]))}"'
        f' data-photo="{int(bool(photo))}">'
        f'{confirm}{delete}<div class="swipe-body lrow" data-swipe>'
        f'<a class="food-link" href="{e(food_url(row["id"], back))}" draggable="false">'
        + with_thumb(
            photo,
            row["name"],
            f'<div class="row"><span class="name clamp">{e(row["name"])}</span>'
            f'<span class="kcal">{num(n["kcal"])} <small>kcal</small></span></div>'
            f'<div class="row small muted"><span class="meta">{" · ".join(meta)}</span>'
            f"<span>{macros_line(n)}</span></div>",
        )
        + f"</a>{more_link(menu, row['name'])}</div></li>"
    )


def photo_form(food_id: int, back: str, text: str, cls: str) -> str:
    """Pick a photo for a food (#74): choosing one sends it (the script); without the
    script a Save photo button does."""
    return (
        f'<form class="photo-form" method="post" action="{PREFIX}/foods/{food_id}/photo"'
        f' enctype="multipart/form-data"><input type="hidden" name="back" value="{e(back)}">'
        f'<label class="{cls}"><span>{text}</span><input type="file" name="photo"'
        ' accept="image/*" required data-autosubmit></label>'
        '<button class="no-js" type="submit">Save photo</button></form>'
    )


def photo_block(food_id: int, name: str, src: str, back: str) -> str:
    """The top of a food's page (#74, #105): its photo edge to edge with a camera button to
    replace it (removing it is in the … menu), or a tile to add one. /app/foods/<id>#photo
    lands here: the connector hands Claude that link for adding a photo."""
    if not src:
        return (
            '<div class="food-photo none" id="photo">'
            + photo_form(
                food_id,
                back,
                f"{icon('camera')}<b>Add a photo</b><small>Take one, or pick one from your"
                " photos</small>",
                "photo-add",
            )
            + "</div>"
        )
    return (
        f'<div class="food-photo" id="photo"><img src="{e(src)}" alt="Photo of {e(name)}">'
        + photo_form(
            food_id, back, f'{icon("camera")}<span class="sr">Replace photo</span>', "photo-swap"
        )
        + "</div>"
    )


def detail_bar(back: str, where: str, menu: str, name: str) -> str:
    """The bar over a food, saved meal or recipe (#105): back to its list, and its … menu."""
    return (
        '<header class="detailbar">'
        f'<a class="iconbtn" href="{e(back)}" aria-label="Back to {where}">'
        f"{icon('chevron-left')}</a>"
        f'<a class="iconbtn" href="{e(menu)}" data-sheet aria-label="More for {e(name)}">'
        f"{icon('ellipsis')}</a></header>"
    )


def initials_art(name: str, tone: str, glyph: str) -> str:
    """The hero of a saved meal or recipe, which have no photo: its first letter on its
    color, with its kind's icon."""
    return (
        f'<div class="detail-art tone-{tone}" aria-hidden="true"><b>{e(initial(name))}</b>'
        f"{icon(glyph)}</div>"
    )


def macro_tiles(n: logic.Nutrients, basis: str, caption: str = "Calories") -> str:
    """Calories (`basis` under them: "per serving") and the three macros as tiles (#105),
    each macro with its share of the calories, then the split as one bar."""
    split = logic.macro_split(n)
    shown = logic.round_for_display(n)
    rects, cells, x = [], [], 0.0
    for key, _, _ in MACROS:
        share = split[key] if split else 0.0
        rects.append(
            f'<rect class="fill {key}" x="{x:.1f}" y="0" width="{share:.1f}" height="10"/>'
        )
        x += share
        tone = key.removesuffix("_g")
        cells.append(
            tile(
                amount(key, shown[key]), NUTRIENT_LABELS[key], f"{round(share)}% of kcal", tone=tone
            )
        )
    kcal = tile(f"{num(shown['kcal'])}<small> kcal</small>", caption, tone="kcal")
    return (
        f'<div class="tiles macro-tiles">{kcal}{"".join(cells)}</div>'
        '<svg class="splitbar" viewBox="0 0 100 10" preserveAspectRatio="none"'
        f' aria-hidden="true">{"".join(rects)}</svg>'
    )


def basis_switch(name: str, options: list[tuple[str, str, str]]) -> str:
    """A segmented control that swaps what the page shows without a reload (#105): radios
    the stylesheet reads (.basis-<value> blocks), so it works without the script. Each
    option is (value, label, block)."""
    labels = "".join(
        f'<label><input type="radio" name="{name}" value="{value}"'
        f"{' checked' if i == 0 else ''}>{label}</label>"
        for i, (value, label, _) in enumerate(options)
    )
    blocks = "".join(
        f'<div class="basis basis-{value}">{block}</div>' for value, _, block in options
    )
    return (
        f'<div class="segment switch" role="radiogroup" aria-label="Show values">{labels}</div>'
        + blocks
    )


def log_bar(href: str, label: str = "Log it") -> str:
    """The detail pages' Log it (#105), kept at the bottom of the screen over the tab bar."""
    return (
        f'<div class="logbar"><a class="button" href="{e(href)}" data-sheet>'
        f"{icon('plus')}<span>{label}</span></a></div>"
    )


def list_row(href: str, name: str, sub: str, value: str, photo: str = "", tone: str = "") -> str:
    """A row in a detail page's list group: a saved meal's foods, a recipe's ingredients."""
    return (
        f'<a class="item" href="{e(href)}">{thumb(photo, name, tone)}<span class="grow">'
        f'<span class="clamp">{e(name)}</span><small>{sub}</small></span>'
        f'<span class="kcal">{value}</span>{icon("chevron-right", "icon chev")}</a>'
    )


def facts_table(per_100g: logic.Nutrients, grams: float | None, head: str) -> str:
    """A food's nutrition facts in label order, for `grams` of it (else per 100 g), under
    `head` ("Per serving · 1 cup (240 g)"). Rows the food has no value for are left out."""
    n = logic.scale(per_100g, grams) if grams else dict(per_100g)
    rows = []
    for key, indent in FACTS:
        if key == "net_carbs":
            value, label, unit_key = logic.net_carbs(n), "Net carbs", "carbs_g"
        else:
            value, label, unit_key = n.get(key), NUTRIENT_LABELS[key], key
        if value is None:
            continue
        cls = ' class="indent"' if indent else (' class="top"' if key == "kcal" else "")
        rows.append(f'<tr{cls}><th scope="row">{label}</th><td>{amount(unit_key, value)}</td></tr>')
    return (
        '<table class="label-table"><thead><tr><th scope="col" colspan="2">'
        f"{head}</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
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
        if k not in ("done", "undone", "problem", "checked", "ok", "portion", "pic", "weighed")
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
    "photo": "That file isn't a photo Chomp can read. Try a JPEG or PNG.",
    "photo_big": "That photo is too big. Try one under 20 MB.",
    "weight": "Weight must be a number of pounds, like 182.4.",
    "meal_name": "A saved meal needs a name.",
    "meal_taken": "You already have a saved meal by that name. Pick another.",
    "hevy": "That file isn't a Hevy workout export. In Hevy: Profile > Settings > Export &"
    " Import Data > Export Workouts.",
    "hevy_big": "That file is too big for a Hevy export (20 MB at most).",
    "hevy_sync": "Syncing with Hevy didn't work. The Hevy card says why.",
    "hevy_key": "Hevy didn't accept that key, so it wasn't saved. Copy it again from"
    " hevy.com/settings?developer (it needs Hevy Pro).",
    "hevy_key_bad": "That doesn't look like a Hevy API key. Copy it from"
    " hevy.com/settings?developer.",
    "shortcut_link": "That isn't a Shortcut's iCloud link. In Shortcuts, press and hold the"
    " Shortcut, then Share > Copy iCloud Link; it starts with https://www.icloud.com/shortcuts/.",
    "health": "That file isn't Apple Health's export. In Health: tap your picture > Export All"
    " Health Data, then pick export.zip.",
    "health_big": "That export is over 95 MB, too big to send through the browser. Import it"
    " on the server instead (docs/apple-health.md).",
}
PHOTO_TOASTS = {"saved": "Photo saved.", "removed": "Photo removed."}

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
    row,
    ctx: DayContext,
    meal_id: int | None,
    q: str,
    default_time: str,
    portions=(),
    photo: str = "",
    *,
    quick: float | None = None,
    quick_estimated: bool = False,
) -> str:
    """A food on Log food: tap it for a grams slider, a live preview and where to log it.
    `quick` is the grams it was last logged at, which its + logs again in one tap (#105)."""
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
        f'<input type="hidden" name="date" value="{ctx.day.isoformat()}">'
        if meal_id is not None
        else meal_menu(ctx, None, "Add to", default_time, with_name=True, with_date=True)
    )
    again = ""
    if quick:
        target = (
            f'<input type="hidden" name="meal" value="{meal_id}">'
            if meal_id is not None
            else f'<input type="hidden" name="to" value="new">'
            f'<input type="hidden" name="time" value="{default_time}">'
        )
        again = (
            f'<form class="quick" method="post" action="{PREFIX}/log">'
            f'<input type="hidden" name="food_id" value="{row["id"]}">'
            f'<input type="hidden" name="grams" value="{round(quick, 1)}">'
            f'<input type="hidden" name="estimated" value="{int(quick_estimated)}">'
            f'<input type="hidden" name="date" value="{ctx.day.isoformat()}">'
            f'<input type="hidden" name="q" value="{e(q)}">{target}'
            f'<button type="submit" aria-label="Log {num(quick, 1)} g of {e(row["name"])}'
            f' again">{icon("plus")}<span>{grams_text(quick, quick_estimated)}</span></button>'
            "</form>"
        )
    return (
        f'<li class="pick{" has-quick" if again else ""}"><details><summary>'
        + with_thumb(
            photo,
            row["name"],
            f'<div class="row"><span class="name clamp">{e(row["name"])}</span>'
            f'<span class="kcal">{num(n["kcal"])} <small>kcal</small></span></div>'
            f'<div class="row small muted"><span>{" · ".join(meta)}</span>'
            f"<span>{macros_line(n)}</span></div>",
        )
        + "</summary>"
        f'<form class="stack edit" method="post" action="{PREFIX}/log"'
        f' data-per100="{per100_attr(per_100g)}"{day_attrs(ctx, ctx.kcal)}>'
        f'<input type="hidden" name="food_id" value="{row["id"]}">'
        f'<input type="hidden" name="estimated" value="{int(estimated)}">'
        f'<input type="hidden" name="q" value="{e(q)}">'
        + (f'<p class="muted small">{e(row["name"])}</p>' if len(row["name"]) > LONG_NAME else "")
        + amount_picker(row, grams, slider_top(grams, row["serving_g"]))
        + grams_chips(row, portions)
        + preview(per_100g, grams, ctx, ctx.kcal)
        + where
        + '<button type="submit">Add to log</button></form></details>'
        + again
        + "</li>"
    )


def found_item(
    row,
    back: str,
    photo: str = "",
    *,
    searched: str = "",
    saved: bool = False,
    prep: str = "",
    same_as: int = 0,
) -> str:
    """A New food search result (#88): tapping it adds the food to Foods and opens its page.
    A USDA food counts as confirmed once picked; an Open Food Facts one stays unconfirmed.

    A result that is already in Foods (#97) says so and just opens it, so its photo,
    corrections and confirmation stay as they are. When the hit resolved to a food from
    another source than `searched` (the same barcode), it says where that food came from.
    `prep` tags how it was prepared (Raw, Cooked, Breaded); `same_as` counts other
    listings of the same product grouped into it."""
    n = logic.round_for_display(store.nutrients_of(row))
    meta = []
    if saved:
        meta.append('<span class="badge ok">In your foods</span>')
    if prep:
        meta.append(f'<span class="badge">{e(prep)}</span>')
    if row["brand"]:
        meta.append(e(row["brand"]))
    if searched and row["source"] != searched:
        meta.append(PROVENANCE.get(row["source"], ""))
    if row["verified"]:
        meta.append('<span class="badge ok">Confirmed</span>')
    elif row["source"] != "fdc" or saved:
        meta.append('<span class="badge unsure">Unconfirmed</span>')
    if same_as:
        meta.append(f"+{same_as} identical listing{'s' if same_as > 1 else ''}")
    inner = with_thumb(
        photo,
        row["name"],
        f'<div class="row"><span class="name clamp">{e(row["name"])}</span>'
        f'<span class="kcal">{num(n["kcal"])} <small>kcal</small></span></div>'
        f'<div class="row small muted"><span>{" · ".join(meta)}</span>'
        f"<span>{macros_line(n)}</span></div>",
    )
    if saved:
        return (
            f'<li><a class="found" href="{e(food_url(row["id"], back))}"'
            f' aria-label="Open {e(row["name"])}, already in your foods">{inner}</a></li>'
        )
    return (
        f'<li><form method="post" action="{PREFIX}/foods/found">'
        f'<input type="hidden" name="food_id" value="{row["id"]}">'
        f'<input type="hidden" name="back" value="{e(back)}">'
        f'<button type="submit" class="found" aria-label="Add {e(row["name"])}">'
        + inner
        + "</button></form></li>"
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
        f'<input type="hidden" name="date" value="{ctx.day.isoformat()}">'
        if meal_id is not None
        else meal_menu(ctx, None, "Add to", default_time, with_date=True)
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
        f'<p class="preview small muted" data-today="{ctx.day.isoformat()}">'
        f"{e(day_title(ctx.day, ctx.today))} after this: {num(after)}{goal} kcal</p>"
        f'<input type="hidden" name="template_id" value="{t["id"]}">'
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
        "background_color": THEME_DARK,
        "theme_color": THEME_DARK,
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


# --- Recipes (#82) --------------------------------------------------------------------------

RECIPE_NAME_MAX, STEPS_MAX, MAX_SERVINGS = 80, 5000, 100
RECIPE_PICKS = 12  # foods listed when searching for an ingredient


@dataclass
class RecipeDraft:
    """A recipe being written in the editor, as typed: it goes back and forth with the
    server while ingredients are searched for, added and removed, and is saved at the end."""

    recipe_id: int | None = None
    name: str = ""
    servings: str = ""
    steps: str = ""
    items: list[tuple[int, str]] = field(default_factory=list)  # (food_id, grams as typed)
    q: str = ""


def recipe_numbers(recipe: store.Recipe) -> dict:
    """logic.recipe_nutrition for a stored recipe."""
    return logic.recipe_nutrition(
        [(store.nutrients_of(i), i["grams"]) for i in recipe.ingredients], recipe.servings
    )


def recipe_url(recipe_id: int) -> str:
    return f"{PREFIX}/recipes/{recipe_id}"


def recipe_item(recipe: store.Recipe) -> str:
    """A recipe in the Recipes list: name, servings and calories and macros per serving.
    A tap opens it and its … opens its menu; with the script, swipe it left to delete it
    and press and hold it to edit it."""
    n = logic.round_for_display(recipe_numbers(recipe)["per_serving"])
    url = recipe_url(recipe.id)
    delete = (
        f'<form class="swipe-action js-only" method="post" action="{url}/delete">'
        f'<button type="submit" aria-label="Delete {e(recipe.name)}">Delete</button></form>'
    )
    return (
        f'<li id="recipe-{recipe.id}" class="swipe" data-hold="{url}/edit">{delete}'
        f'<div class="swipe-body lrow" data-swipe><a class="food-link" href="{url}"'
        ' draggable="false">'
        + with_thumb(
            "",
            recipe.name,
            f'<div class="row"><span class="name clamp">{e(recipe.name)}</span>'
            f'<span class="kcal">{num(n["kcal"])} <small>kcal</small></span></div>'
            f'<div class="row small muted"><span class="meta">Serves'
            f" {count_text(recipe.servings)}</span><span>{macros_line(n)}</span>"
            "</div>",
            "fat",
        )
        + f"</a>{more_link(f'{url}/menu', recipe.name)}</div></li>"
    )


# --- Saved meals (#60) ---------------------------------------------------------------------

SAVED = f"{PREFIX}/saved-meals"
TEMPLATE_NAME_MAX = 80


def elsewhere(q: str, *, typed: bool = False) -> str:
    """Foods' way on when a food isn't saved yet (#97): search USDA and Open Food Facts for
    the same words (only on a tap, never as you type), and with `typed` type it in with the
    name filled in."""
    links = [
        f'<a class="button ghost" href="{PREFIX}/foods/new?{e(urlencode({"q": q}))}">'
        f"Search databases for “{e(q)}”</a>"
    ]
    if typed:
        query = urlencode({"how": "type", "name": q})
        links.append(f'<a class="button ghost" href="{PREFIX}/foods/new?{e(query)}">Type it in</a>')
    return f'<div class="elsewhere">{"".join(links)}</div>'


FOODS_SIDES = (
    ("foods", f"{PREFIX}/foods", "Foods", f"{PREFIX}/foods/new", "New food"),
    ("meals", SAVED, "Meals", f"{SAVED}/new", "New saved meal"),
    ("recipes", f"{PREFIX}/recipes", "Recipes", f"{PREFIX}/recipes/new", "New recipe"),
)


def foods_switch(active: str) -> str:
    """The Foods tab's head (#60, #105): its title with a + for a new one of what's shown,
    and Foods / Meals / Recipes pills; the tab always opens on Foods."""
    links = "".join(
        f'<a href="{href}"{" aria-current=page" if key == active else ""}>{label}</a>'
        for key, href, label, _, _ in FOODS_SIDES
    )
    _, _, _, new, words = next(side for side in FOODS_SIDES if side[0] == active)
    plus = f'<a class="iconbtn" href="{new}" aria-label="{words}">{icon("plus")}</a>'
    return (
        page_head("Foods", plus)
        + f'<nav class="pills" aria-label="Foods, saved meals or recipes">{links}</nav>'
    )


def editor_item(hidden: str, name: str, kcal: str, remove: str, index: int, amount: str) -> str:
    """A food in the saved meal or recipe editor (#105): its name and calories, a remove
    button, and under them its amount (a Stepper)."""
    label = e(name)
    return (
        f'<li class="ingredient">{hidden}<div class="ing-head">'
        f'<span class="name clamp">{label}</span><span class="muted small kcal">{kcal}</span>'
        f'<button class="iconbtn remove" type="submit" name="remove" value="{index}"'
        f' formaction="{remove}" formnovalidate aria-label="Remove {label}">{icon("x")}</button>'
        f"</div>{amount}</li>"
    )


def editor_search(action: str, q: str, results, label: str) -> str:
    """Adding a food in the saved meal or recipe editor: search your foods, then + one."""
    found = ""
    if q:
        picks = "".join(
            f'<li><button class="pick" type="submit" name="add" value="{r["id"]}"'
            f' formaction="{action}" formnovalidate>'
            f'<span class="name clamp">{e(r["name"])}</span><span class="muted small">'
            + (f"{e(r['brand'])} · " if r["brand"] else "")
            + f"{num(logic.round_for_display(store.nutrients_of(r))['kcal'])}{NBSP}kcal"
            f' per 100 g</span><span class="plus" aria-hidden="true">{icon("plus")}</span>'
            "</button></li>"
            for r in results
        )
        found = (
            f'<ul class="picks">{picks}</ul>'
            if picks
            else f'<p class="muted small">None of your foods match “{e(q)}”. Add it'
            f' from <a href="{PREFIX}/foods/new">New food</a> first, or ask Claude.</p>'
        )
    return (
        '<div class="search addsearch"><span class="field-icon">'
        f'{icon("search")}</span><input type="search" name="q"'
        f' value="{e(q)}" placeholder="Search your foods to add one"'
        f' aria-label="{label}" data-draft-search enterkeyhint="search">'
        f'<button type="submit" name="do" value="search" formaction="{action}"'
        f" formnovalidate>Search</button></div>{found}"
    )


def save_bar(label: str) -> str:
    """An editor's Save, kept at the bottom of the screen over the tab bar (#105)."""
    return (
        f'<div class="logbar"><button class="button" type="submit">{icon("check")}'
        f"<span>{label}</span></button></div>"
    )


def list_search(
    action: str, q: str, placeholder: str, label: str, extra: str = "", **attrs: str
) -> str:
    """The search at the top of a Foods list (#105), kept in reach as the list scrolls."""
    more = "".join(
        f" {k.replace('_', '-')}" + (f'="{e(v)}"' if v else "") for k, v in attrs.items()
    )
    clear = f'<a class="clear" href="{action}" data-clear{"" if q else " hidden"}>Clear</a>'
    return (
        f'<form class="search listsearch" method="get" action="{action}" role="search"{more}>'
        f'{extra}<span class="field-icon">{icon("search")}</span>'
        f'<input type="search" name="q" value="{e(q)}" placeholder="{placeholder}"'
        f' aria-label="{label}" maxlength="100" autocomplete="off" enterkeyhint="search">'
        f'<button class="no-js" type="submit">Search</button>{clear}</form>'
    )


def template_totals(items) -> logic.Nutrients:
    """A saved meal's calories and nutrients, from its foods and grams."""
    return logic.sum_nutrients([logic.snapshot(store.nutrients_of(i), i["grams"]) for i in items])


def items_text(count: int) -> str:
    return "1 item" if count == 1 else f"{count} items"


def item_amount(item) -> str:
    """How much of a food a saved meal has: servings when that's a whole or half number of
    them, with the grams; else grams. An estimated amount gets the muted "~"."""
    grams = grams_text(item["grams"], bool(item["estimated"]))
    if item["serving_g"] and (count := whole_servings(item["grams"], item["serving_g"])):
        return f"{servings_text(count)} · {grams}"
    return grams


def saved_meal_item(t, items, query: str = "") -> str:
    """A saved meal on the Meals side of Foods: name, items, calories and macros. A tap
    opens it and its … opens its menu; with the script, swipe it left to delete it (after
    asking), right to log it again now (#105), and press and hold it to edit it. `query`
    (?date=) carries the day the Log sheet was opened on to its page."""
    n = logic.round_for_display(template_totals(items))
    url = f"{SAVED}/{t['id']}"
    name = capitalized(t["name"])
    delete = (
        f'<form class="swipe-action js-only" method="post" action="{url}/delete"'
        f' data-confirm="Delete the saved meal {e(name)}?">'
        f'<button type="submit" aria-label="Delete {e(name)}">Delete</button></form>'
    )
    again = (
        f'<form class="swipe-action confirm js-only" method="post" action="{PREFIX}/log-template">'
        f'<input type="hidden" name="template_id" value="{t["id"]}">'
        '<input type="hidden" name="to" value="new"><input type="hidden" name="now" value="1">'
        f'<button type="submit" aria-label="Log {e(name)} now">Log</button></form>'
    )
    return (
        f'<li id="saved-{t["id"]}" class="swipe" data-hold="{url}/edit">{again}{delete}'
        f'<div class="swipe-body lrow" data-swipe><a class="food-link" href="{url}{query}"'
        ' draggable="false">'
        + with_thumb(
            "",
            name,
            f'<div class="row"><span class="name clamp">{e(name)}</span>'
            f'<span class="kcal">{num(n["kcal"])} <small>kcal</small></span></div>'
            f'<div class="row small muted"><span class="meta">{items_text(len(items))}</span>'
            f"<span>{macros_line(n)}</span></div>",
            "protein",
        )
        + f"</a>{more_link(f'{url}/menu{query}', name)}</div></li>"
    )


@dataclass
class TemplateDraft:
    """A saved meal being written in the editor, as typed. Each item is (food_id, amount as
    typed, unit, est): unit is "servings" or "g", and est is the grams it was saved with when
    that amount was an estimate (#75), which it stays while the grams don't change."""

    template_id: int | None = None
    name: str = ""
    items: list[tuple[int, str, str, str]] = field(default_factory=list)
    q: str = ""


def draft_grams(food, amount: str, unit: str) -> float:
    """An editor item's grams: its servings times the food's serving size, or its grams."""
    raw = amount.replace(",", "").strip()
    if unit == "servings" and food["serving_g"]:
        return parse_servings(raw, food["serving_g"])
    return parse_grams(raw)


def add_routes(
    server: MCPServer,
    settings: Settings,
    provider: ChompAuthProvider | None,
    fdc: FDCClient | None = None,
    off: OFFClient | None = None,
    photos: Photos | None = None,
) -> None:
    """Register the dashboard. provider is None in local-only mode (no sign-in). With fdc
    configured, Add food can also search USDA FoodData Central. New food searches both by
    name and looks barcodes up through them."""
    tz = settings.tz
    sessions = Sessions(settings.db_path)
    secure = bool(settings.public_url)
    css = (STATIC_DIR / "app.css").read_text()
    js = (STATIC_DIR / "app.js").read_text()
    usda = fdc is not None and fdc.enabled
    fdc = fdc or FDCClient(settings.fdc_api_key)
    off = off or OFFClient(settings.off_user_agent)
    photos = photos or Photos(settings.photos_path, off)
    ocr_limit = anyio.CapacityLimiter(1)  # one label photo at a time; OCR is CPU-heavy
    photo_limit = anyio.CapacityLimiter(2)  # resizing a phone photo takes a moment too
    health_limit = anyio.CapacityLimiter(1)  # a Health export can be millions of samples

    def user_of(request: Request) -> int | None:
        """The signed-in user (#46), or None. Local-only mode is always the owner."""
        if provider is None:
            return users.OWNER_ID
        if not hasattr(request.state, "user_id"):
            request.state.user_id = sessions.user(request.cookies.get(COOKIE, ""))
        return request.state.user_id

    def photo_src(food_id: int) -> str:
        """The URL of a food's photo (#74), or "" when it has none. It changes with the
        photo, so browsers may keep it for good."""
        version = photos.version(food_id)
        return "" if version is None else f"{PREFIX}/foods/{food_id}/photo?v={version}"

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

    def sent_text(synced: str, last: date) -> str:
        """When the phone last sent data: "11:49 PM today", "Oct 12, 7:02 AM"."""
        local = logic.local_iso(synced, tz)
        day = datetime.fromisoformat(local).date()
        return (
            f"{clock(local)} today" if day == last else f"{short_date(day, last)}, {clock(local)}"
        )

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

    def start_session(url: str, user_id: int) -> RedirectResponse:
        """Sign someone in on this browser and go to url."""
        response = go(url)
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

    def is_owner(request: Request) -> bool:
        """The owner may add and remove people (#109). Local-only mode has no sign-in, so
        no one else could use the dashboard there."""
        return provider is not None and user_of(request) == users.OWNER_ID

    def hevy_syncs(conn, request: Request) -> bool:
        """Whether this person has a Hevy key to sync with (#113, #109)."""
        return bool(hevy_sync.key_for(conn, settings, user_of(request)))

    def day_url(day: date) -> str:
        return f"{PREFIX}?date={day.isoformat()}"

    def sheet_or_page(
        request: Request, title: str, sheet: str, back: str, active: str = "today"
    ) -> Response:
        """A sheet's content (#105): just the content with ?part=1, for app.js to show over
        the page it opened from; else a page of its own, with a way back."""
        if request.query_params.get("part"):
            return HTMLResponse(sheet, headers=SECURITY_HEADERS)
        return render(
            title,
            back_head(back, "the page before", "")
            + f'<section class="sheet-page">{sheet}</section>',
            active,
            signed_in=provider is not None,
            user=account(request),
        )

    def came_from(request: Request, fallback: str) -> str:
        """Where a sheet or its form goes back to: its ?back= (or the ?from= app.js adds,
        the page the sheet opened over), when that's a dashboard page."""
        params = request.query_params
        value = params.get("back") or params.get("from") or ""
        return safe_next(value) if value else fallback

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
            meal_sizes={m["id"]: len(entries) for m, entries in meals},
        )
        return meals, totals, targets, ctx

    def parse_day(value: str | None) -> date:
        """A date from a page or form: up to today, in a sane year range."""
        day = logic.parse_local_date(value, tz)
        if not MIN_YEAR <= day.year <= MAX_YEAR or day > today():
            raise ValueError(day)
        return day

    def optional_day(value: str | None) -> date | None:
        """A past day carried in a link (?date=), or None when it's missing, today or bad."""
        try:
            day = parse_day(value) if value else None
        except ValueError:
            return None
        return day if day != today() else None

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
        if said := WEIGHED.get(params.get("weighed", "")):
            return toast(f"<span>{said}</span>", "status")
        return ""

    # --- static files (no sign-in: the home-screen installer fetches these) ---

    @server.custom_route(f"{PREFIX}/static/app.css", methods=["GET"])
    async def stylesheet(request: Request) -> Response:
        return Response(css, media_type="text/css", headers={"Cache-Control": "max-age=3600"})

    @server.custom_route(f"{PREFIX}/static/app.js", methods=["GET"])
    async def script(request: Request) -> Response:
        return Response(js, media_type="text/javascript", headers={"Cache-Control": "max-age=3600"})

    @server.custom_route(PREFIX + "/static/{name}", methods=["GET"])
    async def static_file(request: Request) -> Response:
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
        return start_session(next_url, user_id)

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

    def your_day(conn, day: date, now: date) -> str:
        """Today's Your day tiles (#105): steps, exercise, weight and training on that day,
        each opening its screen on Progress."""
        lo, hi = bounds(day - timedelta(days=30), day)
        moved = logic.activity_by_day(store.activity_between(conn, lo, hi), tz)
        synced = store.last_activity_sync(conn)
        goals = store.get_body_goals(conn)
        _, end = bounds(day, day)
        weights = store.weights_between(conn, bounds(day - timedelta(days=60), day)[0], end)
        latest = store.latest_weight(conn, end)
        workouts = store.workouts_between(conn, *bounds(day, day))
        last_workout = workouts[0] if workouts else store.latest_workout(conn, end)
        sets = store.workout_sets(conn, [last_workout["id"]]) if last_workout else {}
        gym = gym_progress(conn, day)

        values = moved.get(day, {})
        so_far = " so far" if day == now else ""
        connect_health = '<span class="link">Connect Apple Health</span>'
        one_day = f"{ACTIVITY_PAGE}/day?date={day.isoformat()}"
        tiles_html = []

        # Steps: against the step goal, else the 30 days before
        if "steps" in values:
            steps = values["steps"]
            if goals["step_goal"]:
                pct = steps / goals["step_goal"] * 100
                sub = f"of {num(goals['step_goal'])}{so_far}" + meter(pct, "steps")
            else:
                before = mean(
                    moved.get(d, {}).get("steps")
                    for d in logic.date_range(day - timedelta(days=30), day - timedelta(days=1))
                )
                sub = f"30-day avg {num(before)}" if before is not None else so_far.strip()
            tiles_html.append(
                day_tile(
                    ACTIVITY_PAGE if day == now else one_day,
                    "footprints",
                    "steps",
                    "Steps",
                    num(steps),
                    sub,
                )
            )
        else:
            sub = (
                ("Nothing from your phone yet" if day == now else "No steps that day")
                if synced
                else connect_health
            )
            tiles_html.append(
                day_tile(
                    ACTIVITY_PAGE if synced else f"{INTEGRATIONS}#health",
                    "footprints",
                    "steps",
                    "Steps",
                    "&ndash;",
                    sub,
                )
            )

        # Exercise: minutes, with the calories burned under them
        if "exercise_min" in values:
            burned = logic.burned_kcal(values)
            sub = f"{num(burned)}{NBSP}kcal burned" if burned is not None else so_far.strip()
            tiles_html.append(
                day_tile(
                    f"{ACTIVITY_PAGE}?metric=exercise" if day == now else one_day,
                    "timer",
                    "exercise",
                    "Exercise",
                    f"{num(values['exercise_min'])}<small>{NBSP}min</small>",
                    sub,
                )
            )
        else:
            tiles_html.append(
                day_tile(
                    f"{ACTIVITY_PAGE}?metric=exercise" if synced else f"{INTEGRATIONS}#health",
                    "timer",
                    "exercise",
                    "Exercise",
                    "&ndash;",
                    "No exercise yet" if synced else connect_health,
                )
            )

        # Weight: the latest weigh-in up to that day, and how the trend moved that week
        if latest is not None:
            measured = logic.local_date(latest["measured_at"], tz)
            trend = logic.weight_trend(
                (logic.local_date(r["measured_at"], tz), r["kg"]) for r in weights
            )
            bits = [] if measured == day else [capitalized(short_date(measured, now))]
            change = logic.week_change(trend, day)
            if change is not None:
                lb = logic.weight_out(abs(change))["lb"]
                if lb >= 0.1:
                    glyph = "trending-down" if change < 0 else "trending-up"
                    sign = "&minus;" if change < 0 else "+"
                    bits.append(
                        f'<span class="trend">{icon(glyph)}{sign}{num(lb, 1)}{NBSP}lb'
                        "</span> this week"
                    )
                else:
                    bits.append("Steady this week")
            sub = " · ".join(bits)
            tiles_html.append(
                day_tile(
                    WEIGHT_PAGE,
                    "scale",
                    "weight",
                    "Weight",
                    f"{num(logic.weight_out(latest['kg'])['lb'], 1)}<small>{NBSP}lb</small>",
                    sub,
                )
            )
        else:
            tiles_html.append(
                day_tile(
                    f"{PREFIX}/weight/new?{urlencode({'back': day_url(day)})}",
                    "scale",
                    "weight",
                    "Weight",
                    "&ndash;",
                    '<span class="link">Add a weigh-in</span>',
                    sheet=True,
                )
            )

        # Training: that day's workout, else when the last one was
        if last_workout is not None:
            started = logic.local_date(last_workout["started_at"], tz)
            rows = sets.get(last_workout["id"], [])
            title = e(last_workout["title"] or "Workout")
            if started == day:
                bits = []
                if volume := logic.workout_volume_kg(rows):
                    bits.append(f"{num(logic.weight_out(volume)['lb'])}{NBSP}lb")
                if length := duration_text(last_workout):
                    bits.append(length)
                if len(workouts) > 1:
                    bits.append(f"+{len(workouts) - 1} more")
                sub = " · ".join(bits)
            else:
                sub = f"Last workout, {ago_text(started, day)}"
            if gym is not None:
                sub += f" · {gym.this_week} of {gym.goal} days this week"
            tiles_html.append(
                day_tile(
                    f"{WORKOUTS}/{last_workout['id']}" if started == day else TRAINING_PAGE,
                    "dumbbell",
                    "train",
                    "Training",
                    f'<span class="clamp1">{title}</span>',
                    sub,
                )
            )
        else:
            tiles_html.append(
                day_tile(
                    f"{INTEGRATIONS}#hevy",
                    "dumbbell",
                    "train",
                    "Training",
                    "&ndash;",
                    '<span class="link">Import from Hevy</span>',
                )
            )
        return (
            '<section class="yourday" aria-labelledby="yourday-title">'
            '<h2 class="section-title" id="yourday-title">Your day</h2>'
            f'<div class="tiles">{"".join(tiles_html)}</div></section>'
        )

    @server.custom_route(PREFIX, methods=["GET"])
    async def day_view(request: Request) -> Response:
        """Today (#105): the day's energy (eaten against the goal and burned), its macros,
        Your day tiles for steps, exercise, weight and training, and the meals. Tapping an
        entry or a meal's … opens its sheet; the + in the tab bar logs to this day."""
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
            meals, totals, targets, _ = load_day(conn, day)
            rows = [x.nutrients for _, entries in meals for x in entries]
            banner = notice(conn, params, page_url)
            moved = logic.activity_by_day(store.activity_between(conn, *bounds(day, day)), tz)
            synced = store.last_activity_sync(conn)
            tiles_section = your_day(conn, day, now) if day <= now else ""
        # Goals not set count as the defaults here (the sliders and History keep the stored ones)
        progress = logic.progress(totals, logic.with_default_targets(targets))
        shown = logic.round_for_display(totals)

        prev_day, next_day = day - timedelta(days=1), day + timedelta(days=1)
        forward = (
            f'<a class="step" href="{day_url(next_day)}" aria-label="Next day">&rsaquo;</a>'
            if day < now
            else '<span class="step off" aria-hidden="true">&rsaquo;</span>'
        )
        fresh = ""
        if synced:
            fresh = fresh_tag(
                f"{INTEGRATIONS}#health", "heart-pulse", "Apple Health", sent_text(synced, now)
            )
        head = [
            '<header class="daynav">',
            f'<a class="step" href="{day_url(prev_day)}" aria-label="Previous day">&lsaquo;</a>',
            # The title opens the month view (#71): a sheet with app.js, else its own page
            f'<div class="daytitle"><h1><a class="opencal" data-calendar'
            f' href="{calendar_url(month_start(day), day)}" aria-label="{e(day_title(day, now))},'
            f' open the calendar">{e(day_title(day, now))}'
            ' <span class="caret" aria-hidden="true">&#9662;</span></a></h1>'
            f'<p class="muted small">{e(long_date(day, now))}</p></div>',
            forward,
            "</header>",
            week_strip(day, now),
            f'<div class="dayfresh">{fresh}</div>' if fresh else "",
            banner,
        ]

        # The energy hero: what's left of the goal in a ring, then eaten, burned and net
        kcal = progress["kcal"]
        left = round(kcal["remaining"])
        # Over the goal: amber up to 100 kcal over, red past that
        tone = "" if left >= 0 else "over" if left < -100 else "caution"
        eaten = totals["kcal"] or 0.0
        burned = logic.burned_kcal(moved.get(day, {}))
        so_far = " so far" if day == now else ""
        if burned is not None:
            net = eaten - burned
            sign = "&minus;" if net < 0 else "+"
            burned_text = f"{num(burned)}"
            net_text = f"{sign}{num(abs(net))}"
        else:
            burned_text = net_text = "&ndash;"
        # At the goal (within the last tenth of it, not over): the ring shines once
        met = tone == "" and (kcal["percent"] or 0.0) >= 90
        title = day_title(day, now)
        more = (
            f'<a class="iconbtn" href="{PREFIX}/day-menu?date={day.isoformat()}" data-sheet'
            f' aria-label="More for {e(title)}: breakdown and goals">{icon("ellipsis")}</a>'
        )
        summary = [
            f'<section class="card energy" aria-label="Energy for {e(title)}">'
            f'<div class="card-head"><h2>Energy</h2>{more}</div>'
            f'<a class="energy-body" href="{breakdown_url(day)}">'
            f'<div class="ring-box{" met" if met else ""}">'
            f"{energy_ring(kcal['percent'] or 0.0, tone)}"
            f'<div class="ring-num"><b class="kcal {tone}">{num(abs(left))}</b>'
            f"<span>{'left' if left >= 0 else 'over'}</span></div></div>"
            '<ul class="energy-lines">'
            f'<li><span class="key kcal-ate">Eaten</span><b>{num(eaten)}</b></li>'
            f'<li><span class="key kcal-burn">Burned{so_far}</span><b>{burned_text}</b></li>'
            f'<li><span class="key">Net</span><b>{net_text}</b></li>'
            f'<li class="goal">Goal {num(kcal["target"])} kcal'
            f' <span class="more-link">Breakdown&nbsp;&rsaquo;</span></li></ul></a></section>'
        ]
        macros = []
        for key, cls, _ in MACROS:
            p = progress.get(key)
            eaten_g = num(shown[key], logic.DISPLAY_PLACES[key])
            if p:
                left_g = round(p["remaining"], logic.DISPLAY_PLACES[key])
                note = f"{amount(key, abs(left_g))} {'left' if left_g >= 0 else 'over'}"
                macros.append(
                    f'<a class="macro" href="{breakdown_url(day, f"top-{key}")}">'
                    f'<div class="small {cls}">{NUTRIENT_LABELS[key]}</div>'
                    f"<div><strong>{eaten_g}</strong>"
                    f' <span class="muted target">/ {amount(key, p["target"])}</span></div>'
                    f"{meter(p['percent'], key)}"
                    f'<div class="muted small">{note}</div></a>'
                )
            else:
                macros.append(
                    f'<a class="macro" href="{breakdown_url(day, f"top-{key}")}">'
                    f'<div class="small {cls}">{NUTRIENT_LABELS[key]}</div>'
                    f"<div><strong>{eaten_g}</strong> g</div></a>"
                )
        more_nutrients = ""
        extras = extras_line(shown)
        micros = micros_table(shown, logic.partial_micros(rows))
        if extras or micros:
            more_nutrients = (
                '<details class="extras morenut"><summary class="small">More nutrients</summary>'
                + (f'<p class="muted small">{extras}</p>' if extras else "")
                + micros.replace('<details class="extras">', '<details class="extras inner">', 1)
                + "</details>"
            )
        summary.append(
            f'<section class="card macros-card" aria-label="Macros for {e(title)}">'
            f'<div class="macros">{"".join(macros)}</div>{more_nutrients}'
        )
        if not targets["kcal"] and not any(targets[k] for k, _, _ in MACROS):
            summary.append(
                f'<p class="muted small">No daily goals yet. <a href="{GOALS_PAGE}">Set them'
                "</a>, or tell Claude something like “my protein target is 180 g”.</p>"
            )
        summary.append("</section>")
        summary.append(tiles_section)

        cards = []
        for meal, entries in meals:
            single = len(entries) == 1
            meal_totals = logic.round_for_display(logic.sum_nutrients(x.nutrients for x in entries))
            items = "".join(entry_row(x, single, photo_src(x.food_id)) for x in entries)
            when = clock(logic.local_iso(meal["eaten_at"], tz))
            label = meal["label"] or ""
            label_html = f' <span class="label">· {e(capitalized(label))}</span>' if label else ""
            note = f'<p class="muted small note">{e(meal["note"])}</p>' if meal["note"] else ""
            total = (
                ""
                if single
                else '<div class="row small muted total"><span>Meal total</span>'
                f"<span>{macros_line(meal_totals)}</span></div>"
            )
            options = (
                f'<a class="iconbtn" href="{PREFIX}/meals/{meal["id"]}" data-sheet'
                f' aria-label="Options for the {e(meal_title(meal))} meal">{icon("ellipsis")}</a>'
            )
            cards.append(
                f'<section class="card meal" id="meal-{meal["id"]}"><div class="mealhead">'
                f'<h2>{when}{label_html}</h2><span class="kcal">{num(meal_totals["kcal"])}'
                f"{NBSP}kcal</span>{options}</div>{note}"
                f'<ul class="entries">{items}</ul>{total}</section>'
            )
        if cards:
            cards.insert(0, '<h2 class="section-title">Meals</h2>')
        elif day <= now:
            log_here = f"{PREFIX}/add?date={day.isoformat()}"
            cards.append(
                '<section class="card empty"><p>Nothing logged on this day.</p>'
                f'<p class="muted small">Tap + to <a href="{log_here}" data-sheet>log food</a>,'
                " or tell Claude what you ate.</p></section>"
            )
        return render(
            title,
            "".join(head) + MAIN_COL + "".join(summary) + SIDE_COL + "".join(cards),
            "today",
            signed_in=provider is not None,
            user=account(request),
            log_date=day if day != now else None,
            refresh=True,
        )

    @server.custom_route(f"{PREFIX}/day-menu", methods=["GET"])
    async def day_menu(request: Request) -> Response:
        """The … on Today's energy (#105): the day's breakdown and the goals."""
        if not signed_in(request):
            return to_login(request)
        try:
            day = parse_day(request.query_params.get("date"))
        except ValueError:
            day = today()
        back = day_url(day)
        rows = (
            item(
                breakdown_url(day),
                "chart-pie",
                "kcal",
                "Breakdown",
                "Macro split, calories by meal, top foods",
            )
            + item(
                f"{GOALS_PAGE}?{urlencode({'back': back})}",
                "target",
                "protein",
                "Edit goals",
                "Calories and macros",
            )
            + item(
                calendar_url(month_start(day), day), "sun", "carbs", "Pick a day", "The month view"
            )
        )
        sheet = (
            f'<h2 id="daymenu-title">{e(day_title(day, today()))}</h2>'
            f'<div class="group">{rows}</div>'
        )
        return sheet_or_page(request, "Today", sheet, back)

    # --- Day breakdown (#63) ---

    @server.custom_route(f"{PREFIX}/calendar", methods=["GET"])
    async def calendar_view(request: Request) -> Response:
        """The month view. ?part=1 is just the calendar, for the sheet app.js opens on Today."""
        if not signed_in(request):
            return to_login(request)
        now = today()
        params = request.query_params
        try:
            shown = parse_day(params.get("date")) if params.get("date") else now
            month = (
                datetime.strptime(params["month"], "%Y-%m").date()
                if params.get("month")
                else month_start(shown)
            )
        except ValueError:
            return message("Unknown date", "Months look like 2026-10.", 400, signed_in=True)
        with connect(request) as conn:
            started = store.first_meal_at(conn)
            first = month_start(min(logic.local_date(started, tz), shown) if started else shown)
            month = min(max(month, first), month_start(now))
            end = add_months(month, 1) - timedelta(days=1)
            lo, _ = logic.local_day_bounds(month, tz)
            _, hi = logic.local_day_bounds(end, tz)
            by_day = logic.totals_by_day(store.entries_between(conn, lo, hi), tz)
            targets = store.get_targets(conn)
        body = month_calendar(month, shown, now, first, by_day, targets)
        if params.get("part"):
            return HTMLResponse(body, headers=SECURITY_HEADERS)
        back = back_head(day_url(shown), day_title(shown, now), "")
        return render(
            "Calendar",
            f'{back}<section class="card calpage">{body}</section>',
            "today",
            signed_in=provider is not None,
            user=account(request),
        )

    @server.custom_route(f"{PREFIX}/breakdown", methods=["GET"])
    async def breakdown(request: Request) -> Response:
        """A day's nutrients: the macro split, calories by meal, the foods that gave the most
        of each, and the full nutrition facts. A food with no value for a nutrient counts as
        none of it (decided on #63); a nutrient no food that day reports isn't shown."""
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

        def top(key: str, photos: bool = False) -> list[tuple]:
            return [
                (foods[f]["name"], bool(foods[f]["verified"]), v)
                + ((photo_src(f),) if photos else ())
                for f, v in logic.top_foods(items, key)
            ]

        title = day_title(day, now)
        parts = [back_head(back, title, "Breakdown", sub=e(long_date(day, now)))]
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
                log_date=day if day != now else None,
            )

        # Calories and the macro split
        split = logic.macro_split(totals)
        tiles = []
        for key, cls, _ in MACROS:
            share = split[key] if split else 0.0
            tiles.append(
                f'<a class="macro" href="#top-{key}"><div class="small {cls}">'
                f"{NUTRIENT_LABELS[key]}</div><div><strong>{round(share)}%</strong></div>"
                f'<div class="muted small">{amount(key, shown[key])}</div></a>'
            )
        parts.append(
            f'<section class="card summary" aria-label="Totals for {e(title)}">'
            f'<div class="split-hero">{split_ring(totals, num(shown["kcal"]))}'
            f'<div class="macros">{"".join(tiles)}</div></div>'
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
            best = top(key, photos=True)
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
            log_date=day if day != now else None,
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
        """Log food (#105): a full-height sheet from + (?part=1), else a page. A chip says
        where food goes (a new meal now, or a meal already that day); pills switch between
        Recent, Frequent, Saved meals and Recipes; a search covers your foods and saved
        meals, then USDA on a tap (or by itself when few of yours match, unless the search
        is being typed: ?live=1). Each food logs again at its last amount with its +, or
        opens for an amount with a tap."""
        if not signed_in(request):
            return to_login(request)
        serving_fill.backfill_soon(settings.db_path, fdc)  # once: servings for USDA foods
        params = request.query_params
        try:
            day = parse_day(params.get("date"))
        except ValueError:
            return message("Unknown date", "Pick a day up to today.", 400, signed_in=True)
        q = params.get("q", "").strip()[:100]
        meal_id = int_param(params.get("meal"))
        view = params.get("view", "")
        usda_rows, usda_note = [], ""
        templates: list = []
        portioned: list = []
        rows: list = []
        with connect(request) as conn:
            meals, _, _, ctx = load_day(conn, day)
            meal = next((m for m, _ in meals if m["id"] == meal_id), None)
            if meal is None:  # gone, or on another day: log a new meal instead
                meal_id = None
            if q:
                view = ""
                portioned = store.search_portions(conn, q, ADD_LIMIT)
                # The last word may be half typed ("chick"), after whole-word matches
                rows = store.search_foods(conn, q, ADD_LIMIT, typing=True)
                names = set(store.templates_matching(conn, q))
                templates = [t for t in store.list_templates(conn) if t[0]["name"] in names]
            else:
                recent = [
                    r for r in store.list_used_foods(conn, ADD_LIMIT, "recent") if r["last_used"]
                ]
                if view not in ADD_VIEWS:
                    view = "recent" if recent else "frequent"
                if view == "recent":
                    rows = recent
                elif view == "frequent":
                    rows = store.list_used_foods(conn, ADD_LIMIT)
                elif view == "meals":
                    templates = store.list_templates(conn)
                else:
                    ids = [r.food_id for r in store.list_recipes(conn) if r.food_id]
                    found = store.get_foods(conn, ids)
                    rows = [found[i] for i in ids if i in found]
            seen = {r["id"] for r in rows} | {a["id"] for a in portioned}
            portions = store.portions_of(conn, seen)
            # On its own when few of your foods match (#88), else behind the button; never
            # while the search is still being typed
            asked = bool(params.get("usda"))
            want_usda = (
                usda
                and bool(q)
                and (asked or (len(seen) < AUTO_USDA_BELOW and not params.get("live")))
            )
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
            last = store.last_amounts(conn, seen)
            back = add_url(day, meal_id, q)
            banner = notice(conn, params, back)

        now_local = datetime.now(ZoneInfo(tz))
        default_time = now_local.strftime("%H:%M") if day == now_local.date() else "12:00"
        keep = {"view": view} if view and not q else {}

        def to(mid: int | None, **extra: str | int) -> str:
            return add_url(day, mid, q, **keep, **extra)

        # Where it goes: a new meal at the time, or a meal already logged that day
        new_meal = (
            f"New meal at {clock(now_local.isoformat())}" if day == now_local.date() else "New meal"
        )
        choices = [(None, new_meal)] + [(mid, title) for mid, title in ctx.meals]
        menu = "".join(
            f'<a href="{e(to(mid))}" data-sheet-swap'
            f"{' aria-current=true' if mid == meal_id else ''}>{e(title)}</a>"
            for mid, title in choices
        )
        dest_title = meal_title(meal) if meal is not None else new_meal
        when = "" if day == ctx.today else f"{day_title(day, ctx.today)} · "
        # Or another day: the sheet reloads on it, with that day's meals to pick from
        other_day = (
            f'<form class="dest-day" method="get" action="{PREFIX}/add">'
            + (f'<input type="hidden" name="view" value="{e(view)}">' if view and not q else "")
            + (f'<input type="hidden" name="q" value="{e(q)}">' if q else "")
            + f'<label>Another day<input type="date" name="date" value="{day.isoformat()}"'
            f' max="{ctx.today.isoformat()}" required></label>'
            '<button type="submit">Go</button></form>'
        )
        dest = (
            f'<details class="dest"><summary class="chip">{icon("utensils")}'
            f'<span>{e(when)}{e(dest_title)}</span><span aria-hidden="true">&#9662;</span>'
            f'</summary><nav class="dest-menu" aria-label="Log to">{menu}{other_day}</nav>'
            "</details>"
        )
        hidden = (
            f'<input type="hidden" name="date" value="{day.isoformat()}">'
            + (
                f'<input type="hidden" name="meal" value="{meal_id}">'
                if meal_id is not None
                else ""
            )
            + (f'<input type="hidden" name="view" value="{e(view)}">' if view else "")
        )
        scan = (
            f'<a class="scan" href="{PREFIX}/foods/new?how=scan" aria-label="Scan a barcode">'
            f"{icon('scan-barcode')}</a>"
        )
        search = (
            f'<form class="search logsearch" method="get" action="{PREFIX}/add" data-live-results'
            f' role="search">{hidden}<span class="field-icon">{icon("search")}</span>'
            f'<input type="search" name="q" value="{e(q)}" placeholder="Search foods and meals"'
            ' aria-label="Search foods and saved meals" autocomplete="off" enterkeyhint="search">'
            f"{scan}</form>"
        )
        pills = ""
        if not q:
            pills = (
                '<nav class="pills" aria-label="Show">'
                + "".join(
                    f'<a href="{e(add_url(day, meal_id, "", view=key))}" data-sheet-swap'
                    f"{' aria-current=page' if key == view else ''}>{label}</a>"
                    for key, label in ADD_VIEWS.items()
                )
                + "</nav>"
            )

        def picks_of(found) -> list[str]:
            out = []
            for r in found:
                grams, est = last.get(r["id"], (None, False))
                out.append(
                    pick_item(
                        r,
                        ctx,
                        meal_id,
                        q,
                        default_time,
                        portions.get(r["id"], ()),
                        photo_src(r["id"]),
                        quick=grams,
                        quick_estimated=est,
                    )
                )
            return out

        parts: list[str] = []
        if templates:
            lis = "".join(
                template_pick(t, items, ctx, meal_id, default_time) for t, items in templates
            )
            parts.append(f'<section class="card"><ul class="entries">{lis}</ul></section>')
        elif view == "meals":
            parts.append(
                '<section class="card empty"><p>No saved meals yet.</p><p class="muted small">'
                "Save one from a meal's … menu on Today, or "
                f'<a href="{SAVED}/new">make one</a>.</p></section>'
            )
        picks = picks_of(portioned) + picks_of(
            [r for r in rows if r["id"] not in {a["id"] for a in portioned}]
        )
        if picks:
            heading = (
                '<h2 class="section-title">Your foods<small>per 100 g</small></h2>' if q else ""
            )
            parts.append(
                f'{heading}<section class="card"><ul class="entries">{"".join(picks)}</ul>'
                "</section>"
            )
        elif q and not templates:
            parts.append(
                f'<section class="card empty"><p>None of your foods match “{e(q)}”.</p></section>'
            )
        elif view in ("recent", "frequent"):
            parts.append(
                '<section class="card empty"><p>No saved foods yet.</p><p class="muted small">'
                "Search for a food to find it in USDA FoodData Central.</p></section>"
            )
        elif view == "recipes":
            parts.append(
                '<section class="card empty"><p>No recipes yet.</p><p class="muted small">'
                f'<a href="{PREFIX}/recipes/new">Make one</a> and it logs like a food.</p>'
                "</section>"
            )
        if usda_rows or usda_note:
            lis = "".join(
                pick_item(r, ctx, meal_id, q, default_time, photo=photo_src(r["id"]))
                for r in usda_rows
            )
            body = f'<ul class="entries">{lis}</ul>' if lis else ""
            parts.append(
                '<h2 class="section-title">USDA FoodData Central<small>per 100 g</small></h2>'
                '<section class="card">'
                + (f'<p class="muted small">{e(usda_note)}</p>' if usda_note else "")
                + f"{body}</section>"
            )
        elif want_usda:
            parts.append('<section class="card empty"><p>No USDA matches.</p></section>')
        elif q and usda:
            parts.append(
                f'<a class="button ghost wide" href="{e(to(meal_id, usda=1))}" data-sheet-swap>'
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
        sheet = (
            '<div class="logfood" data-sheet-full>'
            f'<div class="loghead"><h2 id="log-title">Log food</h2>{dest}</div>'
            + banner
            + search
            + pills
            + f'<div class="results" data-results>{"".join(parts)}</div></div>'
        )
        if params.get("part"):
            return HTMLResponse(sheet, headers=SECURITY_HEADERS)
        return render(
            "Log food",
            back_head(day_url(day), day_title(day, ctx.today), "") + sheet,
            "today",
            signed_in=provider is not None,
            user=account(request),
            log_date=day if day != today() else None,
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
                # Swiping a saved meal right on Foods (#105) logs it as a new meal now
                now_local = datetime.now(ZoneInfo(tz))
                day_text, time_text = (
                    (now_local.date().isoformat(), now_local.strftime("%H:%M"))
                    if form.get("now") == "1"
                    else (str(form.get("date", "")), str(form.get("time", "")))
                )
                try:
                    when = parse_when(day_text, time_text)
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

    @server.custom_route(PREFIX + "/entries/{entry_id:int}", methods=["GET"])
    async def entry_view(request: Request) -> Response:
        """An entry's edit sheet (#105): ?part=1 for app.js, else a page."""
        if not signed_in(request):
            return to_login(request)
        params = request.query_params
        with connect(request) as conn:
            try:
                entry = store.get_entry(conn, request.path_params["entry_id"])
                meal = store.get_meal(conn, entry.meal_id)
            except store.NotFound:
                return message("Not found", "That entry no longer exists.", 404, signed_in=True)
            day = meal_day(meal)
            _, _, _, ctx = load_day(conn, day)
            food = store.get_food(conn, entry.food_id)
            banner = notice(conn, params, day_url(day))
        meal_time = hhmm(logic.local_iso(meal["eaten_at"], tz))
        by = params.get("by", "") if params.get("error") else ""
        typed = params.get("amount", "")[:20] if params.get("error") else None
        sheet = banner + entry_editor(entry, food, ctx, meal_time, by=by, typed=typed)
        return sheet_or_page(request, entry.name, sheet, f"{day_url(day)}#entry-{entry.id}")

    @server.custom_route(PREFIX + "/meals/{meal_id:int}", methods=["GET"])
    async def meal_view(request: Request) -> Response:
        """A meal's … sheet (#105): add food to it, rename it or change its time, combine it
        with another meal, save it as a saved meal, or delete it (with Undo)."""
        if not signed_in(request):
            return to_login(request)
        with connect(request) as conn:
            try:
                meal = store.get_meal(conn, request.path_params["meal_id"])
            except store.NotFound:
                return message("Not found", "That meal no longer exists.", 404, signed_in=True)
            day = meal_day(meal)
            meals, _, _, ctx = load_day(conn, day)
        entries = next((es for m, es in meals if m["id"] == meal["id"]), [])
        meal_id = meal["id"]
        local = logic.local_iso(meal["eaten_at"], tz)
        kcal = logic.sum_nutrients(x.nutrients for x in entries)["kcal"] or 0
        count = items_text(len(entries))
        others = "".join(
            f'<option value="{mid}">{e(title)}</option>'
            for mid, title in ctx.meals
            if mid != meal_id
        )

        def option(glyph: str, tone: str, title: str, body: str, sub: str = "") -> str:
            note = f"<small>{e(sub)}</small>" if sub else ""
            return (
                f'<details class="opt"><summary class="item"><span class="badge-icon'
                f' tone-{tone}">{icon(glyph)}</span><span class="grow">{e(title)}{note}</span>'
                f"{icon('chevron-right', 'icon chev')}</summary>{body}</details>"
            )

        rows = [
            item(
                add_url(day, meal_id, ""),
                "plus",
                "kcal",
                "Add food to this meal",
                data_sheet="",
            ),
            option(
                "pencil",
                "protein",
                "Rename or change the time",
                f'<form class="stack" method="post" action="{PREFIX}/meals/{meal_id}">'
                f'<label>Name<input name="label" value="{e(meal["label"] or "")}"'
                f' maxlength="{LABEL_MAX}" placeholder="Optional, like Post-workout"></label>'
                '<div class="pair">'
                f'<label>Time<input type="time" name="time" value="{hhmm(local)}" required>'
                f'</label><label>Date<input type="date" name="date" value="{local[:10]}"'
                f' max="{ctx.today.isoformat()}" required></label></div>'
                + time_nudges()
                + f'<label>Note<input name="note" value="{e(meal["note"] or "")}"'
                f' maxlength="{NOTE_MAX}" placeholder="Optional"></label>'
                '<button type="submit">Save</button></form>',
            ),
        ]
        if others:
            rows.append(
                option(
                    "merge",
                    "carbs",
                    "Combine with another meal",
                    f'<form class="stack" method="post" action="{PREFIX}/meals/{meal_id}/merge">'
                    f'<label>Move everything in this meal into<select name="into">{others}'
                    '</select></label><button type="submit">Combine</button></form>',
                )
            )
        if entries:
            rows.append(
                option(
                    "bookmark-plus",
                    "fat",
                    "Save as a saved meal",
                    f'<form class="stack" method="post" action="{PREFIX}/meals/{meal_id}/save">'
                    f'<label>Name<input name="name" required maxlength="{TEMPLATE_NAME_MAX}"'
                    f' value="{e(capitalized(meal["label"] or ""))}"'
                    ' placeholder="Like Usual breakfast"></label>'
                    '<button type="submit">Save meal</button></form>',
                    "Log it again in one tap",
                )
            )
        rows.append(
            f'<form method="post" action="{PREFIX}/meals/{meal_id}/delete">'
            '<button class="item danger" type="submit"><span class="badge-icon tone-over">'
            f'{icon("trash-2")}</span><span class="grow">Delete this meal'
            f"<small>{count}</small></span></button></form>"
        )
        sheet = (
            f'<h2 id="meal-title">{e(meal_title(meal))}</h2>'
            f'<p class="sheet-sub muted small">{count} · {num(kcal)}{NBSP}kcal</p>'
            f'<div class="group">{"".join(rows)}</div>'
        )
        return sheet_or_page(request, meal_title(meal), sheet, f"{day_url(day)}#meal-{meal_id}")

    @server.custom_route(PREFIX + "/meals/{meal_id:int}/save", methods=["POST"])
    async def save_meal_as(request: Request) -> Response:
        """Save a logged meal's foods and amounts as a saved meal (#105)."""
        if (no := refused(request, "Save meals")) is not None:
            return no
        form = await request.form()
        name = " ".join(str(form.get("name", "")).split())[:TEMPLATE_NAME_MAX]
        meal_id = request.path_params["meal_id"]
        with connect(request) as conn:
            try:
                meal = store.get_meal(conn, meal_id)
                entries = store.meal_entries(conn, meal_id)
            except store.NotFound:
                return message("Not found", "That meal no longer exists.", 404, signed_in=True)
            day = meal_day(meal)
            if not name:
                return back_to_day(day, f"meal-{meal_id}", problem="meal_name")
            items: list[store.Item] = [(x.food_id, x.grams, x.estimated) for x in entries]
            try:
                template_id = store.edit_template(conn, None, name, items)
            except store.NotFound:
                return back_to_day(day, f"meal-{meal_id}", problem="gone")
            except ValueError:
                return back_to_day(day, f"meal-{meal_id}", problem="meal_taken")
        log.info("dashboard: saved meal %d as template %d", meal_id, template_id)
        return go(f"{SAVED}/{template_id}?saved=1")

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
                return go(
                    f"{PREFIX}/entries/{entry_id}?"
                    + urlencode({"error": 1, "by": by, "amount": raw[:20]})
                )
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
        body = back_head(back, "where you were", "Daily goals") + goals_form(values, back, error)
        return render(
            "Daily goals",
            body,
            "you",
            signed_in=provider is not None,
            user=account(request),
            status=status,
            hue="you",
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
        back: str = f"{PREFIX}/foods/new",
    ) -> Response:
        """New food's flows and Edit food (#59, #105): full screen, the tab bar hidden, with a
        top bar of Cancel and the title. Cancel goes back where the flow was opened from
        (the script), else to `back`."""
        header = (
            '<header class="flowbar">'
            f'<a class="cancel" href="{e(back)}" data-back>Cancel</a>'
            f"<h1>{e(title)}</h1><span></span></header>"
        )
        return render(
            title,
            header + body,
            None,
            signed_in=provider is not None,
            user=account(request),
            status=status,
            scripts=("zxing.min.js", "scan.js"),
        )

    # New food's four ways in (#105): (how, icon, tone, title, what it's for)
    NEW_FOOD_WAYS = (
        ("scan", "scan-barcode", "kcal", "Scan a barcode", "A packaged food, by its code"),
        ("label", "scan-text", "protein", "Read a nutrition label", "A photo of Nutrition Facts"),
        ("search", "globe", "steps", "Search databases", "USDA and Open Food Facts, by name"),
        ("type", "keyboard", "carbs", "Type it in", "Restaurant dishes, home cooking"),
    )

    def new_food_ways(skip: str = "", code: str = "") -> str:
        """The ways to add a food as large rows; `skip` leaves out the one already open."""
        return "".join(
            item(
                new_food_url(how=how, barcode=code if how in ("label", "type") else ""),
                glyph,
                tone,
                title,
                sub,
            )
            for how, glyph, tone, title, sub in NEW_FOOD_WAYS
            if how != skip
        )

    def other_ways(skip: str, code: str = "") -> str:
        return (
            '<section class="group ways"><h3 class="group-cap">Other ways to add it</h3>'
            f"{new_food_ways(skip, code)}</section>"
        )

    def new_food_url(**params: str) -> str:
        query = urlencode({k: v for k, v in params.items() if v})
        return f"{PREFIX}/foods/new" + (f"?{query}" if query else "")

    def barcode_card(code: str = "", error: str = "") -> str:
        return (
            '<section class="card" id="scan"><h2>Scan a barcode</h2>'
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

    def search_card(q: str = "", results: str = "") -> str:
        """New food's search (#88): USDA FoodData Central and Open Food Facts by name."""
        where = "USDA FoodData Central and Open Food Facts" if usda else "Open Food Facts"
        clear = f'<a class="clear" href="{PREFIX}/foods/new">Clear</a>' if q else ""
        return (
            '<section class="card"><h2>Search for a food</h2>'
            f'<form class="search" method="get" action="{PREFIX}/foods/new"'
            + (" data-live-usda" if usda else "")
            + f'><input type="search" name="q" value="{e(q)}" placeholder="e.g. chicken burrito"'
            ' aria-label="Search USDA and Open Food Facts" maxlength="100" autocomplete="off">'
            f'<button type="submit">Search</button>{clear}</form>'
            f'<p class="small muted">Looks in {where}. Tap a food to add it to your'
            " foods.</p></section>"
            f'<div data-db-results aria-live="polite">{results}</div>'
        )

    async def search_online(conn, q: str, *, with_off: bool = True) -> str:
        """Both sources at once, cached into foods so every result has an id. Without
        `with_off` (USDA as you type, #97) Open Food Facts waits for the Search button: it
        allows only about 10 searches a minute."""
        found: dict[str, list] = {"fdc": [], "off": []}
        failed: set[str] = set()

        async def run(name: str, search) -> None:
            try:
                found[name] = await search(q, FOUND_LIMIT)
            except SourceError as err:
                log.warning("dashboard: %s search failed: %s", name, err)
                failed.add(name)

        async with anyio.create_task_group() as tg:
            if usda:
                tg.start_soon(run, "fdc", fdc.search_ranked)
            if with_off:
                tg.start_soon(run, "off", off.search)
        back = new_food_url(q=q)
        rows: dict[str, list] = {"fdc": [], "off": []}
        for source in rows:
            for rec in found[source]:
                try:
                    row, _ = store.cache_record(conn, rec)
                except ValueError:  # one bad upstream record mustn't sink the search
                    continue
                rows[source].append((row, rec))
        saved = store.used_food_ids(conn, (row["id"] for v in rows.values() for row, _ in v))
        seen: set[int] = set()
        out = []
        for source, title, note in (
            ("fdc", "USDA FoodData Central", ""),
            (
                "off",
                "Open Food Facts",
                "Crowd-sourced, so these stay unconfirmed until you check one against its label.",
            ),
        ):
            if source == "fdc" and not usda:
                continue
            items = []
            for row, rec in rows[source]:
                if row["id"] in seen:  # the same food, found by both (same barcode)
                    continue
                seen.add(row["id"])
                items.append(
                    found_item(
                        row,
                        back,
                        photo_src(row["id"]),
                        searched=source,
                        saved=row["id"] in saved,
                        prep=rank.preparation(row["name"]),
                        same_as=rec.same_as,
                    )
                )
            if source == "off" and not with_off:
                note = "Tap Search to look in Open Food Facts too."
            elif source in failed:
                note = f"{title} isn't reachable right now. Try again in a minute."
            elif not items:
                note = f"No matches in {title}."
            out.append(
                f'<section class="card"><h2 class="section">{title}<small>per 100 g</small>'
                "</h2>"
                + (f'<p class="muted small">{e(note)}</p>' if note else "")
                + (f'<ul class="entries">{"".join(items)}</ul>' if items else "")
                + "</section>"
            )
        if not usda:
            out.append('<p class="muted small center">Set FDC_API_KEY to search USDA here too.</p>')
        return "".join(out)

    @server.custom_route(f"{PREFIX}/foods/new", methods=["GET", "POST"])
    async def new_food(request: Request) -> Response:
        if request.method == "POST":
            return await save_new_food(request)
        if not signed_in(request):
            return to_login(request)
        params = request.query_params
        typed = params.get("barcode", "").strip()[:40]
        how = params.get("how", "")
        if how == "type":
            # From Foods' "Type it in" (#97), the name searched for comes along
            name = " ".join(params.get("name", "").split())[:FOOD_NAME_MAX]
            form = FoodForm("manual", {}, barcode=typed, name=name)
            body = f'<section class="card">{food_form(form)}</section>'
            return new_food_page(request, body, title="Type it in")
        q = " ".join(params.get("q", "").split())[:100]
        if params.get("part") == "usda":  # the script's USDA search as you type (#97)
            if not (q and usda):
                return HTMLResponse("", headers=SECURITY_HEADERS)
            with connect(request) as conn:
                results = await search_online(conn, q, with_off=False)
            return HTMLResponse(results, headers=SECURITY_HEADERS)
        if q or how == "search":
            results = ""
            if q:
                with connect(request) as conn:
                    results = await search_online(conn, q)
            body = search_card(q, results) + other_ways("search")
            return new_food_page(request, body, title="Search databases")
        if how == "scan" and not typed:
            body = barcode_card() + other_ways("scan")
            return new_food_page(request, body, title="Scan a barcode")
        if how == "label":
            body = label_card(typed) + other_ways("label", typed)
            return new_food_page(request, body, title="Read a label")
        if not typed:
            # The chooser (#105); an old link to its #scan opens the scanner (the script)
            body = (
                '<p class="muted flow-lead">How do you want to add it?</p>'
                f'<section class="group ways chooser" data-chooser>{new_food_ways()}</section>'
                '<p class="muted small center">Or tell Claude about it, with a photo of the'
                " label or the restaurant's nutrition numbers.</p>"
            )
            return new_food_page(request, body, back=f"{PREFIX}/foods")
        try:
            code = gtins.normalize(typed)
        except gtins.InvalidBarcode:
            problem = "That isn't a barcode number. Check the digits and try again."
            body = barcode_card(typed, problem) + other_ways("scan")
            return new_food_page(request, body, 400, title="Scan a barcode")
        with connect(request) as conn:
            hit = await barcode_lookup(conn, code, fdc, off)
        photos.backfill_soon(settings.db_path)
        if hit.row is not None:
            photos.fill_soon(hit.row["id"], hit.row["gtin"], hit.image_url)
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
                + other_ways("label", code)
            )
            return new_food_page(request, body, title="Scan a barcode")
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

    @server.custom_route(f"{PREFIX}/foods/found", methods=["POST"])
    async def add_found_food(request: Request) -> Response:
        """A New food search result was tapped (#88): add it to Foods and open its page.
        Picking a USDA food confirms it; Open Food Facts data is crowd-sourced, so that
        stays unconfirmed until the person checks it."""
        if (no := refused(request, "Add foods")) is not None:
            return no
        form = await request.form()
        food_id = int_param(str(form.get("food_id", "")))
        back = safe_next(str(form.get("back", ""))) or f"{PREFIX}/foods/new"
        with connect(request) as conn:
            try:
                row = store.get_food(conn, food_id or 0, live=True)
            except store.NotFound:
                return message("Not found", "That food no longer exists.", 404, signed_in=True)
            row = store.mark_saved(conn, row["id"])
            if row["source"] == "fdc" and not row["verified"]:
                row = store.set_food_confirmed(conn, row["id"], True)
        photos.fill_soon(row["id"], row["gtin"])
        log.info("dashboard: food %d added from search", row["id"])
        return go(with_params(food_url(row["id"], back), found=row["name"][:FOOD_NAME_MAX]))

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
            return new_food_page(
                request, label_card(code, str(err)) + other_ways("label", code), 400
            )
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

    def recipe_instead(conn, food_id: int, page: str = "") -> Response | None:
        """A recipe's food row (#82) is shown and changed as its recipe."""
        recipe = store.recipe_of_food(conn, food_id)
        if recipe is None:
            return None
        if recipe["user_id"] == store.user_of(conn):
            return go(recipe_url(recipe["id"]) + page)
        return message(
            "A recipe",
            f"{recipe['name']} is someone else's recipe; it changes only with its recipe.",
            403,
            signed_in=True,
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
            if (to := recipe_instead(conn, food_id, "/edit")) is not None:
                return to
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
        if found := params.get("found", "")[:FOOD_NAME_MAX]:
            out += toast(f"<span>Added {e(found)} to your foods.</span>", "status")
        if pic := PHOTO_TOASTS.get(params.get("pic", "")):
            out += toast(f"<span>{pic}</span>", "status")
        return out

    @server.custom_route(PREFIX + "/foods/{food_id:int}", methods=["GET"])
    async def food_page(request: Request) -> Response:
        """One food (#72, #105): its photo edge to edge (or a tile to add one), name, brand
        and whether it's confirmed; per serving / per 100 g switching the calorie and macro
        tiles and the label-style nutrition facts, vitamins and minerals folded away; the
        person's portions; how often they've logged it and where it came from; Log it at
        the bottom and everything else in its … menu. #photo lands on the photo."""
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
            if (to := recipe_instead(conn, food_id)) is not None:
                return to
            uses, last_used = store.food_usage(conn, food_id)
            portions = store.portions_of(conn, [food_id])[food_id]
            banner = notice(conn, request.query_params, here)
        banner += food_toasts(request.query_params)
        per_100g = store.nutrients_of(row)
        serving_g = row["serving_g"]

        def basis(grams: float | None, head: str) -> str:
            n = logic.scale(per_100g, grams) if grams else per_100g
            micros = food_micros(per_100g, grams)
            return (
                macro_tiles(n, "per serving" if grams else "per 100 g")
                + f'<section class="card facts-card"><h2>Nutrition facts</h2>'
                f"{facts_table(per_100g, grams, head)}</section>"
                + (
                    fold("food-micros", "Vitamins and minerals", micros, opened=False)
                    if micros
                    else ""
                )
            )

        if serving_g:
            grams = f"{num(serving_g, 1)}{NBSP}g"
            desc = row["serving_desc"]
            head = f"Per serving · {e(desc)}" if desc and desc != grams else f"Per {grams} serving"
            if desc and grams.replace(NBSP, "") not in desc.replace(" ", ""):
                head += f" ({grams})"
            values = basis_switch(
                "basis",
                [
                    ("serving", "Per serving", basis(serving_g, head)),
                    ("100g", "Per 100 g", basis(None, "Per 100 g")),
                ],
            )
        else:
            values = (
                '<p class="muted small basis-note">No serving size on record, so these are per'
                " 100 g.</p>" + basis(None, "Per 100 g")
            )
        base = f"{PREFIX}/foods/{food_id}"
        keep = f'<input type="hidden" name="back" value="{e(here)}">'
        if row["verified"]:
            state = '<span class="badge ok">Confirmed</span>'
        else:
            state = (
                '<span class="badge unsure">Unconfirmed</span>'
                f'<form class="inline" method="post" action="{base}/confirm">{keep}'
                '<input type="hidden" name="confirmed" value="1">'
                '<button class="chip" type="submit">Confirm it</button></form>'
            )
        foot = []
        if uses:
            when = day_title(logic.local_date(last_used, tz), today())
            when = when.lower() if when in ("Today", "Yesterday") else when
            foot.append(f"Logged {uses} time{'s' if uses != 1 else ''}, last {e(when)}")
        else:
            foot.append("Not logged yet")
        foot.append(f"Source: {SOURCES[row['source']]}")
        menu = f"{base}/menu?{urlencode({'back': here, 'gone': back})}"
        parts = [
            detail_bar(back, "Foods", menu, row["name"]),
            banner,
            f'<article class="detail{"" if row["verified"] else " unconfirmed"}">'
            + photo_block(food_id, row["name"], photo_src(food_id), here)
            + f'<div class="detail-head"><h1 class="detail-name">{e(row["name"])}</h1>'
            + (f'<p class="detail-sub">{e(row["brand"])}</p>' if row["brand"] else "")
            + f'<div class="badges">{state}</div></div>'
            + values
            + portions_block(row, portions, here)
            + f'<p class="muted small center detail-foot">{" · ".join(foot)}</p>'
            + log_bar(f"{base}/log?{urlencode({'back': here})}")
            + "</article>",
        ]
        return render(
            row["name"],
            "".join(parts),
            "foods",
            signed_in=provider is not None,
            user=account(request),
        )

    @server.custom_route(PREFIX + "/foods/{food_id:int}/menu", methods=["GET"])
    async def food_menu(request: Request) -> Response:
        """A food's … (#105), from its row on Foods or its page: Log it, Edit, Rename, its
        photo, Confirm (or Mark unconfirmed) and Delete. ?back= is where a change returns
        to; ?gone= where Delete does (the list, from the food's page)."""
        if not signed_in(request):
            return to_login(request)
        food_id = request.path_params["food_id"]
        back = came_from(request, f"{PREFIX}/foods")
        gone = safe_next(request.query_params.get("gone", "")) or back
        with connect(request) as conn:
            try:
                row = store.get_food(conn, food_id, live=True)
            except store.NotFound:
                return message("Not found", "That food no longer exists.", 404, signed_in=True)
        base = f"{PREFIX}/foods/{food_id}"
        name = row["name"]
        keep = f'<input type="hidden" name="back" value="{e(back)}">'
        src = photo_src(food_id)

        def option(glyph: str, tone: str, title: str, body: str, sub: str = "") -> str:
            note = f"<small>{sub}</small>" if sub else ""
            return (
                f'<details class="opt"><summary class="item"><span class="badge-icon'
                f' tone-{tone}">{icon(glyph)}</span><span class="grow">{title}{note}</span>'
                f"{icon('chevron-right', 'icon chev')}</summary>{body}</details>"
            )

        def button(action: str, fields: str, glyph: str, tone: str, title: str, cls="") -> str:
            return (
                f'<form method="post" action="{base}{action}">{keep}{fields}'
                f'<button class="item{cls}" type="submit"><span class="badge-icon tone-{tone}">'
                f'{icon(glyph)}</span><span class="grow">{title}</span></button></form>'
            )

        photo = option(
            "camera",
            "steps",
            "Change photo" if src else "Add a photo",
            '<div class="body">'
            + photo_form(food_id, back, "Choose a photo", "button ghost")
            + "</div>",
            "Take one, or pick one from your photos",
        )
        rows = [
            item(
                f"{base}/log?{urlencode({'back': back})}",
                "plus",
                "kcal",
                "Log it",
                "Pick an amount and a meal",
                data_sheet="",
            ),
            item(
                f"{base}/edit?{urlencode({'back': back})}",
                "pencil",
                "protein",
                "Edit",
                "Serving size and nutrition values",
            ),
            option(
                "pencil",
                "carbs",
                "Rename",
                f'<form class="stack" method="post" action="{base}/rename">{keep}'
                f'<label>Name<input name="name" value="{e(name)}" required'
                f' maxlength="{FOOD_NAME_MAX}"></label>'
                '<button type="submit">Save name</button></form>',
            ),
            photo,
        ]
        if src:
            rows.append(button("/photo/delete", "", "image-minus", "muted", "Remove photo"))
        if row["verified"]:
            rows.append(
                button(
                    "/confirm",
                    '<input type="hidden" name="confirmed" value="0">',
                    "badge-check",
                    "carbs",
                    "Mark unconfirmed",
                )
            )
        else:
            rows.append(
                button(
                    "/confirm",
                    '<input type="hidden" name="confirmed" value="1">',
                    "badge-check",
                    "kcal",
                    "Confirm<small>You've checked its numbers</small>",
                )
            )
        rows.append(
            f'<details class="opt danger"><summary class="item danger"><span class="badge-icon'
            f' tone-over">{icon("trash-2")}</span><span class="grow">Delete</span>'
            f"{icon('chevron-right', 'icon chev')}</summary>"
            f'<form class="stack" method="post" action="{base}/delete">'
            f'<input type="hidden" name="back" value="{e(gone)}">'
            '<p class="small muted">Deletes it for everyone. Meals already logged keep their'
            " numbers; portions of it go, and it leaves any saved meal.</p>"
            f'<button class="danger" type="submit">Yes, delete {e(name)}</button></form>'
            "</details>"
        )
        state = "Confirmed" if row["verified"] else "Unconfirmed"
        sub = " · ".join(filter(None, [e(row["brand"] or ""), state]))
        sheet = (
            f'<h2 id="food-menu-title">{e(name)}</h2><p class="sheet-sub muted small">{sub}</p>'
            f'<div class="group">{"".join(rows)}</div>'
        )
        return sheet_or_page(request, name, sheet, back, "foods")

    @server.custom_route(PREFIX + "/foods/{food_id:int}/log", methods=["GET"])
    async def food_log(request: Request) -> Response:
        """Log it (#105), from a food's or recipe's page or its … menu: the amount (servings
        when it has a serving size), a live preview, and a new meal now or a meal already
        logged that day. ?date= starts it on a past day; its Date box picks another (up to
        today)."""
        if not signed_in(request):
            return to_login(request)
        food_id = request.path_params["food_id"]
        back = came_from(request, food_url(food_id, f"{PREFIX}/foods"))
        now = today()
        day = optional_day(request.query_params.get("date")) or now
        with connect(request) as conn:
            try:
                food = store.get_food(conn, food_id, live=True)
            except store.NotFound:
                return message("Not found", "That food no longer exists.", 404, signed_in=True)
            last, estimated = store.last_amounts(conn, [food_id]).get(food_id, (None, False))
            portions = store.portions_of(conn, [food_id])[food_id]
            _, _, _, ctx = load_day(conn, day)
        per_100g = store.nutrients_of(food)
        serving = food["serving_g"]
        grams = serving or last or 100.0
        estimated = bool(not serving and last and estimated)
        default_time = datetime.now(ZoneInfo(tz)).strftime("%H:%M") if day == now else "12:00"
        form = (
            f'<form class="stack edit" method="post" action="{PREFIX}/log"'
            f' data-per100="{per100_attr(per_100g)}"{day_attrs(ctx, ctx.kcal)}>'
            f'<input type="hidden" name="food_id" value="{food["id"]}">'
            f'<input type="hidden" name="estimated" value="{int(estimated)}">'
            + amount_picker(food, grams, slider_top(grams, serving))
            + grams_chips(food, portions, None if serving else last)
            + preview(per_100g, grams, ctx, ctx.kcal)
            + meal_menu(ctx, None, "Add to", default_time, with_name=True, with_date=True)
            + '<button type="submit">Log it</button></form>'
        )
        sheet = f'<h2 id="food-log-title">Log {e(food["name"])}</h2>{form}'
        return sheet_or_page(request, f"Log {food['name']}", sheet, back, "foods")

    @server.custom_route(f"{PREFIX}/foods/sort", methods=["GET"])
    async def foods_sort(request: Request) -> Response:
        """The Foods sort sheet (#105): what to sort by, which way, and whether values show
        per serving or per 100 g. Each pick reloads the list (and is remembered)."""
        if not signed_in(request):
            return to_login(request)
        params = request.query_params
        q = params.get("q", "").strip()[:100]
        sort, desc, per_serving = foods_order(params, request.cookies.get(FOODS_COOKIE, ""))
        by = "".join(
            choice(foods_list_url(q, key, True, per_serving), label, key == sort)
            for key, label in FOOD_SORTS.items()
        )
        words = SORT_ORDERS.get(sort) or ("Highest first", "Lowest first")
        order = choice(foods_list_url(q, sort, True, per_serving), words[0], desc) + choice(
            foods_list_url(q, sort, False, per_serving), words[1], not desc
        )
        per = choice(
            foods_list_url(q, sort, desc, True),
            "Per serving",
            per_serving,
            "Foods without a serving size stay per 100 g",
        ) + choice(foods_list_url(q, sort, desc, False), "Per 100 g", not per_serving)
        sheet = (
            '<h2 id="sort-title">Sort foods</h2>'
            f'<div class="group"><h3 class="group-cap">Sort by</h3>{by}</div>'
            f'<div class="group"><h3 class="group-cap">Order</h3>{order}</div>'
            f'<div class="group"><h3 class="group-cap">Show values</h3>{per}</div>'
        )
        return sheet_or_page(
            request, "Sort foods", sheet, foods_list_url(q, sort, desc, per_serving), "foods"
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

    # --- Photos (#74) ---

    @server.custom_route(PREFIX + "/foods/{food_id:int}/photo", methods=["GET", "POST"])
    async def food_photo(request: Request) -> Response:
        """GET: the food's photo. POST: a new one from the person (multipart `photo`),
        replacing any it had; label photos never come here."""
        food_id = request.path_params["food_id"]
        if request.method == "GET":
            if not signed_in(request):
                return Response("Not signed in", status_code=401)
            try:
                data = photos.path(food_id).read_bytes()
            except OSError:
                return Response("Not found", status_code=404)
            return Response(
                data,
                media_type="image/jpeg",
                headers={"Cache-Control": "private, max-age=31536000, immutable"},
            )
        if (no := refused(request, "Add photos")) is not None:
            return no
        default = food_url(food_id, f"{PREFIX}/foods")
        size = int_param(request.headers.get("content-length"))
        if size is not None and size > PHOTO_MAX:
            return go(with_params(default, problem="photo_big"))
        async with request.form(max_files=1, max_fields=3) as form:
            back = safe_next(str(form.get("back", "")) or default)
            picture = form.get("photo")
            data = await picture.read() if hasattr(picture, "read") else b""
        if not data:
            return go(with_params(back, problem="photo"))
        if len(data) > PHOTO_MAX:
            return go(with_params(back, problem="photo_big"))
        with connect(request) as conn:
            try:
                store.get_food(conn, food_id, live=True)
            except store.NotFound:
                return message("Not found", "That food no longer exists.", 404, signed_in=True)
        try:
            await anyio.to_thread.run_sync(photos.save, food_id, data, limiter=photo_limit)
        except PhotoError:
            return go(with_params(back, problem="photo"))
        log.info("dashboard: photo saved for food %d", food_id)
        return go(with_params(back, pic="saved"))

    @server.custom_route(PREFIX + "/foods/{food_id:int}/photo/delete", methods=["POST"])
    async def remove_photo(request: Request) -> Response:
        if (no := refused(request, "Remove photos")) is not None:
            return no
        form = await request.form()
        food_id = request.path_params["food_id"]
        back = safe_next(str(form.get("back", "")) or food_url(food_id, f"{PREFIX}/foods"))
        await anyio.to_thread.run_sync(photos.remove, food_id)
        log.info("dashboard: photo removed for food %d", food_id)
        return go(with_params(back, pic="removed"))

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

    # --- Recipes (#82): a book button on Today opens them ---

    def recipe_page(title: str, body: str, request: Request, status: int = 200) -> Response:
        return render(
            title,
            body,
            "foods",
            signed_in=provider is not None,
            user=account(request),
            status=status,
        )

    @server.custom_route(f"{PREFIX}/recipes", methods=["GET"])
    async def recipes_list(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        params = request.query_params
        with connect(request) as conn:
            recipes = store.list_recipes(conn)
        banner = ""
        if removed := params.get("removed", "")[:RECIPE_NAME_MAX]:
            banner = toast(f"<span>Deleted {e(removed)}.</span>", "status")
        parts = [foods_switch("recipes"), banner]
        if recipes:
            items = "".join(recipe_item(r) for r in recipes)
            parts.append(
                f'<h2 class="section-title">Your recipes<small>{len(recipes)}</small></h2>'
                f'<section class="group list"><ul class="entries recipes">{items}</ul></section>'
            )
        else:
            parts.append(
                '<section class="card empty"><p>No recipes yet.</p><p class="muted small">'
                "Add one here, or tell Claude “save this as my chili recipe”.</p>"
                f'<a class="button" href="{PREFIX}/recipes/new">New recipe</a></section>'
            )
        return recipe_page("Recipes", "".join(parts), request)

    def detail_menu(
        request: Request,
        name: str,
        sub: str,
        rows: list[str],
        back: str,
        title_id: str,
        active: str = "foods",
    ) -> Response:
        """A saved meal's, recipe's or workout's … sheet (#105)."""
        sheet = (
            f'<h2 id="{title_id}">{e(name)}</h2><p class="sheet-sub muted small">{sub}</p>'
            f'<div class="group">{"".join(rows)}</div>'
        )
        return sheet_or_page(request, name, sheet, back, active)

    def delete_row(action: str, what: str, note: str, ask: str = "") -> str:
        """Delete in a … sheet: it opens to say what goes, then a button that does it."""
        confirm = f' data-confirm="{e(ask)}"' if ask else ""
        return (
            f'<details class="opt danger"><summary class="item danger"><span class="badge-icon'
            f' tone-over">{icon("trash-2")}</span><span class="grow">Delete</span>'
            f"{icon('chevron-right', 'icon chev')}</summary>"
            f'<form class="stack" method="post" action="{action}"{confirm}>'
            f'<p class="small muted">{note}</p>'
            f'<button class="danger" type="submit">Yes, delete {e(what)}</button></form></details>'
        )

    @server.custom_route(PREFIX + "/recipes/{recipe_id:int}", methods=["GET"])
    async def recipe_view(request: Request) -> Response:
        """One recipe (#82, #105), on the same template as a food: per serving / whole
        recipe switching the calorie and macro tiles, its ingredients as a list (each opens
        its food), its steps, Log it (in servings) at the bottom, and Edit and Delete in
        its … menu."""
        if not signed_in(request):
            return to_login(request)
        recipe_id = request.path_params["recipe_id"]
        with connect(request) as conn:
            try:
                recipe = store.get_recipe_by_id(conn, recipe_id)
            except store.NotFound:
                return message("Not found", "That recipe no longer exists.", 404, signed_in=True)
        numbers = recipe_numbers(recipe)
        params = request.query_params
        banner = (
            toast(f"<span>Saved {e(recipe.name)}.</span>", "status") if params.get("saved") else ""
        )
        url = recipe_url(recipe.id)
        ingredients = "".join(
            list_row(
                food_url(i["id"], url),
                i["name"],
                " · ".join(filter(None, [e(i["brand"] or ""), f"{num(i['grams'], 1)}{NBSP}g"])),
                f"{num(logic.snapshot(store.nutrients_of(i), i['grams'])['kcal'])}{NBSP}"
                "<small>kcal</small>",
                photo_src(i["id"]),
            )
            for i in recipe.ingredients
        )
        steps = (
            '<ol class="steps">' + "".join(f"<li>{e(s)}</li>" for s in recipe.steps) + "</ol>"
            if recipe.steps
            else '<p class="muted small">No steps yet. Add them with Edit in the … menu.</p>'
        )
        whole = f"Whole recipe · {num(numbers['total_g'], 0)}{NBSP}g of ingredients"
        values = basis_switch(
            "basis",
            [
                ("serving", "Per serving", macro_tiles(numbers["per_serving"], "per serving")),
                ("whole", "Whole recipe", macro_tiles(numbers["total"], "whole recipe")),
            ],
        )
        log = f"{PREFIX}/foods/{recipe.food_id}/log?{urlencode({'back': url})}"
        parts = [
            detail_bar(f"{PREFIX}/recipes", "Recipes", f"{url}/menu", recipe.name),
            banner,
            '<article class="detail">'
            + initials_art(recipe.name, "fat", "chef-hat")
            + f'<div class="detail-head"><h1 class="detail-name">{e(recipe.name)}</h1>'
            f'<p class="detail-sub">Recipe · makes {servings_text(recipe.servings)}</p></div>'
            + values
            + f'<section class="group"><h3 class="group-cap">Ingredients<small>{e(whole)}'
            f"</small></h3>{ingredients}</section>"
            + f'<section class="card"><h2>Steps</h2>{steps}</section>'
            + (log_bar(log) if recipe.food_id else "")
            + "</article>",
        ]
        return recipe_page(recipe.name, "".join(parts), request)

    @server.custom_route(PREFIX + "/recipes/{recipe_id:int}/menu", methods=["GET"])
    async def recipe_menu(request: Request) -> Response:
        """A recipe's … (#105): Log it, Edit and Delete."""
        if not signed_in(request):
            return to_login(request)
        with connect(request) as conn:
            try:
                recipe = store.get_recipe_by_id(conn, request.path_params["recipe_id"])
            except store.NotFound:
                return message("Not found", "That recipe no longer exists.", 404, signed_in=True)
        url = recipe_url(recipe.id)
        back = came_from(request, url)
        rows = [
            item(
                f"{PREFIX}/foods/{recipe.food_id}/log?{urlencode({'back': back})}",
                "plus",
                "kcal",
                "Log it",
                "In servings",
                data_sheet="",
            ),
            item(f"{url}/edit", "pencil", "protein", "Edit", "Ingredients, servings and steps"),
            delete_row(
                f"{url}/delete",
                recipe.name,
                "Meals already logged keep their numbers.",
            ),
        ]
        sub = f"Recipe · makes {servings_text(recipe.servings)}"
        return detail_menu(request, recipe.name, sub, rows, back, "recipe-menu-title")

    def recipe_editor(conn, draft: RecipeDraft, error: str = "") -> str:
        """The recipe form: name, servings, ingredients with grams (search your foods to add
        one), steps, and what a serving comes to so far."""
        foods = store.get_foods(conn, (f for f, _ in draft.items)) if draft.items else {}
        results = []
        if draft.q:
            own = store.get_recipe_by_id(conn, draft.recipe_id).food_id if draft.recipe_id else 0
            results = [r for r in store.search_foods(conn, draft.q, RECIPE_PICKS) if r["id"] != own]
        rows, picked = [], []
        for index, (food_id, grams) in enumerate(draft.items):
            food = foods[food_id]
            kcal = ""
            try:
                g = parse_grams(grams.replace(",", ""))
                picked.append((store.nutrients_of(food), g))
                n = logic.scale(store.nutrients_of(food), g)
                kcal = f"{num(n['kcal'])}{NBSP}kcal"
            except ValueError:
                pass
            rows.append(
                editor_item(
                    f'<input type="hidden" name="food_id" value="{food_id}">',
                    food["name"],
                    kcal,
                    f"{PREFIX}/recipes/draft",
                    index,
                    stepper(
                        "grams", grams, what=f"grams of {food['name']}", step=10, low=1, unit="g"
                    ),
                )
            )
        so_far = ""
        try:
            servings = float(draft.servings)
            if picked and len(picked) == len(draft.items) and 0 < servings <= MAX_SERVINGS:
                n = logic.recipe_nutrition(picked, servings)["per_serving"]
                so_far = macro_tiles(n, "per serving", "Per serving")
        except ValueError:
            pass
        title = "Edit recipe" if draft.recipe_id else "New recipe"
        back = recipe_url(draft.recipe_id) if draft.recipe_id else f"{PREFIX}/recipes"
        keep = (
            f'<input type="hidden" name="recipe_id" value="{draft.recipe_id}">'
            if draft.recipe_id
            else ""
        )
        draft_url = f"{PREFIX}/recipes/draft"
        return (
            back_head(back, draft.name or "Recipes" if draft.recipe_id else "Recipes", title)
            + (toast(f"<span>{e(error)}</span>", "alert", " bad") if error else "")
            + f'<form class="recipe-form editor" method="post" action="{PREFIX}/recipes/save"'
            " data-draft>"
            # Enter in a box searches (or refreshes the totals) rather than saving
            f'<button class="sr" type="submit" name="do" value="search" tabindex="-1"'
            f' formaction="{draft_url}" formnovalidate aria-hidden="true"></button>'
            + keep
            + '<div class="editor-main"><section class="group"><h2>Recipe</h2><div class="body">'
            + form_field(
                "name",
                "Name",
                draft.name,
                inputmode="text",
                attrs=f'required maxlength="{RECIPE_NAME_MAX}" placeholder="Like Chili"',
            )
            + stepper(
                "servings",
                draft.servings,
                what="servings it makes",
                label="Servings it makes",
                low=1,
            )
            + '</div></section><section class="group" id="ingredients"><h2>Ingredients'
            '<small>grams in the whole recipe</small></h2><div class="body">'
            + (f'<ul class="ingredients">{"".join(rows)}</ul>' if rows else "")
            + editor_search(draft_url, draft.q, results, "Search your foods for an ingredient")
            + "</div></section></div>"
            + '<div class="editor-side">'
            + (
                f'<section class="group"><h2>So far</h2><div class="body">{so_far}</div></section>'
                if so_far
                else ""
            )
            + '<section class="group"><h2>Steps<small>one per line</small></h2>'
            '<div class="body"><label class="fld"><span class="sr">Steps, one per line</span>'
            f'<textarea name="steps" rows="8" maxlength="{STEPS_MAX}"'
            ' placeholder="Brown the beef&#10;Add the beans and simmer 30 minutes">'
            f"{e(draft.steps)}</textarea></label></div></section></div>"
            + save_bar("Save recipe")
            + "</form>"
        )

    def read_draft(form) -> RecipeDraft:
        ids = [int_param(str(v)) for v in form.getlist("food_id")]
        grams = [str(v).strip()[:20] for v in form.getlist("grams")]
        items = [(i, g) for i, g in zip(ids, grams, strict=False) if i is not None]
        return RecipeDraft(
            recipe_id=int_param(str(form.get("recipe_id", ""))),
            name=" ".join(str(form.get("name", "")).split())[:RECIPE_NAME_MAX],
            servings=str(form.get("servings", "")).strip()[:20],
            steps=str(form.get("steps", ""))[:STEPS_MAX],
            items=items,
            q=str(form.get("q", "")).strip()[:100],
        )

    @server.custom_route(f"{PREFIX}/recipes/new", methods=["GET"])
    async def recipe_new(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        with connect(request) as conn:
            body = recipe_editor(conn, RecipeDraft())
        return recipe_page("New recipe", body, request)

    @server.custom_route(PREFIX + "/recipes/{recipe_id:int}/edit", methods=["GET"])
    async def recipe_edit(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        with connect(request) as conn:
            try:
                recipe = store.get_recipe_by_id(conn, request.path_params["recipe_id"])
            except store.NotFound:
                return message("Not found", "That recipe no longer exists.", 404, signed_in=True)
            draft = RecipeDraft(
                recipe_id=recipe.id,
                name=recipe.name,
                servings=count_text(recipe.servings),
                steps="\n".join(recipe.steps),
                items=[(i["id"], num(i["grams"], 1).replace(",", "")) for i in recipe.ingredients],
            )
            body = recipe_editor(conn, draft)
        return recipe_page("Edit recipe", body, request)

    @server.custom_route(f"{PREFIX}/recipes/draft", methods=["POST"])
    async def recipe_draft(request: Request) -> Response:
        """The editor again after searching for, adding or removing an ingredient. The
        script swaps the new form in place; without it this is a fresh page."""
        if (no := refused(request, "Edit recipes")) is not None:
            return no
        form = await request.form()
        draft = read_draft(form)
        with connect(request) as conn:
            index = int_param(str(form.get("remove", "")))
            if index is not None and 0 <= index < len(draft.items):
                del draft.items[index]
            if (food_id := int_param(str(form.get("add", "")))) is not None:
                try:
                    food = store.get_food(conn, food_id, live=True)
                except store.NotFound:
                    food = None
                if food is not None:
                    grams = num(food["serving_g"] or 100, 1).replace(",", "")
                    draft.items.append((food["id"], grams))
                    draft.q = ""
            try:
                body = recipe_editor(conn, draft)
            except store.NotFound:  # an ingredient was deleted meanwhile
                draft.items = [(f, g) for f, g in draft.items if _food_exists(conn, f)]
                body = recipe_editor(conn, draft)
        return recipe_page("Edit recipe" if draft.recipe_id else "New recipe", body, request)

    def _food_exists(conn, food_id: int) -> bool:
        try:
            store.get_food(conn, food_id)
        except store.NotFound:
            return False
        return True

    @server.custom_route(f"{PREFIX}/recipes/save", methods=["POST"])
    async def recipe_save(request: Request) -> Response:
        if (no := refused(request, "Save recipes")) is not None:
            return no
        form = await request.form()
        draft = read_draft(form)
        title = "Edit recipe" if draft.recipe_id else "New recipe"
        with connect(request) as conn:
            problem = ""
            ingredients = []
            try:
                servings = float(draft.servings.replace(",", ""))
                if not math.isfinite(servings) or not 0 < servings <= MAX_SERVINGS:
                    raise ValueError
            except ValueError:
                problem = f"Servings must be a number more than 0 and at most {MAX_SERVINGS}."
            for food_id, grams in draft.items:
                try:
                    ingredients.append((food_id, parse_grams(grams.replace(",", ""))))
                except ValueError:
                    problem = problem or PROBLEMS["grams"]
            if not draft.name:
                problem = "A recipe needs a name."
            elif not draft.items:
                problem = "Add at least one ingredient: search your foods above."
            if not problem:
                try:
                    recipe, _ = store.save_recipe(
                        conn,
                        draft.name,
                        servings,
                        ingredients,
                        draft.steps,
                        recipe_id=draft.recipe_id,
                    )
                except store.NotFound:
                    problem = PROBLEMS["gone"]
                except ValueError as err:
                    problem = capitalized(str(err)) + "."
            if problem:
                try:
                    body = recipe_editor(conn, draft, problem)
                except store.NotFound:
                    draft.items = [(f, g) for f, g in draft.items if _food_exists(conn, f)]
                    body = recipe_editor(conn, draft, problem)
                return recipe_page(title, body, request, status=400)
        log.info("dashboard: saved recipe %d", recipe.id)
        return go(f"{recipe_url(recipe.id)}?saved=1")

    @server.custom_route(PREFIX + "/recipes/{recipe_id:int}/delete", methods=["POST"])
    async def recipe_delete(request: Request) -> Response:
        if (no := refused(request, "Delete recipes")) is not None:
            return no
        with connect(request) as conn:
            try:
                recipe = store.delete_recipe(conn, request.path_params["recipe_id"])
            except store.NotFound:
                return go(f"{PREFIX}/recipes")
        log.info("dashboard: deleted recipe %d", recipe.id)
        return go(f"{PREFIX}/recipes?{urlencode({'removed': recipe.name})}")

    # --- Saved meals (#60) ---

    def foods_page(title: str, body: str, request: Request, status: int = 200) -> Response:
        return render(
            title,
            body,
            "foods",
            signed_in=provider is not None,
            user=account(request),
            status=status,
        )

    @server.custom_route(SAVED, methods=["GET"])
    async def saved_meals(request: Request) -> Response:
        """The Meals side of Foods: saved meals, searched by name, and + New saved meal."""
        if not signed_in(request):
            return to_login(request)
        params = request.query_params
        q = params.get("q", "").strip()[:100]
        day = optional_day(params.get("date"))
        query = f"?{urlencode({'date': day.isoformat()})}" if day else ""
        with connect(request) as conn:
            templates = store.list_templates(conn)
            if q:
                names = set(store.templates_matching(conn, q))
                templates = [t for t in templates if t[0]["name"] in names]
        banner = ""
        if removed := params.get("removed", "")[:TEMPLATE_NAME_MAX]:
            banner = toast(f"<span>Deleted {e(capitalized(removed))}.</span>", "status")
        parts = [
            foods_switch("meals"),
            banner,
            list_search(
                SAVED,
                q,
                "Search your saved meals",
                "Search your saved meals",
                (f'<input type="hidden" name="date" value="{day.isoformat()}">' if day else ""),
            ),
        ]
        if templates:
            items = "".join(saved_meal_item(t, t_items, query) for t, t_items in templates)
            heading = "Matches" if q else "Your saved meals"
            parts.append(
                f'<h2 class="section-title">{heading}<small>{len(templates)}</small></h2>'
                f'<section class="group list"><ul class="entries">{items}</ul></section>'
            )
        elif q:
            parts.append(
                f'<section class="card empty"><p>No saved meal matches “{e(q)}”.</p></section>'
            )
        else:
            parts.append(
                '<section class="card empty"><p>No saved meals yet.</p><p class="muted small">'
                "Save one from a meal's … on Today, or tell Claude “save this as my usual"
                f' breakfast”.</p><a class="button" href="{SAVED}/new">New saved meal</a>'
                "</section>"
            )
        return foods_page("Saved meals", "".join(parts), request)

    @server.custom_route(SAVED + "/{template_id:int}", methods=["GET"])
    async def saved_meal_view(request: Request) -> Response:
        """One saved meal (#60, #105), on the same template as a food: its total calories
        and macros as tiles, its foods as a list (each opens its food), Log it at the
        bottom, and Edit and Delete in its … menu. ?date= (from the Log sheet) is the day
        Log it starts on."""
        if not signed_in(request):
            return to_login(request)
        template_id = request.path_params["template_id"]
        day = optional_day(request.query_params.get("date"))
        query = f"?{urlencode({'date': day.isoformat()})}" if day else ""
        with connect(request) as conn:
            try:
                template, items = store.template_with_foods(conn, template_id)
            except store.NotFound:
                return message(
                    "Not found", "That saved meal no longer exists.", 404, signed_in=True
                )
        name = capitalized(template["name"])
        banner = (
            toast(f"<span>Saved {e(name)}.</span>", "status")
            if request.query_params.get("saved")
            else ""
        )
        url = f"{SAVED}/{template['id']}"
        foods_list = "".join(
            list_row(
                food_url(i["id"], url),
                i["name"],
                " · ".join(filter(None, [e(i["brand"] or ""), item_amount(i)])),
                f"{num(logic.snapshot(store.nutrients_of(i), i['grams'])['kcal'])}{NBSP}"
                "<small>kcal</small>",
                photo_src(i["id"]),
            )
            for i in items
        )
        parts = [
            detail_bar(SAVED, "Meals", f"{url}/menu{query}", name),
            banner,
            '<article class="detail">'
            + initials_art(name, "protein", "utensils")
            + f'<div class="detail-head"><h1 class="detail-name">{e(name)}</h1>'
            f'<p class="detail-sub">Saved meal · {items_text(len(items))}</p></div>'
            + macro_tiles(template_totals(items), "the whole meal")
            + f'<section class="group"><h3 class="group-cap">Foods</h3>{foods_list}</section>'
            + log_bar(f"{url}/log{query}", "Log this meal")
            + "</article>",
        ]
        return foods_page(name, "".join(parts), request)

    @server.custom_route(SAVED + "/{template_id:int}/menu", methods=["GET"])
    async def saved_meal_menu(request: Request) -> Response:
        """A saved meal's … (#105): Log it, Edit and Delete."""
        if not signed_in(request):
            return to_login(request)
        day = optional_day(request.query_params.get("date"))
        query = f"?{urlencode({'date': day.isoformat()})}" if day else ""
        with connect(request) as conn:
            try:
                template, items = store.template_with_foods(
                    conn, request.path_params["template_id"]
                )
            except store.NotFound:
                return message(
                    "Not found", "That saved meal no longer exists.", 404, signed_in=True
                )
        name = capitalized(template["name"])
        url = f"{SAVED}/{template['id']}"
        rows = [
            item(
                f"{url}/log{query}",
                "plus",
                "kcal",
                "Log it",
                "As a new meal or into one",
                data_sheet="",
            ),
            item(f"{url}/edit", "pencil", "protein", "Edit", "Name, foods and amounts"),
            delete_row(
                f"{url}/delete",
                name,
                "Meals already logged from it stay as they are.",
            ),
        ]
        kcal = num(template_totals(items)["kcal"] or 0)
        sub = f"Saved meal · {items_text(len(items))} · {kcal}{NBSP}kcal"
        return detail_menu(
            request, name, sub, rows, came_from(request, url), "saved-meal-menu-title"
        )

    @server.custom_route(SAVED + "/{template_id:int}/log", methods=["GET"])
    async def saved_meal_log(request: Request) -> Response:
        """Log a saved meal (#105): a new meal at a time and date, or into a meal already
        logged that day."""
        if not signed_in(request):
            return to_login(request)
        now = today()
        day = optional_day(request.query_params.get("date")) or now
        with connect(request) as conn:
            try:
                template, items = store.template_with_foods(
                    conn, request.path_params["template_id"]
                )
            except store.NotFound:
                return message(
                    "Not found", "That saved meal no longer exists.", 404, signed_in=True
                )
            _, _, _, ctx = load_day(conn, day)
        name = capitalized(template["name"])
        shown = logic.round_for_display(template_totals(items))
        after = ctx.kcal + shown["kcal"]
        goal = f" of {num(ctx.goal)}" if ctx.goal else ""
        default_time = datetime.now(ZoneInfo(tz)).strftime("%H:%M") if day == now else "12:00"
        form = (
            f'<form class="stack edit" method="post" action="{PREFIX}/log-template">'
            f'<input type="hidden" name="template_id" value="{template["id"]}">'
            + meal_menu(ctx, None, "Add to", default_time, with_date=True)
            # Only true for today, so the script hides it while another date is picked
            + (
                f'<p class="preview small muted" data-today="{day.isoformat()}">'
                f"Today after this: {num(after)}{goal} kcal</p>"
                if day == now
                else ""
            )
            + '<button type="submit">Log this meal</button></form>'
        )
        sheet = (
            f'<h2 id="saved-log-title">Log {e(name)}</h2>'
            f'<p class="sheet-sub muted small">{items_text(len(items))} ·'
            f" {num(shown['kcal'])}{NBSP}kcal · {macros_line(shown)}</p>{form}"
        )
        url = f"{SAVED}/{template['id']}"
        return sheet_or_page(request, f"Log {name}", sheet, came_from(request, url), "foods")

    def template_editor(conn, draft: TemplateDraft, error: str = "") -> str:
        """The saved meal form: a name, then its foods (search your foods to add one), each
        in servings or grams, and what the meal comes to so far."""
        foods = store.get_foods(conn, (i[0] for i in draft.items)) if draft.items else {}
        results = store.search_foods(conn, draft.q, RECIPE_PICKS) if draft.q else []
        rows, picked = [], []
        for index, (food_id, amount_typed, unit, est) in enumerate(draft.items):
            food = foods[food_id]
            kcal = ""
            try:
                g = draft_grams(food, amount_typed, unit)
                picked.append(logic.snapshot(store.nutrients_of(food), g))
                kcal = f"{num(picked[-1]['kcal'])}{NBSP}kcal"
            except ValueError:
                pass
            unit_field = ""
            if food["serving_g"]:
                options = "".join(
                    f'<option value="{v}"{" selected" if unit == v else ""}>{text}</option>'
                    for v, text in (
                        ("servings", f"&times; {num(food['serving_g'], 1)}{NBSP}g"),
                        ("g", "g"),
                    )
                )
                unit_field = (
                    f'<select name="unit" data-serving="{float(food["serving_g"])!r}"'
                    f' aria-label="Servings or grams of {e(food["name"])}">'
                    f"{options}</select>"
                )
            hidden = (
                f'<input type="hidden" name="food_id" value="{food_id}">'
                f'<input type="hidden" name="est" value="{e(est)}">'
                + ("" if unit_field else '<input type="hidden" name="unit" value="g">')
            )
            rows.append(
                editor_item(
                    hidden,
                    food["name"],
                    kcal,
                    f"{SAVED}/draft",
                    index,
                    stepper(
                        "amount",
                        amount_typed,
                        what=f"amount of {food['name']}",
                        step=0.5 if unit_field else 10,
                        low=0.5 if unit_field else 1,
                        unit=unit_field or "g",
                        grams_step=10 if unit_field else None,
                    ),
                )
            )
        so_far = ""
        if picked and len(picked) == len(draft.items):
            so_far = macro_tiles(logic.sum_nutrients(picked), "in all", "In all")
        title = "Edit saved meal" if draft.template_id else "New saved meal"
        back = f"{SAVED}/{draft.template_id}" if draft.template_id else SAVED
        keep = (
            f'<input type="hidden" name="template_id" value="{draft.template_id}">'
            if draft.template_id
            else ""
        )
        return (
            back_head(back, "the saved meal" if draft.template_id else "Meals", title)
            + (toast(f"<span>{e(error)}</span>", "alert", " bad") if error else "")
            + f'<form class="recipe-form editor" method="post" action="{SAVED}/save" data-draft>'
            # Enter in a box searches (or refreshes the totals) rather than saving
            f'<button class="sr" type="submit" name="do" value="search" tabindex="-1"'
            f' formaction="{SAVED}/draft" formnovalidate aria-hidden="true"></button>'
            + keep
            + '<div class="editor-main"><section class="group"><h2>Saved meal</h2>'
            '<div class="body">'
            + form_field(
                "name",
                "Name",
                draft.name,
                inputmode="text",
                attrs=f'required maxlength="{TEMPLATE_NAME_MAX}"'
                ' placeholder="Like Usual breakfast"',
            )
            + '</div></section><section class="group" id="ingredients"><h2>Foods'
            '<small>in servings or grams</small></h2><div class="body">'
            + (f'<ul class="ingredients meal-items">{"".join(rows)}</ul>' if rows else "")
            + editor_search(f"{SAVED}/draft", draft.q, results, "Search your foods to add one")
            + "</div></section></div>"
            + (
                '<div class="editor-side"><section class="group"><h2>So far</h2>'
                f'<div class="body">{so_far}</div></section></div>'
                if so_far
                else ""
            )
            + save_bar("Save meal")
            + "</form>"
        )

    def read_template_draft(form) -> TemplateDraft:
        ids = [int_param(str(v)) for v in form.getlist("food_id")]
        amounts = [str(v).strip()[:20] for v in form.getlist("amount")]
        units = [str(v) for v in form.getlist("unit")]
        ests = [str(v).strip()[:20] for v in form.getlist("est")]
        items = [
            (i, a, u if u in ("servings", "g") else "g", est)
            for i, a, u, est in zip(ids, amounts, units, ests, strict=False)
            if i is not None
        ]
        return TemplateDraft(
            template_id=int_param(str(form.get("template_id", ""))),
            name=" ".join(str(form.get("name", "")).split())[:TEMPLATE_NAME_MAX],
            items=items,
            q=str(form.get("q", "")).strip()[:100],
        )

    def known_items(conn, draft: TemplateDraft) -> None:
        """Drop items whose food was deleted while the editor was open."""
        live = []
        for item in draft.items:
            try:
                store.get_food(conn, item[0], live=True)
            except store.NotFound:
                continue
            live.append(item)
        draft.items = live

    @server.custom_route(SAVED + "/new", methods=["GET"])
    async def saved_meal_new(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        with connect(request) as conn:
            body = template_editor(conn, TemplateDraft())
        return foods_page("New saved meal", body, request)

    @server.custom_route(SAVED + "/{template_id:int}/edit", methods=["GET"])
    async def saved_meal_edit(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        with connect(request) as conn:
            try:
                template, items = store.template_with_foods(
                    conn, request.path_params["template_id"]
                )
            except store.NotFound:
                return message(
                    "Not found", "That saved meal no longer exists.", 404, signed_in=True
                )
            draft = TemplateDraft(template_id=template["id"], name=capitalized(template["name"]))
            for i in items:
                grams = num(i["grams"], 1).replace(",", "")
                est = grams if i["estimated"] else ""
                count = whole_servings(i["grams"], i["serving_g"]) if i["serving_g"] else None
                if count:
                    draft.items.append((i["id"], count_text(count), "servings", est))
                else:
                    draft.items.append((i["id"], grams, "g", est))
            body = template_editor(conn, draft)
        return foods_page("Edit saved meal", body, request)

    @server.custom_route(SAVED + "/draft", methods=["POST"])
    async def saved_meal_draft(request: Request) -> Response:
        """The editor again after searching for, adding or removing a food. The script
        swaps the new form in place; without it this is a fresh page."""
        if (no := refused(request, "Edit saved meals")) is not None:
            return no
        form = await request.form()
        draft = read_template_draft(form)
        with connect(request) as conn:
            index = int_param(str(form.get("remove", "")))
            if index is not None and 0 <= index < len(draft.items):
                del draft.items[index]
            if (food_id := int_param(str(form.get("add", "")))) is not None:
                try:
                    food = store.get_food(conn, food_id, live=True)
                except store.NotFound:
                    food = None
                if food is not None:
                    if food["serving_g"]:
                        draft.items.append((food["id"], "1", "servings", ""))
                    else:
                        draft.items.append((food["id"], "100", "g", ""))
                    draft.q = ""
            try:
                body = template_editor(conn, draft)
            except store.NotFound:
                known_items(conn, draft)
                body = template_editor(conn, draft)
        title = "Edit saved meal" if draft.template_id else "New saved meal"
        return foods_page(title, body, request)

    @server.custom_route(SAVED + "/save", methods=["POST"])
    async def saved_meal_save(request: Request) -> Response:
        if (no := refused(request, "Save meals")) is not None:
            return no
        form = await request.form()
        draft = read_template_draft(form)
        title = "Edit saved meal" if draft.template_id else "New saved meal"
        with connect(request) as conn:
            problem = ""
            items: list[store.Item] = []
            try:
                foods = store.get_foods(conn, (i[0] for i in draft.items), live=True)
            except store.NotFound:
                known_items(conn, draft)
                foods = store.get_foods(conn, (i[0] for i in draft.items))
                problem = PROBLEMS["gone"]
            for food_id, amount_typed, unit, est in draft.items:
                try:
                    grams = draft_grams(foods[food_id], amount_typed, unit)
                except ValueError as err:
                    problem = problem or str(err)
                    continue
                # An estimated amount stays one only while its grams are unchanged
                try:
                    kept = bool(est) and abs(float(est) - grams) < 0.05
                except ValueError:
                    kept = False
                items.append((food_id, grams, kept))
            if not draft.name:
                problem = "A saved meal needs a name."
            elif not draft.items:
                problem = "Add at least one food: search your foods above."
            template_id = draft.template_id
            if not problem:
                try:
                    template_id = store.edit_template(conn, draft.template_id, draft.name, items)
                except store.NotFound:
                    problem = PROBLEMS["gone"]
                except ValueError as err:
                    problem = capitalized(str(err)) + "."
            if problem:
                try:
                    body = template_editor(conn, draft, problem)
                except store.NotFound:
                    known_items(conn, draft)
                    body = template_editor(conn, draft, problem)
                return foods_page(title, body, request, status=400)
        log.info("dashboard: saved meal template %d", template_id)
        return go(f"{SAVED}/{template_id}?saved=1")

    @server.custom_route(SAVED + "/{template_id:int}/delete", methods=["POST"])
    async def saved_meal_delete(request: Request) -> Response:
        if (no := refused(request, "Delete saved meals")) is not None:
            return no
        with connect(request) as conn:
            try:
                template = store.delete_template(conn, request.path_params["template_id"])
            except store.NotFound:
                return go(SAVED)
        log.info("dashboard: deleted meal template %d", template["id"])
        return go(f"{SAVED}?{urlencode({'removed': template['name']})}")

    # --- Workouts (#94) ---

    @server.custom_route(ACTIVITY + "/import", methods=["POST"])
    async def activity_import(request: Request) -> Response:
        """Apple Health's export.zip from the Activity page: past weigh-ins, and whole days
        of activity the phone hasn't sent."""
        if (no := refused(request, "Import Apple Health data")) is not None:
            return no
        size = int_param(request.headers.get("content-length"))
        if size is not None and size > HEALTH_MAX:
            return go(f"{INTEGRATIONS}?problem=health_big#health")
        async with request.form(max_files=1, max_fields=2) as form:
            upload = form.get("export")
            if not hasattr(upload, "file"):
                return go(f"{INTEGRATIONS}?problem=health#health")
            if (upload.size or 0) > HEALTH_MAX:
                return go(f"{INTEGRATIONS}?problem=health_big#health")
            try:
                parsed = await anyio.to_thread.run_sync(
                    health_export.read, upload.file, tz, limiter=health_limit
                )
            except health_export.ExportError:
                return go(f"{INTEGRATIONS}?problem=health#health")
        with connect(request) as conn:
            weights = store.add_weights(conn, parsed.weights.items(), "health")
            activity = store.import_activity_days(conn, health_export.day_rows(parsed, tz))
        log.info(
            "dashboard: Health export import, %d weigh-ins added, %d days added, %d updated,"
            " %d kept, %d samples skipped",
            weights.added,
            activity.added,
            activity.updated,
            activity.kept,
            parsed.rejected,
        )
        done = (
            f"{weights.added}-{activity.added}-{activity.updated}-{activity.kept}-{parsed.rejected}"
        )
        return go(f"{INTEGRATIONS}?health={done}#health")

    @server.custom_route(WORKOUTS + "/import", methods=["POST"])
    async def workouts_import(request: Request) -> Response:
        if (no := refused(request, "Import workouts")) is not None:
            return no
        size = int_param(request.headers.get("content-length"))
        if size is not None and size > HEVY_MAX:
            return go(f"{INTEGRATIONS}?problem=hevy_big#hevy")
        async with request.form(max_files=1, max_fields=2) as form:
            upload = form.get("csv")
            data = await upload.read() if hasattr(upload, "read") else b""
        if len(data) > HEVY_MAX:
            return go(f"{INTEGRATIONS}?problem=hevy_big#hevy")
        try:
            parsed = hevy.parse(data, tz)
        except hevy.HevyError:
            return go(f"{INTEGRATIONS}?problem=hevy#hevy")
        with connect(request) as conn:
            counts = store.import_workouts(conn, parsed.workouts, "hevy")
        log.info(
            "dashboard: Hevy import, %d added, %d updated, %d unchanged, %d left out (%d bad rows)",
            counts.added,
            counts.updated,
            counts.unchanged,
            parsed.incomplete,
            parsed.rejected,
        )
        done = f"{counts.added}-{counts.updated}-{counts.unchanged}-{parsed.incomplete}"
        return go(f"{INTEGRATIONS}?hevy={done}#hevy")

    @server.custom_route(WORKOUTS + "/sync", methods=["POST"])
    async def workouts_sync(request: Request) -> Response:
        """Sync now (#113): one Hevy sync run, then back to Integrations with the result."""
        if (no := refused(request, "Sync with Hevy")) is not None:
            return no
        with connect(request) as conn:
            if not hevy_syncs(conn, request):
                return go(f"{INTEGRATIONS}#hevy")
        result = await anyio.to_thread.run_sync(hevy_sync.run, settings, user_of(request))
        if result.error:
            return go(f"{INTEGRATIONS}?problem=hevy_sync#hevy")
        done = f"{result.added}-{result.updated}-{result.deleted}"
        return go(f"{INTEGRATIONS}?hevysync={done}#hevy")

    @server.custom_route(WORKOUTS + "/{workout_id:int}", methods=["GET"])
    async def workout_view(request: Request) -> Response:
        """One workout (#105): title, when, how long and volume under it, notes, then each
        exercise's sets with PR badges and how it went last time; … holds Delete."""
        if not signed_in(request):
            return to_login(request)
        with connect(request) as conn:
            try:
                row = store.get_workout(conn, request.path_params["workout_id"])
            except store.NotFound:
                return message("Not found", "That workout isn't here anymore.", 404, signed_in=True)
            sets = store.workout_sets(conn, [row["id"]])[row["id"]]
            before = store.workouts_between(conn, "", row["started_at"])  # newest first
            before_sets = store.workout_sets(conn, [w["id"] for w in before])
        earlier = [(logic.local_date(w["started_at"], tz), before_sets[w["id"]]) for w in before]
        records = logic.set_records((x for _, ss in earlier for x in ss), sets)
        local = logic.local_iso(row["started_at"], tz)
        when = datetime.fromisoformat(local)
        by_exercise: dict[str, list] = {}
        for i, x in enumerate(sets):
            by_exercise.setdefault(x["exercise"], []).append((i, x))
        description = (
            f'<p class="muted small">{e(row["description"])}</p>' if row["description"] else ""
        )
        menu = (
            f'<a class="iconbtn" href="{WORKOUTS}/{row["id"]}/menu" data-sheet'
            f' aria-label="More for {e(row["title"])}">{icon("ellipsis")}</a>'
        )
        sub = (
            f"{e(when.strftime('%A, %B'))} {when.day}, {clock(local)}"
            f" · {workout_summary(row, sets)}"
            + (f" · {len(records)} PR{'' if len(records) == 1 else 's'}" if records else "")
        )
        parts = [
            back_head(TRAINING_PAGE, "Training", row["title"], menu, sub),
            description,
            '<div class="exercises">',
            *(
                exercise_card(name, group, records, logic.last_time(earlier, name))
                for name, group in by_exercise.items()
            ),
            "</div>",
        ]
        return progress_page(request, row["title"], "".join(parts), "train")

    @server.custom_route(WORKOUTS + "/{workout_id:int}/menu", methods=["GET"])
    async def workout_menu(request: Request) -> Response:
        """A workout's … (#105): its day on Today, and Delete."""
        if not signed_in(request):
            return to_login(request)
        with connect(request) as conn:
            try:
                row = store.get_workout(conn, request.path_params["workout_id"])
            except store.NotFound:
                return message("Not found", "That workout isn't here anymore.", 404, signed_in=True)
        url = f"{WORKOUTS}/{row['id']}"
        day = logic.local_date(row["started_at"], tz)
        rows = [
            item(day_url(day), "sun", "kcal", "Food that day", long_date(day, today())),
            delete_row(
                f"{url}/delete",
                row["title"],
                "Importing a Hevy export that still has it brings it back.",
            ),
        ]
        return detail_menu(
            request, row["title"], "Workout", rows, url, "workout-menu-title", "progress"
        )

    @server.custom_route(WORKOUTS + "/{workout_id:int}/delete", methods=["POST"])
    async def workout_delete(request: Request) -> Response:
        if (no := refused(request, "Delete workouts")) is not None:
            return no
        with connect(request) as conn:
            try:
                row = store.delete_workout(conn, request.path_params["workout_id"])
            except store.NotFound:
                return go(TRAINING_PAGE)
        log.info("dashboard: deleted workout %d", row["id"])
        return go(f"{TRAINING_PAGE}?removed=1")

    # --- Weight (#94) ---

    @server.custom_route(WEIGHT, methods=["GET", "POST"])
    async def weight_moved(request: Request) -> Response:
        """Weigh-ins are still added here; the page moved to Progress (#105)."""
        if request.method == "POST":
            return await weight_view(request)
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(WEIGHT_PAGE + query, status_code=301, headers=SECURITY_HEADERS)

    WEIGHED = {
        "added": "Weigh-in saved.",
        "edited": "Weigh-in changed.",
        "removed": "Weigh-in deleted.",
    }

    def weigh_in_form(
        action: str, back: str, lb: str, local: datetime, *, problem: str = "", note: str = ""
    ) -> str:
        """The weigh-in sheet's form (#105): one big number in lb, then when."""
        today_iso = today().isoformat()
        error = f'<p class="error" role="alert">{e(problem)}</p>' if problem else ""
        return (
            f'<form class="stack weigh-in" method="post" action="{action}">{error}'
            f'<input type="hidden" name="back" value="{e(back)}">'
            '<label class="bignum"><span class="sr">Weight in pounds</span>'
            f'<input name="lb" inputmode="decimal" autocomplete="off" required value="{e(lb)}"'
            ' aria-describedby="lb-unit" data-select><span id="lb-unit">lb</span></label>'
            + (f'<p class="muted small center">{note}</p>' if note else "")
            + '<div class="pair"><label>Time<input type="time" name="time"'
            f' value="{local.strftime("%H:%M")}" required></label>'
            f'<label>Date<input type="date" name="date" value="{local.date().isoformat()}"'
            f' max="{today_iso}" required></label></div>'
            '<button type="submit">Save</button></form>'
        )

    def weighed(back: str, what: str, **extra: str | int) -> RedirectResponse:
        """Back where the weigh-in sheet opened, saying what happened."""
        return go(with_params(back, weighed=what, **extra))

    def read_weigh_in(form) -> tuple[str, float]:
        """A weigh-in form's time (UTC) and kg. Raises ValueError with a PROBLEMS key."""
        when = parse_when(str(form.get("date", "")), str(form.get("time", "")))
        try:
            lb = float(str(form.get("lb", "")).replace(",", "").strip())
        except ValueError:
            raise ValueError("weight") from None
        if not math.isfinite(lb) or not 0 < lb < 1500:
            raise ValueError("weight")
        return when, logic.to_kg(lb, "lb")

    @server.custom_route(WEIGHT + "/new", methods=["GET"])
    async def weight_new(request: Request) -> Response:
        """The weigh-in sheet (#105), from the + sheet, Weight's + or Today's weight tile:
        the last weight to start from, now as the time."""
        if not signed_in(request):
            return to_login(request)
        back = came_from(request, WEIGHT_PAGE)
        with connect(request) as conn:
            latest = store.latest_weight(conn)
        lb = f"{logic.weight_out(latest['kg'])['lb']:.1f}" if latest else ""
        note = f"Last: {lb_text(latest['kg'])}" if latest else ""
        problem = PROBLEMS.get(request.query_params.get("problem", ""), "")
        sheet = '<h2 id="weigh-title">Add a weigh-in</h2>' + weigh_in_form(
            WEIGHT, back, lb, datetime.now(ZoneInfo(tz)), problem=problem, note=note
        )
        return sheet_or_page(request, "Add a weigh-in", sheet, back, "progress")

    async def weight_add(request: Request) -> Response:
        if (no := refused(request, "Add weigh-ins")) is not None:
            return no
        form = await request.form()
        back = safe_next(str(form.get("back", ""))) if form.get("back") else WEIGHT_PAGE
        if form.get("days") and not form.get("back"):  # an older page's form
            back = f"{WEIGHT_PAGE}?{urlencode({'days': str(form.get('days'))})}"
        try:
            when, kg = read_weigh_in(form)
        except ValueError as err:
            query = urlencode({"back": back, "problem": str(err)})
            return go(f"{WEIGHT}/new?{query}")
        with connect(request) as conn:
            row = store.add_weight(conn, when, kg, "app")
        log.info("dashboard: added weigh-in %d", row["id"])
        return weighed(back, "added")

    @server.custom_route(WEIGHT + "/{weight_id:int}", methods=["GET", "POST"])
    async def weight_edit(request: Request) -> Response:
        """A weigh-in's sheet (#105): change its weight or time, or delete it. A reading from
        Apple Health stays as changed here when the phone sends it again."""
        weight_id = request.path_params["weight_id"]
        if request.method == "POST":
            if (no := refused(request, "Change weigh-ins")) is not None:
                return no
            form = await request.form()
            back = safe_next(str(form.get("back", ""))) if form.get("back") else WEIGHT_PAGE
            try:
                when, kg = read_weigh_in(form)
            except ValueError as err:
                query = urlencode({"back": back, "problem": str(err)})
                return go(f"{WEIGHT}/{weight_id}?{query}")
            with connect(request) as conn:
                try:
                    was = store.get_weight(conn, weight_id)["measured_at"]
                    # the form shows local minutes: the same wall minute keeps the reading's
                    # own time (seconds, and which side of a DST fall-back hour it was on)
                    typed = f"{form.get('date', '')}T{form.get('time', '')}"
                    at = was if logic.local_iso(was, tz)[:16] == typed[:16] else when
                    row = store.update_weight(conn, weight_id, at, kg)
                except store.NotFound:
                    return go(with_params(back, problem="gone"))
            log.info("dashboard: changed weigh-in %d", row["id"])
            return weighed(back, "edited")

        if not signed_in(request):
            return to_login(request)
        back = came_from(request, WEIGHT_PAGE)
        with connect(request) as conn:
            try:
                row = store.get_weight(conn, weight_id)
            except store.NotFound:
                return go(back)
        local = datetime.fromisoformat(logic.local_iso(row["measured_at"], tz))
        label = f"{local.strftime('%a %b')} {local.day}, {clock(local.isoformat())}"
        source = WEIGHT_SOURCES.get(row["source"], "")
        problem = PROBLEMS.get(request.query_params.get("problem", ""), "")
        sheet = (
            '<h2 id="weigh-title">Weigh-in</h2>'
            f'<p class="sheet-sub muted small">{e(label)} · {e(source)}</p>'
            + weigh_in_form(
                f"{WEIGHT}/{weight_id}",
                back,
                f"{logic.weight_out(row['kg'])['lb']:.1f}",
                local,
                problem=problem,
            )
            + f'<div class="group"><form method="post" action="{WEIGHT}/{weight_id}/delete"'
            f' data-confirm="Delete the weigh-in from {e(label)}?">'
            f'<input type="hidden" name="back" value="{e(back)}">'
            '<button class="item danger" type="submit"><span class="badge-icon tone-over">'
            f'{icon("trash-2")}</span><span class="grow">Delete this weigh-in</span></button>'
            "</form></div>"
        )
        return sheet_or_page(request, "Weigh-in", sheet, back, "progress")

    @server.custom_route(WEIGHT + "/{weight_id:int}/delete", methods=["POST"])
    async def weight_delete(request: Request) -> Response:
        if (no := refused(request, "Delete weigh-ins")) is not None:
            return no
        form = await request.form()
        back = safe_next(str(form.get("back", ""))) if form.get("back") else WEIGHT_PAGE
        with connect(request) as conn:
            try:
                row = store.delete_weight(conn, request.path_params["weight_id"])
            except store.NotFound:
                return go(back)
        log.info("dashboard: deleted weigh-in %d", row["id"])
        return weighed(back, "removed")

    def moved(old: str, new: str) -> None:
        """An old page's address (#105) sends a GET on to where it lives now, query and all."""

        async def redirect(request: Request) -> Response:
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(new + query, status_code=301, headers=SECURITY_HEADERS)

        server.custom_route(old, methods=["GET"])(redirect)

    moved(f"{PREFIX}/history", NUTRITION_PAGE)
    moved(ACTIVITY, ACTIVITY_PAGE)
    moved(WORKOUTS, TRAINING_PAGE)

    def progress_page(request: Request, title: str, body: str, hue: str = "") -> Response:
        return render(
            title,
            body,
            "progress",
            signed_in=provider is not None,
            user=account(request),
            hue=hue,
        )

    def first_day(started: str | None, fallback: date) -> date:
        """The local day of the first record (for All), never after `fallback`."""
        return min(logic.local_date(started, tz), fallback) if started else fallback

    # --- Progress (#105): Overview, Nutrition, Weight, Activity, Training ---

    def range_days(span: int, started: str | None) -> tuple[date, date]:
        """A range's first and last day: the last `span` days through today, or for All
        (0) from the first record's day."""
        last = today()
        first = first_day(started, last) if span == 0 else last - timedelta(days=span - 1)
        return first, last

    def bounds(first: date, last: date) -> tuple[str, str]:
        lo, _ = logic.local_day_bounds(first, tz)
        _, hi = logic.local_day_bounds(last, tz)
        return lo, hi

    def gym_progress(conn, day: date) -> logic.GymGoal | None:
        """The weekly gym-day goal on a day, from the days with a workout; None without a goal."""
        goal = store.get_gym_goal(conn)
        if not goal:
            return None
        starts = store.workout_starts(conn, bounds(day, day)[1])
        return logic.gym_goal((logic.local_date(s, tz) for s in starts), goal, day)

    def gym_card(g: logic.GymGoal | None, day: date) -> str:
        """Training's Gym days card: this week against the goal, the streak and the weeks
        since the first workout; without a goal, a link to set one."""
        if g is None:
            return (
                '<section class="card"><div class="chart-head"><h2>Gym days</h2></div>'
                f'<p class="muted small"><a href="{GYM_GOAL}">Set a weekly gym-day goal</a>'
                " to track how many days a week you train.</p></section>"
            )
        if g.left == 0:
            status = "Goal met this week"
        elif not g.reachable:
            status = (
                f"{g.left} more needed, {g.days_left} day{'' if g.days_left == 1 else 's'}"
                " left: out of reach this week"
            )
        else:
            status = (
                f"{g.left} more by Sunday · {g.days_left} day{'' if g.days_left == 1 else 's'} left"
            )
        streak = f"{g.streak}-week streak" if g.streak else "No streak yet"
        record = (
            f" · goal met {g.met_weeks} of {g.counted_weeks} week"
            f"{'' if g.counted_weeks == 1 else 's'}"
            if g.counted_weeks
            else ""
        )
        this_monday = day - timedelta(days=day.weekday())
        rows = []
        for w in g.weeks:
            if w.monday == this_monday:
                name = "This week"
            elif w.monday == this_monday - timedelta(days=7):
                name = "Last week"
            else:
                name = f"Week of {w.monday.strftime('%b')} {w.monday.day}"
            if w.met:
                mark = '<span class="badge pr">Met</span>'
            elif w.monday == this_monday:
                mark = '<span class="muted small">so far</span>'
            else:
                mark = '<span class="muted small">missed</span>'
            rows.append(
                f'<li class="item"><span class="grow">{name}</span>'
                f'<span class="kcal">{w.days}<small> / {g.goal}</small></span> {mark}</li>'
            )
        return (
            '<section class="card gym-goal"><div class="chart-head"><h2>Gym days'
            f" <small>goal {g.goal} a week</small></h2>"
            f'<a class="link small" href="{GYM_GOAL}">Change</a></div>'
            f'<p class="big"><b>{g.this_week}</b> of {g.goal} this week</p>'
            f'<p class="inspect">{e(status)} · {e(streak)}{e(record)}</p>'
            f'<ul class="entries">{"".join(rows)}</ul>'
            '<p class="muted small">A day with at least one workout counts; weeks run Monday'
            " to Sunday.</p></section>"
        )

    def previous(span: int, first: date, last: date) -> tuple[date, date] | None:
        """The period before the range, the same length; All has none."""
        return None if span == 0 else logic.previous_period(first, last)

    def mean(values) -> float | None:
        values = [v for v in values if v is not None]
        return sum(values) / len(values) if values else None

    def as_lb(kg: float) -> float:
        return kg / logic.KG_PER_LB

    def lb_num(lb: float) -> str:
        return f"{lb:,.1f}{NBSP}lb"

    def progress_head(active: str, url: str, span: int) -> list[str]:
        return [
            page_head("Progress"),
            progress_pills(active),
            range_control(url, span),
            MAIN_COL,
        ]

    @server.custom_route(PROGRESS, methods=["GET"])
    async def progress(request: Request) -> Response:
        """Progress overview (#105): one range for five cards, each with how it moved since
        the period before and a way into its screen. Energy balance ties food to activity."""
        if not signed_in(request):
            return to_login(request)
        span = pick_range(request.query_params.get("days"), 30)
        last = today()
        with connect(request) as conn:
            started = [
                store.first_meal_at(conn),
                store.first_activity_at(conn),
                store.first_workout_at(conn),
                store.first_weight_at(conn),
            ]
            starts = [s for s in started if s]
            first, last = range_days(span, min(starts) if starts else None)
            prev = previous(span, first, last)
            lo, hi = bounds(prev[0] if prev else first, last)
            eaten = logic.totals_by_day(store.entries_between(conn, lo, hi), tz)
            moved = logic.activity_by_day(store.activity_between(conn, lo, hi), tz)
            strip = strip_days(first, last)
            workouts = store.workouts_between(conn, min(lo, bounds(strip[0], last)[0]), hi)
            sets = store.workout_sets(conn, (r["id"] for r in workouts))
            weights = store.weights_between(
                conn, "0" if span == 0 else bounds(first - timedelta(days=90), last)[0], hi
            )
            targets = store.get_targets(conn)
            synced = store.last_activity_sync(conn)
        days = logic.date_range(first, last)
        # today is still going, so averages use full days; Today's range is today so far
        done = [d for d in days if d != last] or days
        per = "today" if span == 1 else "a day"
        prev_days = logic.date_range(*prev) if prev else []
        words = prev_words(span) if prev else ""
        parts = progress_head("overview", PROGRESS, span)
        cards = []

        # Energy balance: full days with both food logged and energy burned
        both = [d for d in done if d in eaten and logic.burned_kcal(moved.get(d, {})) is not None]
        if both:
            ate = mean(eaten[d]["kcal"] for d in both) or 0.0
            burned = mean(logic.burned_kcal(moved[d]) for d in both) or 0.0
            net = ate - burned
            spark = charts.sparkline(
                [eaten[d]["kcal"] for d in both],
                "kcal",
                also=[logic.burned_kcal(moved[d]) for d in both],
                also_kind="burn",
            )
            cards.append(
                overview_card(
                    NUTRITION_PAGE + f"?days={span}#balance",
                    "flame",
                    "burn",
                    "Energy balance",
                    f"{'&minus;' if net < 0 else '+'}{num(abs(net))}",
                    f"kcal {per}",
                    f'<span class="kcal-ate">{num(ate)} eaten</span> · '
                    f'<span class="kcal-burn">{num(burned)} burned</span>'
                    f" · {len(both)} day{'' if len(both) == 1 else 's'} with both",
                    spark,
                )
            )

        # Weight: the trend's level and how it moved over the range, and its weigh-in days
        if weights:
            trend = logic.weight_trend(
                (logic.local_date(r["measured_at"], tz), r["kg"]) for r in weights
            )
            change, rate = trend_change(trend, first)
            in_range = [(d, v) for d, v in trend if d >= first]
            spark = charts.sparkline([as_lb(v) for _, v in in_range], "weight")
            weighed = {logic.local_date(r["measured_at"], tz): 1.0 for r in weights}
            spark += charts.heat_strip(weighed, strip, "weight", "Weigh-in days")
            sub = f"{change_text(change)} {e(range_words(span, first))}" if in_range else ""
            if rate is not None and round(as_lb(rate), 1):
                sign = "&minus;" if rate < 0 else "+"
                sub += f" · {sign}{num(abs(as_lb(rate)), 1)}{NBSP}lb a week"
            cards.append(
                overview_card(
                    f"{WEIGHT_PAGE}?days={span}",
                    "scale",
                    "protein",
                    "Weight",
                    f"{as_lb(trend[-1][1]):,.1f}",
                    "lb trend",
                    sub or "No weigh-ins in this period",
                    spark,
                )
            )

        # Activity: steps a day over full days, vs the period before
        steps = [moved[d]["steps"] for d in done if "steps" in moved.get(d, {})]
        if steps:
            avg = sum(steps) / len(steps)
            before = mean(moved.get(d, {}).get("steps") for d in prev_days)
            extra = []
            if (a := mean(logic.burned_kcal(moved.get(d, {})) for d in done)) is not None:
                extra.append(f"{num(a)}{NBSP}kcal burned")
            if (x := mean(moved.get(d, {}).get("exercise_min") for d in done)) is not None:
                extra.append(f"{num(x)}{NBSP}min exercise")
            unit = logic.chart_unit(len(days))
            spark = charts.sparkline(
                [
                    v
                    for _, v in logic.bucket_days(
                        ((d, moved.get(d, {}).get("steps")) for d in done), unit
                    )
                ],
                "steps",
                bars=True,
            )
            cards.append(
                overview_card(
                    f"{ACTIVITY_PAGE}?days={span}",
                    "footprints",
                    "steps",
                    "Activity",
                    num(avg),
                    f"steps {per}",
                    " · ".join(extra) + delta_chip(logic.change_percent(avg, before), words, "up"),
                    spark,
                    fresh=f"Apple Health · {sent_text(synced, last)}" if synced else "",
                )
            )

        elif started[1]:
            cards.append(
                empty_card(
                    f"{ACTIVITY_PAGE}?days={span}", "footprints", "steps", "Activity", "steps"
                )
            )

        # Training: sessions a week and volume, vs the period before, and its days
        mine = [r for r in workouts if logic.local_date(r["started_at"], tz) >= first]
        if mine:
            before = [
                r
                for r in workouts
                if prev and prev[0] <= logic.local_date(r["started_at"], tz) <= prev[1]
            ]
            volume = sum(as_lb(logic.workout_volume_kg(sets[r["id"]])) for r in mine)
            prior = sum(as_lb(logic.workout_volume_kg(sets[r["id"]])) for r in before)
            weekly = len(mine) / max(len(days) / 7, 1)
            by_day: dict[date, float] = {}
            for r in workouts:
                d = logic.local_date(r["started_at"], tz)
                by_day[d] = by_day.get(d, 0) + max(logic.workout_volume_kg(sets[r["id"]]), 1)
            heat = charts.heat_strip(by_day, strip, "train", "Training days")
            cards.append(
                overview_card(
                    f"{TRAINING_PAGE}?days={span}",
                    "dumbbell",
                    "train",
                    "Training",
                    num(weekly, 1),
                    "workouts a week",
                    f"{num(volume)}{NBSP}lb lifted"
                    + delta_chip(logic.change_percent(volume, prior if before else None), words),
                    heat,
                )
            )

        elif started[2]:
            cards.append(
                empty_card(
                    f"{TRAINING_PAGE}?days={span}", "dumbbell", "train", "Training", "workouts"
                )
            )

        # Nutrition: calories and protein a day against the goals, days on target
        logged = [d for d in done if d in eaten] or ([last] if last in eaten else [])
        if logged:
            kcal = mean(eaten[d]["kcal"] for d in logged) or 0.0
            protein = mean(eaten[d]["protein_g"] for d in logged) or 0.0
            on = sum(1 for d in logged if logic.near_goals(eaten[d], targets))
            goal = f" of {num(targets['kcal'])}" if targets["kcal"] else ""
            spark = charts.sparkline(
                [(eaten.get(d) or {}).get("kcal") for d in done or days], "kcal", bars=True
            )
            cards.append(
                overview_card(
                    f"{NUTRITION_PAGE}?days={span}",
                    "salad",
                    "kcal",
                    "Nutrition",
                    num(kcal),
                    f"kcal {per}{goal}",
                    f"{num(protein)}{NBSP}g protein · on target {on} of {len(logged)} days"
                    f" · {len(logged)} of {len(done) or 1} days logged",
                    spark,
                )
            )

        elif started[0]:
            cards.append(
                empty_card(f"{NUTRITION_PAGE}?days={span}", "salad", "kcal", "Nutrition", "meals")
            )

        if cards:
            parts.append(f'<div class="overview">{"".join(cards)}</div>')
        else:
            parts.append(
                '<section class="card empty"><p>Nothing to show yet.</p>'
                '<p class="muted small">Log food, add a weigh-in, or connect Apple Health and'
                f' Hevy in <a href="{INTEGRATIONS}">Integrations</a>, and your trends appear'
                " here.</p></section>"
            )
        return progress_page(request, "Progress", "".join(parts))

    def overview_card(
        href: str,
        glyph: str,
        tone: str,
        title: str,
        value: str,
        unit: str,
        sub: str,
        visual: str = "",
        fresh: str = "",
    ) -> str:
        tag = f'<span class="fresh">{e(fresh)}</span>' if fresh else ""
        return (
            f'<a class="ov-card tone-{tone}" href="{e(href)}"><div class="ov-head">'
            f'<span class="badge-icon tone-{tone}">{icon(glyph)}</span><span class="grow">'
            f"{e(title)}</span>{tag}{icon('chevron-right', 'icon chev')}</div>"
            f'<div class="ov-value"><span class="val">{value}</span> <span class="unit">'
            f'{e(unit)}</span></div><div class="sub">{sub}</div>{visual}</a>'
        )

    def empty_card(href: str, glyph: str, tone: str, title: str, what: str) -> str:
        """An overview card for an area with data, just none in the range."""
        return overview_card(href, glyph, tone, title, "&ndash;", "", f"No {what} in this period")

    def strip_days(first: date, last: date) -> list[date]:
        """A heat strip's days: the selected range, at most a year, to today."""
        return logic.date_range(max(first, last - timedelta(days=370)), last)

    def trend_change(trend: list[tuple[date, float]], first: date) -> tuple[float, float | None]:
        """How the weight trend moved over a range starting on first: from its value going
        into the range (the last before it, else the first in it) to its latest; and that
        as a weekly rate."""
        before = [p for p in trend if p[0] < first]
        inside = [p for p in trend if p[0] >= first]
        start = before[-1] if before else (inside[0] if inside else trend[-1])
        end = inside[-1] if inside else trend[-1]
        return end[1] - start[1], logic.weekly_rate([start, end])

    @server.custom_route(NUTRITION_PAGE, methods=["GET"])
    async def history(request: Request) -> Response:
        """Nutrition trends (was History): averages against the goals and the period before,
        calories and protein per day (per week or month over long ranges) with goal lines
        and a 7-day rolling average, energy balance when activity is here, and each day."""
        if not signed_in(request):
            return to_login(request)
        span = pick_range(request.query_params.get("days"), 7)
        with connect(request) as conn:
            first, last = range_days(span, store.first_meal_at(conn))
            prev = previous(span, first, last)
            lo, hi = bounds(prev[0] if prev else first, last)
            everything = logic.totals_by_day(store.entries_between(conn, lo, hi), tz)
            targets = store.get_targets(conn)
            moved = logic.activity_by_day(store.activity_between(conn, *bounds(first, last)), tz)
        by_day = {d: n for d, n in everything.items() if d >= first}
        before = {d: n for d, n in everything.items() if d < first}
        days = logic.date_range(first, last)
        prev_days = logic.date_range(*prev) if prev else []
        words = prev_words(span) if prev else ""
        here = f"{NUTRITION_PAGE}?days={span}"
        parts = progress_head("nutrition", NUTRITION_PAGE, span)
        if not by_day:
            parts.append(
                '<section class="card empty"><p>Nothing logged in this period.</p>'
                f'<p class="muted small"><a href="{PREFIX}/add">Log food</a> and your'
                " averages and trends show here.</p></section>"
            )
            return progress_page(request, "Nutrition", "".join(parts))

        # Averages over full days; today alone only when it's all there is
        full = [d for d in days if d in by_day and d != last] or [last]
        so_far = full == [last]

        def avg(key: str, among, source) -> float | None:
            return mean((source[d].get(key) or 0.0) for d in among if d in source)

        kcal, protein = avg("kcal", full, by_day), avg("protein_g", full, by_day)
        on = sum(1 for d in full if logic.near_goals(by_day[d], targets))

        def goal_sub(key: str, unit: str) -> str:
            goal = targets.get(key)
            return f"of {num(goal)}{NBSP}{unit} goal" if goal else "no goal set"

        parts.append(
            tiles(
                tile(
                    f'<span class="kcal-ate">{num(kcal)}</span>',
                    "Calories a day",
                    goal_sub("kcal", "kcal")
                    + delta_chip(logic.change_percent(kcal, avg("kcal", prev_days, before)), words),
                    tone="kcal",
                ),
                tile(
                    f'<span class="p">{num(protein)}{NBSP}g</span>',
                    "Protein a day",
                    goal_sub("protein_g", "g")
                    + delta_chip(
                        logic.change_percent(protein, avg("protein_g", prev_days, before)),
                        words,
                        "up",
                    ),
                    tone="protein",
                ),
                tile(
                    f"{on}",
                    "Days on target",
                    f"of {len(full)} within 10% of every goal",
                    tone="kcal",
                ),
                tile(
                    f"{len(by_day)}",
                    "Days logged",
                    f"of {len(days)} days" + (" · today so far" if so_far else ""),
                    tone="kcal",
                ),
            )
        )
        if len(by_day) < len(days) and span != 0:
            parts.append(
                f'<p class="muted small note">{len(by_day)} of {len(days)} days have entries;'
                " days without any are gaps, not zeros.</p>"
            )

        unit = logic.chart_unit(len(days))
        # Today is one bar: the tiles and the day's row say it, so no charts
        for key, kind, label, unit_word in (
            ()
            if span == 1
            else (
                ("kcal", "kcal", "Calories", "kcal"),
                ("protein_g", "protein_g", "Protein", "g"),
            )
        ):
            daily = [(d, (by_day.get(d) or {}).get(key)) for d in days]
            points = logic.bucket_days(daily, unit)
            ghost = None
            if prev_days:
                earlier = logic.bucket_days(
                    [(d, (before.get(d) or {}).get(key)) for d in prev_days], unit
                )
                if len(earlier) == len(points):
                    ghost = [v for _, v in earlier]
            # Today is still going, so the rolling average stops at yesterday
            rolling = (
                [v for _, v in logic.rolling_average(daily[:-1])] + [None]
                if unit == "day" and len(days) > 7
                else None
            )
            goal = targets.get(key)
            mean_v = kcal if key == "kcal" else protein

            def tone(v: float, goal=goal, key=key) -> str:
                if key != "kcal" or not goal or unit != "day":
                    return ""
                return "over" if v > goal + 100 else ("caution" if v > goal else "")

            summary = (
                f"Avg {num(mean_v)}{NBSP}{unit_word} a day"
                + (f" · goal {num(goal)}" if goal else "")
                + (" · 7-day average line" if rolling else "")
            )
            parts.append(
                '<section class="card">'
                + charts.bar_chart(
                    points,
                    kind=kind,
                    title=f"{label} per {unit} {range_words(span, first)}",
                    value_text=lambda v, w=unit_word: f"{num(v)}{NBSP}{w}",
                    summary=summary,
                    unit=unit,
                    goal=goal,
                    rolling=rolling,
                    ghost=ghost,
                    partial_last=True,
                    tone=tone,
                    href=day_url,
                    heading=label + ("" if unit == "day" else f" <small>a day, by {unit}</small>"),
                )
                + "</section>"
            )

        # Energy balance: eaten against burned, on full days with both
        both = [
            (d, by_day[d]["kcal"] or 0.0, burned)
            for d in days
            if d != last and d in by_day and (burned := logic.burned_kcal(moved.get(d, {})))
        ]
        if both:
            ate = mean(a for _, a, _ in both) or 0.0
            out = mean(b for _, _, b in both) or 0.0
            net = ate - out
            stops = [
                (
                    d,
                    charts.Stop(
                        f"{d.strftime('%a %b')} {d.day}",
                        f"{num(a)} eaten · {num(b)} burned",
                        f"{MINUS if a < b else '+'}{num(abs(a - b))} net",
                        href=day_url(d),
                    ),
                )
                for d, a, b in both
            ]
            parts.append(
                '<section class="card" id="balance">'
                + charts.line_chart(
                    first=both[0][0],
                    last=both[-1][0],
                    lines=[
                        charts.Line("kcal", [(d, a) for d, a, _ in both]),
                        charts.Line("burn", [(d, b) for d, _, b in both]),
                    ],
                    title="Calories eaten and burned per day",
                    summary=(
                        f'<span class="kcal-ate">{num(ate)} eaten</span> · '
                        f'<span class="kcal-burn">{num(out)} burned</span> a day · '
                        f"{'&minus;' if net < 0 else '+'}{num(abs(net))}{NBSP}kcal net"
                    ),
                    stops=stops,
                    value_text=lambda v: f"{num(v)}{NBSP}kcal",
                    zero=False,
                    table_head=("Day", "Eaten", "Burned"),
                    table=[(f"{d.strftime('%a %b')} {d.day}", num(a), num(b)) for d, a, b in both],
                    heading="Energy balance",
                    kind="balance",
                )
                + f'<p class="muted small">Over {len(both)} day{"" if len(both) == 1 else "s"}'
                " with both food logged and calories burned from Apple Health.</p>"
                "</section>"
            )

        rows = []
        for d in reversed(days):
            n = by_day.get(d)
            if n is None:
                continue
            shown = logic.round_for_display(n)
            rows.append(
                f'<li><a class="item" href="{day_url(d)}"><span class="grow">'
                f"{e(day_title(d, last))}{' <em>so far</em>' if d == last else ''}"
                f"<small>{macros_line(shown)}</small>{split_bar(shown)}</span>"
                f'<span class="kcal">{num(shown["kcal"])}<small> kcal</small></span>'
                f"{icon('chevron-right', 'icon chev')}</a>"
                f'<a class="small bd" href="{e(breakdown_url(d, back=here))}">'
                f"Breakdown&nbsp;&rsaquo;</a></li>"
            )
        parts.append(
            f'{SIDE_COL}<section class="group"><h2>Days</h2><ul class="days">{"".join(rows)}</ul>'
            "</section>"
        )
        return progress_page(request, "Nutrition", "".join(parts))

    @server.custom_route(WEIGHT_PAGE, methods=["GET", "POST"])
    async def weight_view(request: Request) -> Response:
        """Body weight in lb: the trend weight (smoothed over about 10 days) as the hero,
        how it moved over the range and a week, every reading under the trend line with the
        goal, lowest, highest and average against the period before, and each weigh-in by
        month (swipe left to delete). Adding one posts here."""
        if request.method == "POST":
            return await weight_add(request)

        if not signed_in(request):
            return to_login(request)
        params = request.query_params
        span = pick_range(params.get("days"), 30)
        last = today()
        with connect(request) as conn:
            latest = store.latest_weight(conn)
            first, last = range_days(span, store.first_weight_at(conn))
            prev = previous(span, first, last)
            # The trend needs a run-up before the range, so it starts settled
            warm = (prev[0] if prev else first) - timedelta(days=60)
            lo, hi = bounds(warm, last)
            every = store.weights_between(conn, "0" if span == 0 else lo, hi)
            goals = store.get_body_goals(conn)
        rows = [r for r in every if logic.local_date(r["measured_at"], tz) >= first]
        earlier = (
            [r for r in every if prev[0] <= logic.local_date(r["measured_at"], tz) <= prev[1]]
            if prev
            else []
        )
        readings = [(logic.local_date(r["measured_at"], tz), r["kg"]) for r in every]
        trend = logic.weight_trend(readings)
        if not trend and latest is not None:
            trend = [(logic.local_date(latest["measured_at"], tz), latest["kg"])]
        goal_lb = as_lb(goals["goal_weight_kg"]) if goals["goal_weight_kg"] else None

        banner = ""
        if (problem := PROBLEMS.get(params.get("problem", ""))) is not None:
            banner = toast(f"<span>{e(problem)}</span>", "alert", " bad")
        elif said := WEIGHED.get(params.get("weighed", "")):
            banner = toast(f"<span>{said}</span>", "status")
        here = f"{WEIGHT_PAGE}?days={span}"
        add_url_ = f"{WEIGHT}/new?{urlencode({'back': here})}"
        add = (
            f'<a class="iconbtn" href="{e(add_url_)}" data-sheet aria-label="Add a weigh-in">'
            f"{icon('plus')}</a>"
        )
        parts = [
            page_head("Progress", add),
            progress_pills("weight"),
            range_control(WEIGHT_PAGE, span),
            banner,
        ]

        if latest is not None and trend:
            local = logic.local_iso(latest["measured_at"], tz)
            when = datetime.fromisoformat(local)
            chips = ""
            if rows:
                change, rate = trend_change(trend, first)
                over = e(range_words(span, first))
                chips = f'<span class="delta">{change_text(change)} {over}</span>'
                if rate is not None and logic.weight_out(abs(rate))["lb"]:
                    per = logic.weight_out(abs(rate))["lb"]
                    sign = "&minus;" if rate < 0 else "+"
                    chips += f'<span class="delta">{sign}{num(per, 1)}{NBSP}lb a week</span>'
            to_goal = ""
            if goal_lb is not None:
                gap = as_lb(trend[-1][1]) - goal_lb
                to_goal = (
                    f'<p class="small muted">Goal {lb_num(goal_lb)} · '
                    + ("reached" if abs(gap) < 0.1 else f"{num(abs(gap), 1)}{NBSP}lb to go")
                    + "</p>"
                )
            parts.append(
                hero(
                    f"{as_lb(trend[-1][1]):,.1f}",
                    "lb",
                    f"Trend weight · latest {lb_text(latest['kg'])}, {e(when.strftime('%a %b'))}"
                    f" {when.day}, {clock(local)}",
                    chips,
                    to_goal,
                )
            )

        if rows:
            daily: dict[date, list[float]] = {}
            for r in rows:
                daily.setdefault(logic.local_date(r["measured_at"], tz), []).append(r["kg"])
            dots = [(d, as_lb(sum(v) / len(v))) for d, v in daily.items()]
            smooth = dict(trend)
            shown_trend = [(d, as_lb(v)) for d, v in trend if d >= dots[0][0]]
            stops = [
                (
                    d,
                    charts.Stop(
                        f"{d.strftime('%a %b')} {d.day}",
                        lb_num(v),
                        f"trend {lb_num(as_lb(smooth[d]))}",
                    ),
                )
                for d, v in dots
            ]
            parts.append(
                '<section class="card">'
                + charts.line_chart(
                    first=dots[0][0],
                    last=max(last, dots[-1][0]),
                    lines=[charts.Line("trend", shown_trend)],
                    dots=dots,
                    title=f"Weight {range_words(span, first)}: each weigh-in and the trend",
                    summary=(
                        f"{len(dots)} weigh-in day{'' if len(dots) == 1 else 's'}"
                        " · dots are readings, the line is the trend"
                    ),
                    stops=stops,
                    value_text=lb_num,
                    goal=goal_lb,
                    table_head=("Day", "Weight", "Trend"),
                    table=[(s.title, s.value, s.note.removeprefix("trend ")) for _, s in stops],
                    kind="weight",
                    heading="Weight",
                )
                + "</section>"
            )
            values = [as_lb(r["kg"]) for r in rows]
            average = sum(values) / len(values)
            prior = mean(as_lb(r["kg"]) for r in earlier)
            parts.append(
                tiles(
                    tile(lb_num(min(values)), "Lowest", tone="protein"),
                    tile(lb_num(max(values)), "Highest", tone="protein"),
                    tile(lb_num(average), "Average", tone="protein"),
                    tile(
                        change_text((average - prior) * logic.KG_PER_LB)
                        if prior is not None
                        else "&ndash;",
                        "Vs " + (prev_words(span) if prev else "before"),
                        "average to average" if prior is not None else "nothing to compare",
                        tone="protein",
                    ),
                )
            )

        if rows:
            months: dict[tuple[int, int], list] = {}
            for r in reversed(rows):
                d = logic.local_date(r["measured_at"], tz)
                months.setdefault((d.year, d.month), []).append(r)
            groups = "".join(
                f'<section class="card"><h2>{date(y, m, 1).strftime("%B %Y")}'
                f' <small class="muted">{len(rs)} weigh-in{"" if len(rs) == 1 else "s"}'
                f'</small></h2><ul class="entries">'
                + "".join(weight_item(r, tz, here) for r in rs)
                + "</ul></section>"
                for (y, m), rs in months.items()
            )
            parts.append(f'{SIDE_COL}<div class="weigh-ins">{groups}</div>')
        elif latest is None:
            parts.append(
                '<section class="card empty"><p>No weigh-ins yet.</p><p class="muted small">'
                "Your scale's readings arrive from Apple Health once your phone is set up, or"
                " tell Claude “I weighed 182.4 this morning”.</p></section>"
            )
        else:
            parts.append(
                '<section class="card empty"><p>No weigh-ins in this period.</p></section>'
            )

        return progress_page(request, "Weight", "".join(parts), "weight")

    @server.custom_route(ACTIVITY_PAGE, methods=["GET"])
    async def activity_view(request: Request) -> Response:
        """Activity from Apple Health. Today (the default): today's numbers, 0 until the
        phone sends some. A longer range: one metric at a time (steps, energy, exercise,
        distance) as the hero against the period before, its bars with the average, the
        step goal and the previous period, and each day (tap for all its numbers)."""
        if not signed_in(request):
            return to_login(request)
        params = request.query_params
        span = pick_range(params.get("days"), 1)
        pick = params.get("metric", "steps")
        chart = ACTIVITY_CHARTS.get(pick) or ACTIVITY_CHARTS["steps"]
        pick = next(k for k, v in ACTIVITY_CHARTS.items() if v is chart)
        with connect(request) as conn:
            first, last = range_days(span, store.first_activity_at(conn))
            prev = previous(span, first, last)
            lo, hi = bounds(prev[0] if prev else first, last)
            everything = logic.activity_by_day(store.activity_between(conn, lo, hi), tz)
            synced = store.last_activity_sync(conn)
            goals = store.get_body_goals(conn)
        by_day = {d: v for d, v in everything.items() if d >= first}
        parts = progress_head("activity", ACTIVITY_PAGE, span)
        if synced is None:
            parts.append(
                '<section class="card empty"><p>No activity yet.</p><p class="muted small">'
                "Steps, energy, exercise minutes and distance arrive from Apple Health"
                f' once your phone sends them. <a href="{INTEGRATIONS}#health">Set it up in'
                " Integrations</a>.</p></section>"
            )
            return progress_page(request, "Activity", "".join(parts), "steps")
        parts.append(
            fresh_tag(
                f"{INTEGRATIONS}#health", "heart-pulse", "Apple Health", sent_text(synced, last)
            )
        )

        if span == 1:
            # Today is today (#105): nothing sent yet reads as 0, not as yesterday
            values = dict.fromkeys(SUMMARY_METRICS, 0) | by_day.get(last, {})
            step_goal = goals["step_goal"]
            goal_line = (
                f"{num(values['steps'] / step_goal * 100)}% of your {num(step_goal)} step goal"
                if step_goal
                else "steps today so far"
            )
            parts.append(
                hero(
                    num(values["steps"]),
                    "steps",
                    e(goal_line),
                    extra=(meter(values["steps"] / step_goal * 100, "steps") if step_goal else ""),
                )
            )
            parts.append(activity_tiles(values, " so far"))
            return progress_page(request, "Activity", "".join(parts), "steps")

        # A range: today is still going, so the averages are over its full days
        days = logic.date_range(first, last)
        done = {d: v for d, v in by_day.items() if d != last}
        prev_days = logic.date_range(*prev) if prev else []
        words = prev_words(span) if prev else ""
        switch = "".join(
            f'<a href="{ACTIVITY_PAGE}?{urlencode({"days": span, "metric": key})}"'
            f"{' aria-current=page' if key == pick else ''}>{c.label}</a>"
            for key, c in ACTIVITY_CHARTS.items()
        )
        parts.append(f'<nav class="segment metrics" aria-label="Metric">{switch}</nav>')

        values = {d: chart.value(v) for d, v in done.items()}
        values = {d: v for d, v in values.items() if v is not None}
        if values:
            avg = sum(values.values()) / len(values)
            before = mean(chart.value(everything.get(d, {})) for d in prev_days)
            best_day = max(values, key=lambda d: values[d])
            daily_word = {
                "steps": "steps a day",
                "energy": "kcal burned a day",
                "exercise": "min of exercise a day",
                "distance": "miles a day",
            }[pick]
            parts.append(
                hero(
                    num(avg, chart.places),
                    "",
                    f"{e(daily_word)} {e(range_words(span, first))}",
                    delta_chip(logic.change_percent(avg, before), words, "up"),
                )
            )
            parts.append(
                tiles(
                    tile(
                        chart.text(values[best_day]),
                        "Best day",
                        e(short_date(best_day, last).capitalize()),
                        tone=chart.kind,
                    ),
                    tile(
                        f"{len(values)}",
                        "Days with data",
                        f"of {len(days) - 1} full days",
                        tone=chart.kind,
                    ),
                )
            )
            unit = logic.chart_unit(len(days))
            daily = [(d, chart.value(by_day.get(d, {}))) for d in days]
            points = logic.bucket_days(daily, unit)
            ghost = None
            if prev_days:
                earlier = logic.bucket_days(
                    [(d, chart.value(everything.get(d, {}))) for d in prev_days], unit
                )
                if len(earlier) == len(points):
                    ghost = [v for _, v in earlier]
            goal = goals["step_goal"] if pick == "steps" else None
            parts.append(
                '<section class="card">'
                + charts.bar_chart(
                    points,
                    kind=chart.kind,
                    title=f"{chart.label} per {unit} {range_words(span, first)}",
                    value_text=chart.text,
                    summary=f"Avg {chart.text(avg)} a day"
                    + delta_chip(logic.change_percent(avg, before), words, "up"),
                    unit=unit,
                    goal=goal,
                    goal_label="goal",
                    average=avg,
                    ghost=ghost,
                    partial_last=True,
                    ref_text=lambda v: num(v, chart.places),
                    heading=chart.label
                    + ("" if unit == "day" else f" <small>a day, by {unit}</small>"),
                )
                + "</section>"
            )
        else:
            parts.append(
                f'<section class="card empty"><p>No {e(chart.label.lower())} in this period.'
                "</p></section>"
            )
        if done:
            parts.append(
                '<section class="group"><h2>Averages</h2><div class="body">'
                + activity_tiles(
                    logic.activity_average(done),
                    burned=mean(logic.burned_kcal(v) for v in done.values()),
                )
                + "</div></section>"
            )
        if by_day:
            # Years of an imported export would be thousands of rows: the latest 100 days
            recent = list(reversed(by_day))[:ACTIVITY_LIST_MAX]
            items = "".join(
                activity_row(
                    d, by_day[d], last, f"{ACTIVITY_PAGE}/day?{urlencode({'date': d.isoformat()})}"
                )
                for d in recent
            )
            more = (
                f'<p class="small muted">The latest {ACTIVITY_LIST_MAX} days with activity.</p>'
                if len(by_day) > ACTIVITY_LIST_MAX
                else ""
            )
            parts.append(
                f'{SIDE_COL}<section class="group"><h2>Days</h2><ul class="days">{items}</ul>'
                f"{more}</section>"
            )
        else:
            parts.append('<section class="card empty"><p>No activity in this period.</p></section>')
        return progress_page(request, "Activity", "".join(parts), "steps")

    def activity_tiles(
        values: Mapping[str, float], so_far: str = "", burned: float | None = None
    ) -> str:
        """Calories burned (active + resting as one number, or `burned` when given: an
        average over the days that have both), exercise and miles."""
        if burned is None:
            burned = logic.burned_kcal(values)
        miles = num(logic.miles(values["distance_m"]), 1) if "distance_m" in values else num(None)
        return tiles(
            tile(num(burned), "Kcal burned", so_far.strip(), tone="burn"),
            tile(num(values.get("exercise_min")), "Exercise min", tone="exercise"),
            tile(miles, "Miles", tone="steps"),
        )

    @server.custom_route(ACTIVITY_PAGE + "/day", methods=["GET"])
    async def activity_day(request: Request) -> Response:
        """One day's activity, all its numbers: a sheet over Activity (?part=1), else a page."""
        if not signed_in(request):
            return to_login(request)
        try:
            day = parse_day(request.query_params.get("date"))
        except ValueError:
            return go(ACTIVITY_PAGE)
        with connect(request) as conn:
            values = logic.activity_by_day(store.activity_between(conn, *bounds(day, day)), tz)
        found = values.get(day, {})
        last = today()
        rows = [
            ("Steps", num(found.get("steps"))),
            ("Calories burned", f"{num(logic.burned_kcal(found))}{NBSP}kcal"),
            ("Exercise", f"{num(found.get('exercise_min'))}{NBSP}min"),
            (
                "Distance",
                f"{num(logic.miles(found['distance_m']), 1)}{NBSP}mi"
                if "distance_m" in found
                else "&ndash;",
            ),
        ]
        if "flights" in found:
            rows.append(("Flights climbed", num(found["flights"])))
        listing = "".join(
            f'<li class="row"><span>{label}</span><span class="kcal">{value}</span></li>'
            for label, value in rows
        )
        title = day_title(day, last) + (" so far" if day == last else "")
        sheet = (
            f'<h2 id="day-title">{e(title)}</h2><div class="group"><ul class="facts-list">'
            f"{listing}</ul></div>"
            f'<p class="small"><a href="{day_url(day)}">Food that day &rsaquo;</a></p>'
        )
        if request.query_params.get("part"):
            return HTMLResponse(sheet, headers=SECURITY_HEADERS)
        return progress_page(
            request,
            "Activity",
            back_head(ACTIVITY_PAGE, "Activity", "")
            + f'<section class="sheet-page">{sheet}</section>',
            "steps",
        )

    @server.custom_route(TRAINING_PAGE, methods=["GET"])
    async def workouts_view(request: Request) -> Response:
        """Training (was Workouts): workouts, volume, length and sessions a week against the
        period before; weekly volume with the previous period as ghosts; a heat strip of
        training days; personal bests by estimated one-rep max; and the workouts by week.
        The Hevy import lives in You > Integrations (#105)."""
        if not signed_in(request):
            return to_login(request)
        params = request.query_params
        span = pick_range(params.get("days"), 30)
        with connect(request) as conn:
            summary = store.workouts_summary(conn)
            first, last = range_days(span, store.first_workout_at(conn))
            everything = store.workouts_between(conn, "0", bounds(last, last)[1])
            sets = store.workout_sets(conn, (r["id"] for r in everything))
        prev = previous(span, first, last)

        def day_of(r) -> date:
            return logic.local_date(r["started_at"], tz)

        rows = [r for r in everything if first <= day_of(r) <= last]
        earlier = [r for r in everything if prev and prev[0] <= day_of(r) <= prev[1]]
        words = prev_words(span) if prev else ""

        banner = ""
        if params.get("removed"):
            banner = toast("<span>Workout deleted.</span>", "status")
        parts = [*progress_head("training", TRAINING_PAGE, span), banner]
        if summary["count"]:
            parts.append(
                fresh_tag(
                    f"{INTEGRATIONS}#hevy",
                    "dumbbell",
                    "Hevy",
                    # Hevy has no API, so this is as fresh as the last export brought in
                    f"latest workout {short_date(logic.local_date(summary['latest'], tz), last)}",
                )
            )
        if not summary["count"]:
            parts.append(
                '<section class="card empty"><p>No workouts yet.</p><p class="muted small">'
                f'<a href="{INTEGRATIONS}#hevy">Import your Hevy export</a> to bring in your'
                " history.</p></section>"
            )
            return progress_page(request, "Training", "".join(parts), "train")

        def volume(rs) -> float:
            return sum(as_lb(logic.workout_volume_kg(sets[r["id"]])) for r in rs)

        def minutes(rs) -> float | None:
            spans = [
                (logic.parse_utc(r["ended_at"]) - logic.parse_utc(r["started_at"])).total_seconds()
                / 60
                for r in rs
                if r["ended_at"]
            ]
            return sum(spans) / len(spans) if spans else None

        with connect(request) as conn:
            parts.append(gym_card(gym_progress(conn, today()), today()))
        days = logic.date_range(first, last)
        weeks = max(len(days) / 7, 1)
        prior = bool(prev)
        parts.append(
            tiles(
                tile(
                    f"{len(rows)}",
                    "Workouts",
                    delta_chip(
                        logic.change_percent(len(rows), len(earlier) if prior else None),
                        words,
                        "up",
                    ),
                    tone="train",
                ),
                tile(
                    f"{num(volume(rows))}{NBSP}lb",
                    "Volume",
                    delta_chip(
                        logic.change_percent(volume(rows), volume(earlier) if prior else None),
                        words,
                    ),
                    tone="train",
                ),
                tile(
                    f"{num(minutes(rows))}{NBSP}min",
                    "Average length",
                    delta_chip(logic.change_percent(minutes(rows), minutes(earlier)), words),
                    tone="train",
                ),
                tile(
                    num(len(rows) / weeks, 1),
                    "A week",
                    "sessions" + (f" · {num(len(earlier) / weeks, 1)} before" if prior else ""),
                    tone="train",
                ),
            )
        )

        if rows:
            unit = "week" if len(days) <= 400 else "month"
            per_day: dict[date, float] = {}
            for r in everything:
                per_day[day_of(r)] = per_day.get(day_of(r), 0.0) + as_lb(
                    logic.workout_volume_kg(sets[r["id"]])
                )
            points = logic.bucket_days(((d, per_day.get(d)) for d in days), unit, "sum")
            ghost = None
            if prev:
                back = logic.bucket_days(
                    ((d, per_day.get(d)) for d in logic.date_range(*prev)), unit, "sum"
                )
                if len(back) == len(points):
                    ghost = [v for _, v in back]
            parts.append(
                '<section class="card">'
                + charts.bar_chart(
                    points,
                    kind="train",
                    title=f"Volume per {unit} {range_words(span, first)}",
                    value_text=lambda v: f"{num(v)}{NBSP}lb",
                    summary=f"{num(volume(rows))}{NBSP}lb lifted {e(range_words(span, first))}",
                    unit=unit,
                    per_day=False,
                    ghost=ghost,
                    partial_last=True,
                    heading=f"Volume <small>by {unit}</small>",
                )
                + "</section>"
            )
        strip = strip_days(first, last)
        trained = {d: v or 1.0 for d, v in per_day.items()} if rows else {}
        in_strip = sum(1 for d in strip if d in trained)
        parts.append(
            '<section class="card"><div class="chart-head"><h2>Training days</h2></div>'
            f'<p class="inspect">{in_strip} of {len(strip)} day{"" if len(strip) == 1 else "s"}'
            " · darker is more volume</p>"
            + charts.heat_strip(trained, strip, "train", f"Training days, {len(strip)} days")
            + "</section>"
        )

        bests = logic.exercise_bests(
            (day_of(r), r["id"], sets[r["id"]]) for r in reversed(everything)
        )
        if bests:
            items = []
            for b in bests[:8]:
                new = b.day >= first and b.beat_kg is not None
                badge = '<span class="badge pr">PR</span> ' if new else ""
                items.append(
                    f'<a class="item" href="{WORKOUTS}/{b.workout_id}"><span class="grow">'
                    f"{badge}{e(b.exercise)}<small>{lb_num(as_lb(b.weight_kg))} &times;"
                    f" {b.reps} · {e(short_date(b.day, last))}</small></span>"
                    f'<span class="kcal">{num(as_lb(b.e1rm_kg))}<small> lb 1RM</small></span>'
                    f"{icon('chevron-right', 'icon chev')}</a>"
                )
            parts.append(
                '<section class="group"><h2>Personal bests</h2>'
                + "".join(items)
                + '<p class="muted small body">Best set by estimated one-rep max (weight'
                " &times; (1 + reps / 30)), newest first. PR marks a record set in this"
                " period.</p></section>"
            )

        if rows:
            this_week = last - timedelta(days=last.weekday())
            by_week: dict[date, list] = {}
            for r in rows:
                d = day_of(r)
                by_week.setdefault(d - timedelta(days=d.weekday()), []).append(r)
            hint = (
                '<p class="small muted js-only hint">Tap a workout to see its sets. Swipe'
                " left to delete.</p>"
            )
            groups = []
            for monday, rs in by_week.items():
                if monday == this_week:
                    name = "This week"
                elif monday == this_week - timedelta(days=7):
                    name = "Last week"
                else:
                    name = f"Week of {monday.strftime('%b')} {monday.day}"
                groups.append(
                    f'<section class="card"><h2>{name} <small class="muted">{len(rs)}'
                    f" workout{'' if len(rs) == 1 else 's'} · {num(volume(rs))}{NBSP}lb"
                    '</small></h2><ul class="entries">'
                    + "".join(workout_item(r, sets[r["id"]], tz) for r in rs)
                    + "</ul></section>"
                )
            parts.append(SIDE_COL + hint + "".join(groups))
        else:
            parts.append('<section class="card empty"><p>No workouts in this period.</p></section>')
        return progress_page(request, "Training", "".join(parts), "train")

    # --- You (#105): goals, integrations, appearance, account ---

    def you_page(request: Request, title: str, body: str) -> Response:
        return render(
            title, body, "you", signed_in=provider is not None, user=account(request), hue="you"
        )

    @server.custom_route(YOU, methods=["GET"])
    async def you(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        last = today()
        with connect(request) as conn:
            targets = store.get_targets(conn)
            body = store.get_body_goals(conn)
            gym = store.get_gym_goal(conn)
            synced = store.last_activity_sync(conn)
            workouts = store.workouts_summary(conn)
            name = users.name_of(conn, store.user_of(conn)) if provider is not None else None
        body_goal = " · ".join(
            bit
            for bit in (
                f"Goal {lb_text(body['goal_weight_kg'])}" if body["goal_weight_kg"] else "",
                f"{num(body['step_goal'])} steps a day" if body["step_goal"] else "",
            )
            if bit
        )
        if targets["kcal"]:
            goal = f"{num(targets['kcal'])} kcal" + "".join(
                f" · {letter} {num(targets[k])} g" for k, _, letter in MACROS if targets[k]
            )
        else:
            goal = "Not set yet"
        health = f"Last sent {sent_text(synced, last)}" if synced else "Not sending yet"
        hevy_note = (
            f"{workouts['count']} workouts · latest"
            f" {short_date(logic.local_date(workouts['latest'], tz), last)}"
            if workouts["count"]
            else "No workouts imported yet"
        )
        integrations = [
            item(f"{INTEGRATIONS}#health", "heart-pulse", "burn", "Apple Health", e(health)),
            item(f"{INTEGRATIONS}#hevy", "dumbbell", "train", "Hevy", e(hevy_note)),
        ]
        if settings.public_url:
            integrations.append(
                item(f"{INTEGRATIONS}#claude", "sparkles", "protein", "Claude", "Connector address")
            )
            integrations.append(
                item(f"{INTEGRATIONS}#chatgpt", "bot", "protein", "ChatGPT", "Connector setup")
            )
        picks = "".join(
            f'<button type="button" data-theme-pick="{key}">{label}</button>'
            for key, label in (("system", "System"), ("dark", "Dark"), ("light", "Light"))
        )
        account_rows = (
            f'<div class="item"><span class="badge-icon tone-muted">{icon("circle-user")}</span>'
            f'<span class="grow">{e(name or "Signed in")}</span></div>'
            + (
                item(PEOPLE, "users", "train", "People", "Add someone and send them an invite link")
                if is_owner(request)
                else ""
            )
            + f'<form method="post" action="{PREFIX}/logout"><button class="item danger"'
            f' type="submit"><span class="badge-icon tone-muted">{icon("log-out")}</span>'
            '<span class="grow">Sign out</span></button></form>'
            if provider is not None
            else '<div class="body"><p class="muted small">Local mode: this dashboard needs no'
            " sign-in.</p></div>"
        )
        saved = request.query_params.get("saved")
        saved_text = {"body": "Weight and step goals saved.", "gym": "Gym-day goal saved."}
        body = (
            page_head("You")
            + (toast(f"<span>{saved_text[saved]}</span>", "status") if saved in saved_text else "")
            + '<section class="group"><h2>Goals</h2>'
            + item(
                f"{PREFIX}/targets?{urlencode({'back': YOU})}",
                "target",
                "kcal",
                "Daily goals",
                e(goal),
            )
            + item(
                BODY_GOALS,
                "scale",
                "steps",
                "Weight and steps",
                e(body_goal or "Optional: a goal weight and a step goal"),
            )
            + item(
                GYM_GOAL,
                "dumbbell",
                "train",
                "Gym days",
                f"{gym} day{'' if gym == 1 else 's'} a week"
                if gym
                else "Optional: how many days a week you train",
            )
            + "</section>"
            + '<section class="group"><h2>Integrations</h2>'
            + "".join(integrations)
            + "</section>"
            + '<section class="group js-only"><h2>Appearance</h2>'
            f'<div class="segment themes" role="group" aria-label="Appearance">{picks}</div>'
            "</section>"
            + '<section class="group"><h2>Data</h2>'
            + item(
                f"{YOU}/export.csv",
                "download",
                "kcal",
                "Export entries as CSV",
                "Every food you've logged, one row each",
                download="",
            )
            + item(
                f"{YOU}/export.json",
                "database",
                "protein",
                "Export everything as JSON",
                "Meals, foods, recipes, goals, weigh-ins, activity, workouts",
                download="",
            )
            + "</section>"
            + f'<section class="group"><h2>Account</h2>{account_rows}</section>'
            + '<section class="group js-only keys-group"><h2>About</h2>'
            f'<a class="item" href="{PREFIX}/shortcuts" data-sheet>'
            f'<span class="badge-icon tone-muted">{icon("keyboard")}</span>'
            '<span class="grow">Keyboard shortcuts<small>Press ? anywhere to see them'
            f"</small></span>{icon('chevron-right', 'icon chev')}</a></section>"
            + f'<p class="muted small center">Chomp {e(__version__)}</p>'
        )
        return you_page(request, "You", body)

    @server.custom_route(YOU + "/export.{kind:str}", methods=["GET"])
    async def export_file(request: Request) -> Response:
        """You > Data (#105): the log as a download, the same files as `chomp export`. CSV is
        one row per logged food; JSON is everything."""
        if not signed_in(request):
            return to_login(request)
        kind = request.path_params["kind"]
        if kind not in ("csv", "json"):
            return message("Not found", "Exports are CSV or JSON.", 404, signed_in=True)
        out = io.StringIO()
        with connect(request) as conn:
            if kind == "csv":
                export.write_csv(conn, out, tz)
            else:
                export.write_json(conn, out, tz)
        name = f"chomp-{today().isoformat()}.{kind}"
        log.info("dashboard: exported %s", kind)
        return Response(
            out.getvalue(),
            media_type="text/csv" if kind == "csv" else "application/json",
            headers={**SECURITY_HEADERS, "Content-Disposition": f'attachment; filename="{name}"'},
        )

    @server.custom_route(SHORTCUTS, methods=["GET"])
    async def shortcuts(request: Request) -> Response:
        """The keyboard shortcuts (#105), from You or by pressing ? anywhere."""
        if not signed_in(request):
            return to_login(request)
        rows = "".join(
            f'<li><span>{e(what)}</span><span class="keys">'
            + "".join(f"<kbd>{e(k)}</kbd>" for k in keys)
            + "</span></li>"
            for keys, what in SHORTCUT_KEYS
        )
        sheet = (
            '<h2 id="keys-title">Keyboard shortcuts</h2>'
            f'<ul class="group keylist" aria-labelledby="keys-title">{rows}</ul>'
            '<p class="muted small">They wait while you type in a box.</p>'
        )
        return sheet_or_page(request, "Keyboard shortcuts", sheet, YOU, "you")

    @server.custom_route(BODY_GOALS, methods=["GET", "POST"])
    async def body_goals(request: Request) -> Response:
        """Goal weight and daily step goal (#105), both optional: the dashed lines on Weight
        and Activity. A blank box clears that goal."""
        error = ""
        if request.method == "POST":
            if (no := refused(request, "Change goals")) is not None:
                return no
            form = await request.form()
            typed = {k: str(form.get(k, "")).replace(",", "").strip()[:12] for k in ("lb", "steps")}
            values: dict[str, float | None] = {}
            try:
                values["goal_weight_kg"] = (
                    logic.to_kg(float(typed["lb"]), "lb") if typed["lb"] else None
                )
            except ValueError:
                error = "Goal weight must be a number of pounds, like 170."
            try:
                steps = float(typed["steps"]) if typed["steps"] else None
                if steps is not None and not 0 < steps <= 200_000:
                    raise ValueError(steps)
                values["step_goal"] = steps
            except ValueError:
                error = error or "Step goal must be a number of steps from 1 to 200,000."
            if not error:
                with connect(request) as conn:
                    store.set_body_goals(conn, values)
                log.info("dashboard: updated body goals")
                return go(f"{YOU}?saved=body")
        else:
            if not signed_in(request):
                return to_login(request)
            with connect(request) as conn:
                goals = store.get_body_goals(conn)
            typed = {
                "lb": f"{logic.weight_out(goals['goal_weight_kg'])['lb']:.1f}"
                if goals["goal_weight_kg"]
                else "",
                "steps": f"{goals['step_goal']:.0f}" if goals["step_goal"] else "",
            }
        problem = f'<p class="error" role="alert">{e(error)}</p>' if error else ""
        body = (
            back_head(YOU, "You", "Weight and steps")
            + f'<form class="card stack" method="post" action="{BODY_GOALS}">{problem}'
            '<label>Goal weight (lb)<input name="lb" inputmode="decimal" autocomplete="off"'
            f' placeholder="170" value="{e(typed["lb"])}"></label>'
            '<label>Daily step goal<input name="steps" inputmode="numeric" autocomplete="off"'
            f' placeholder="10000" value="{e(typed["steps"])}"></label>'
            '<p class="muted small">Both are optional: the goal weight is a dashed line on'
            " Weight, the step goal one on Activity. Leave a box empty for no goal.</p>"
            '<button type="submit">Save</button></form>'
        )
        return render(
            "Weight and steps",
            body,
            "you",
            signed_in=provider is not None,
            user=account(request),
            status=400 if error else 200,
            hue="you",
        )

    @server.custom_route(GYM_GOAL, methods=["GET", "POST"])
    async def gym_goal(request: Request) -> Response:
        """The weekly gym-day goal, 1 to 7 days, tracked on Training and Today. A blank box
        clears it."""
        error = ""
        if request.method == "POST":
            if (no := refused(request, "Change goals")) is not None:
                return no
            form = await request.form()
            typed = str(form.get("days", "")).strip()[:4]
            days = None
            if typed:
                try:
                    days = int(typed)
                    if not 1 <= days <= 7:
                        raise ValueError(days)
                except ValueError:
                    error = "Gym days must be a whole number of days a week, 1 to 7."
            if not error:
                with connect(request) as conn:
                    store.set_gym_goal(conn, days)
                log.info("dashboard: updated gym-day goal")
                return go(f"{YOU}?saved=gym")
        else:
            if not signed_in(request):
                return to_login(request)
            with connect(request) as conn:
                goal = store.get_gym_goal(conn)
            typed = str(goal) if goal else ""
        problem = f'<p class="error" role="alert">{e(error)}</p>' if error else ""
        body = (
            back_head(YOU, "You", "Gym days")
            + f'<form class="card stack" method="post" action="{GYM_GOAL}">{problem}'
            '<label>Days a week at the gym<input name="days" type="number" min="1" max="7"'
            f' inputmode="numeric" autocomplete="off" placeholder="6" value="{e(typed)}">'
            "</label>"
            '<p class="muted small">A day with at least one workout (from Hevy) counts. Training'
            " shows this week against the goal, your streak and past weeks. Leave it empty for"
            " no goal.</p>"
            '<button type="submit">Save</button></form>'
        )
        return render(
            "Gym days",
            body,
            "you",
            signed_in=provider is not None,
            user=account(request),
            status=400 if error else 200,
            hue="you",
        )

    # --- People (#109): the owner adds someone and gives them an invite link ---

    def owner_refused(request: Request, post: bool = False) -> Response | None:
        """People is the owner's: anyone else gets Not found, as if it weren't there."""
        if not signed_in(request):
            return to_login(request)
        if not is_owner(request):
            return message("Not found", "There's no such page.", 404, signed_in=True)
        if post and not same_origin(request):
            return message("Not allowed", "Change people from the dashboard.", 403)
        return None

    def invite_until(expires: int) -> str:
        day = datetime.fromtimestamp(expires, ZoneInfo(tz)).date()
        return short_date(day, today())

    def person_status(row) -> str:
        now = time.time()
        if row["id"] == users.OWNER_ID:
            return "Owner · signs in with AUTH_PASSWORD"
        expires = row["invite_expires"]
        open_invite = expires is not None and expires > now
        if row["has_password"]:
            meals = f"{num(row['meals'])} meal{'' if row['meals'] == 1 else 's'} logged"
            if open_invite:
                return f"{meals} · password reset link open until {invite_until(expires)}"
            return meals
        if open_invite:
            return f"Invited · link open until {invite_until(expires)}"
        return "Invite expired · make a new link"

    def people_page(request: Request, error: str = "", typed: str = "") -> Response:
        with connect(request) as conn:
            rows = users.list_users(conn)
        listed = "".join(
            item(f"{PEOPLE}/{r['id']}", "circle-user", "train", r["name"], e(person_status(r)))
            if r["id"] != users.OWNER_ID
            else (
                f'<div class="item"><span class="badge-icon tone-muted">{icon("circle-user")}'
                f'</span><span class="grow">{e(r["name"])}<small>{e(person_status(r))}</small>'
                "</span></div>"
            )
            for r in rows
        )
        problem = f'<p class="error" role="alert">{e(error)}</p>' if error else ""
        body = (
            back_head(YOU, "You", "People")
            + f'<section class="group">{listed}</section>'
            + f'<form class="card stack" method="post" action="{PEOPLE}">'
            f"<h2>{icon('users')} Add a person</h2>{problem}"
            f'<label>Name<input name="name" maxlength="{users.NAME_MAX}" required'
            f' autocomplete="off" value="{e(typed)}"></label>'
            '<p class="small muted">They get their own log, goals, weight, activity and'
            " workouts; foods are shared. You get a link to send them, where they pick their"
            " own password.</p>"
            '<button type="submit">Add person</button></form>'
        )
        return render(
            "People",
            body,
            "you",
            signed_in=True,
            user=account(request),
            status=400 if error else 200,
            hue="you",
        )

    def person_page(
        request: Request, user_id: int, link: str = "", error: str = "", status: int = 200
    ) -> Response:
        with connect(request) as conn:
            row = users.get(conn, user_id)
        if row is None or user_id == users.OWNER_ID:
            return message("Not found", "There's no such person.", 404, signed_in=True)
        name = row["name"]
        url = f"{PEOPLE}/{user_id}"
        shown = ""
        if link:
            shown = (
                f'<section class="card stack" id="invite"><h2>Link for {e(name)}</h2>'
                f'<input readonly id="invite-link" value="{e(link)}" aria-label="Invite link">'
                '<button type="button" class="js-only" data-copy="invite-link">Copy</button>'
                f'<p class="small muted">Send it to {e(name)}. It works once, for'
                f" {users.INVITE_TTL_S // 86400} days: opening it lets them pick their password"
                " and signs them in. It shows only now; make a new one if it gets lost.</p>"
                "</section>"
            )
        if row["has_password"]:
            again = (
                '<p class="small muted">Forgot their password? A reset link lets them pick a'
                " new one; the current one keeps working until the link is used.</p>"
                '<button type="submit">Reset password</button>'
            )
        else:
            again = (
                '<p class="small muted">A new link replaces any earlier one.</p>'
                '<button type="submit">New invite link</button>'
            )
        problem = f'<p class="error" role="alert">{e(error)}</p>' if error else ""
        body = (
            back_head(PEOPLE, "People", name, sub=e(person_status(row)))
            + shown
            + problem
            + f'<form class="card stack" method="post" action="{url}/invite">{again}</form>'
            + f'<form class="card stack" method="post" action="{url}/rename">'
            f'<label>Name<input name="name" maxlength="{users.NAME_MAX}" required'
            f' autocomplete="off" value="{e(name)}"></label>'
            '<button type="submit">Rename</button></form>'
            + f'<form class="card stack" method="post" action="{url}/remove">'
            f"<h2>Remove {e(name)}</h2>"
            '<p class="small muted">Deletes them and their whole log: meals, saved meals,'
            " recipes, goals, weigh-ins, activity and workouts. Foods stay. This can't be"
            f" undone. Type <b>{e(name)}</b> to confirm.</p>"
            '<input name="confirm" autocomplete="off" required aria-label="Name to confirm">'
            '<button type="submit" class="danger">Remove</button></form>'
        )
        return render(
            name, body, "you", signed_in=True, user=account(request), status=status, hue="you"
        )

    def invite_link(token: str) -> str:
        return f"{settings.public_url}{INVITE}/{token}"

    @server.custom_route(PEOPLE, methods=["GET", "POST"])
    async def people(request: Request) -> Response:
        """Everyone with a log here, and Add a person (#109)."""
        if (no := owner_refused(request, request.method == "POST")) is not None:
            return no
        if request.method == "GET":
            return people_page(request)
        form = await request.form()
        typed = str(form.get("name", ""))[:100]
        try:
            with connect(request) as conn:
                user_id = users.add_person(conn, typed)
                token = users.new_invite(conn, user_id)
        except users.UserError as err:
            return people_page(request, capitalized(str(err)) + ".", typed)
        log.info("dashboard: added user %d", user_id)
        return person_page(request, user_id, invite_link(token))

    @server.custom_route(PEOPLE + "/{user_id:int}", methods=["GET"])
    async def person(request: Request) -> Response:
        if (no := owner_refused(request)) is not None:
            return no
        return person_page(request, request.path_params["user_id"])

    @server.custom_route(PEOPLE + "/{user_id:int}/{action:str}", methods=["POST"])
    async def person_action(request: Request) -> Response:
        """A new invite (or password reset) link, a new name, or removing someone."""
        if (no := owner_refused(request, post=True)) is not None:
            return no
        user_id = request.path_params["user_id"]
        action = request.path_params["action"]
        with connect(request) as conn:
            row = users.get(conn, user_id)
        if row is None or user_id == users.OWNER_ID or action not in ("invite", "rename", "remove"):
            return message("Not found", "There's no such person.", 404, signed_in=True)
        form = await request.form()
        try:
            with connect(request) as conn:
                if action == "invite":
                    token = users.new_invite(conn, user_id)
                    log.info("dashboard: new invite link for user %d", user_id)
                    return person_page(request, user_id, invite_link(token))
                if action == "rename":
                    users.rename(conn, user_id, str(form.get("name", ""))[:100])
                    return go(f"{PEOPLE}/{user_id}")
                typed = " ".join(str(form.get("confirm", "")).split())
                if typed.casefold() != row["name"].casefold():
                    raise users.UserError(f"type {row['name']} to remove them")
                users.remove_user(conn, user_id)
                log.info("dashboard: removed user %d", user_id)
                return go(PEOPLE)
        except users.UserError as err:
            return person_page(request, user_id, error=capitalized(str(err)) + ".", status=400)

    INVITE_FORM = (
        '<div class="login"><img src="{p}/static/icon-192.png" alt="" width="72" height="72">'
        "<h1>Welcome, {name}</h1>{error}"
        '<form method="post" action="{action}">'
        '<input type="text" name="username" value="{name}" autocomplete="username" hidden>'
        '<label>Pick a password<input type="password" name="password" autofocus required'
        ' minlength="{min}" autocomplete="new-password"></label>'
        '<label>Same password again<input type="password" name="again" required'
        ' autocomplete="new-password"></label>'
        '<button type="submit">Save and sign in</button></form>'
        '<p class="muted">At least {min} characters. It signs you in here, and you type it when'
        " you connect Claude to Chomp. Keep it in a password manager.</p></div>"
    )

    @server.custom_route(INVITE + "/{token:str}", methods=["GET", "POST"])
    async def invite(request: Request) -> Response:
        """An invite link (#109): the person picks their own password and is signed in. The
        link works once."""
        if provider is None:
            return go(PREFIX)
        token = request.path_params["token"]
        with closing(db.connect(settings.db_path)) as conn:
            user_id = users.invite_user(conn, token)
            name = users.name_of(conn, user_id) if user_id is not None else None
        if user_id is None or name is None:
            return message(
                "Link expired",
                "This invite link has expired or was already used. Ask for a new one.",
                410,
            )

        def page(error: str = "", status: int = 200) -> Response:
            body = INVITE_FORM.format(
                p=PREFIX,
                name=e(name),
                action=e(f"{INVITE}/{token}"),
                min=users.MIN_PASSWORD_LEN,
                error=f'<p class="error">{e(error)}</p>' if error else "",
            )
            return render("Pick a password", body, status=status)

        if request.method == "GET":
            return page()
        if not same_origin(request):
            return page("Pick your password from this page.", 403)
        form = await request.form()
        password = str(form.get("password", ""))
        if password != str(form.get("again", "")):
            return page("The two passwords didn't match.", 400)
        try:
            with closing(db.connect(settings.db_path)) as conn:
                user_id = users.accept_invite(conn, token, password, settings.auth_password)
        except users.UserError as err:
            return page(capitalized(str(err)) + ".", 400)
        log.info("dashboard: user %d set their password from an invite", user_id)
        return start_session(PREFIX, user_id)

    @server.custom_route(INTEGRATIONS, methods=["GET"])
    async def integrations(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        return integrations_page(request)

    def connect_iphone(token: str, has_token: bool, link: str) -> str:
        """Connect iPhone (#109): make the phone's ingest token here and copy it into the
        shared Send to Chomp Shortcut, which asks for it when it is added."""
        if not token:
            if has_token:
                return (
                    f'<form class="stack" method="post" action="{INTEGRATIONS}/ingest-token">'
                    '<p class="small muted">Your phone has a token. A new one is for a new'
                    " phone or a lost token: the old one stops working.</p>"
                    '<button type="submit">Replace token</button></form>'
                )
            return (
                f'<form class="stack" method="post" action="{INTEGRATIONS}/ingest-token">'
                '<button type="submit">Connect iPhone</button></form>'
            )
        if link:
            get_it = (
                f'<li>On your iPhone, open <a href="{e(link)}" rel="noreferrer">the Send to'
                " Chomp Shortcut</a> and tap <b>Add Shortcut</b>. When it asks for your"
                " token, paste it.</li>"
            )
        else:
            get_it = (
                "<li>Build the Send to Chomp Shortcut (docs/apple-health.md) and paste the token"
                " in its header.</li>"
            )
        return (
            '<div class="stack" id="phone-token">'
            '<p class="small"><b>Your phone\'s token.</b> It shows only now; Chomp keeps'
            " no copy. It can only add your weight and activity.</p>"
            f'<input readonly id="ingest-token" value="{e(token)}" aria-label="Ingest token">'
            '<button type="button" class="js-only" data-copy="ingest-token">Copy</button>'
            '<ol class="small">'
            "<li>Copy the token (on your iPhone, or paste it over from a Mac).</li>"
            f"{get_it}"
            "<li>Run it once with &#9654; and allow it to read Health and to send to Chomp"
            " (<b>Always Allow</b>).</li>"
            "<li>Make it run by itself: Shortcuts &rsaquo; <b>Automation</b> &rsaquo; + &rsaquo;"
            " <b>App</b> &rsaquo; an app you open a few times a day (Messages, Hevy) &rsaquo;"
            " <b>Is Opened</b> &rsaquo; <b>Run Immediately</b> &rsaquo; Send to Chomp. iPhone"
            " doesn't share automations, so this step is always by hand.</li>"
            "</ol></div>"
        )

    def shortcut_link_form(link: str) -> str:
        """The owner's place for the shared Shortcut's iCloud link (#109)."""
        return (
            '<details><summary class="small muted">Share your Shortcut</summary>'
            f'<form class="stack" method="post" action="{INTEGRATIONS}/shortcut-link">'
            '<p class="small muted">Share your Send to Chomp Shortcut with its token as an'
            " Import Question (docs/people.md, part 1) and paste its iCloud"
            " link here. Connect iPhone then offers it to everyone. Leave it empty to remove"
            " it.</p>"
            f'<input name="url" type="url" inputmode="url" autocomplete="off" value="{e(link)}"'
            ' placeholder="https://www.icloud.com/shortcuts/..." aria-label="Shortcut link">'
            '<button type="submit">Save link</button></form></details>'
        )

    def hevy_key_form(own_key: bool, server_key: bool) -> str:
        """Your own Hevy API key (#109): paste it and Chomp syncs your workouts."""
        if own_key:
            return (
                f'<form method="post" action="{INTEGRATIONS}/hevy-key"'
                ' data-confirm="Stop syncing with Hevy? Your workouts stay in Chomp.">'
                '<input type="hidden" name="remove" value="1">'
                '<button type="submit" class="danger">Remove your Hevy key</button></form>'
            )
        form = (
            f'<form class="stack" method="post" action="{INTEGRATIONS}/hevy-key"'
            ' data-busy="Checking the key with Hevy">'
            '<p class="small muted">With Hevy Pro, Chomp can pull your workouts by itself. In'
            " Hevy on the web, open hevy.com/settings?developer, make an API key and paste it"
            " here.</p>"
            '<input name="key" type="password" autocomplete="off" required'
            ' aria-label="Hevy API key" placeholder="Hevy API key">'
            '<button type="submit">Save and sync</button></form>'
        )
        if server_key:
            return (
                '<details><summary class="small muted">Use a different Hevy key</summary>'
                f"{form}</details>"
            )
        return form

    def integrations_page(request: Request, token: str = "") -> Response:
        """Where data comes in from (#105): Apple Health (the phone's sends, Connect iPhone and
        the export import), Hevy (your API key, or its CSV), and the Claude and ChatGPT
        connectors. Imports land back here; `token` is a phone token made just now."""
        params = request.query_params
        last = today()
        user_id = user_of(request)
        with connect(request) as conn:
            synced = store.last_activity_sync(conn)
            workouts = store.workouts_summary(conn)
            syncing = hevy_syncs(conn, request)
            sync_state = store.hevy_sync_state(conn) if syncing else None
            own_key = bool(hevy_sync.saved_key(conn, user_id))
            has_token = health.token_info(conn, user_id) is not None
            link = users.get_setting(conn, SHORTCUT_LINK)
        banner = ""
        if (problem := PROBLEMS.get(params.get("problem", ""))) is not None:
            banner = toast(f"<span>{e(problem)}</span>", "alert", " bad")
        elif (done := params.get("health", "")).count("-") == 4:
            weighed, added, updated, kept, skipped = (int_param(x) or 0 for x in done.split("-"))
            text = (
                f"Imported {added} day{'' if added == 1 else 's'} of activity and {weighed}"
                f" weigh-in{'' if weighed == 1 else 's'}"
            )
            extra = [f"{updated} day{'' if updated == 1 else 's'} updated"] if updated else []
            extra += (
                [f"{kept} day{'' if kept == 1 else 's'} kept as your phone sent"] if kept else []
            )
            if skipped:
                extra.append(f"{skipped} unreadable samples skipped")
            text += f" ({', '.join(extra)})." if extra else "."
            banner = toast(
                f'<span>{e(text)}</span><a class="undo" href="{ACTIVITY_PAGE}?days=365">See it</a>',
                "status",
            )
        elif (done := params.get("hevysync", "")).count("-") == 2:
            added, updated, deleted = (int_param(x) or 0 for x in done.split("-"))
            if added or updated or deleted:
                text = (
                    f"Synced with Hevy: {added} new workout{'' if added == 1 else 's'},"
                    f" {updated} updated, {deleted} deleted."
                )
            else:
                text = "Synced with Hevy. Nothing new."
            banner = toast(
                f'<span>{e(text)}</span><a class="undo" href="{TRAINING_PAGE}?days=365">See it</a>',
                "status",
            )
        elif (done := params.get("hevy", "")).count("-") == 3:
            added, updated, same, skipped = (int_param(x) or 0 for x in done.split("-"))
            text = f"Imported {added} new workout{'' if added == 1 else 's'}"
            extra = []
            if updated:
                extra.append(f"{updated} updated")
            if same:
                extra.append(f"{same} already here")
            if skipped:
                extra.append(
                    f"{skipped} skipped because a row couldn't be read; fix it in Hevy and"
                    " export again"
                )
            text += f" ({', '.join(extra)})." if extra else "."
            banner = toast(
                f'<span>{e(text)}</span><a class="undo" href="{TRAINING_PAGE}?days=365">See it</a>',
                "status",
            )
        health_status = (
            f"Your phone last sent data {sent_text(synced, last)}."
            if synced
            else "Your phone hasn't sent anything yet. The Send to Chomp shortcut sends weigh-ins"
            " and each day's activity (docs/apple-health.md)."
        )
        hevy_status = (
            f"{workouts['count']} workouts in Chomp, the latest on"
            f" {short_date(logic.local_date(workouts['latest'], tz), last)}."
            if workouts["count"]
            else "No workouts yet."
        )
        if syncing:
            every = settings.hevy_sync_minutes
            if sync_state is None or sync_state["last_run_at"] is None:
                sync_text = "Chomp syncs with Hevy by itself. The first sync runs shortly."
            else:
                sync_text = (
                    f"Chomp syncs with Hevy every {every} min; last checked"
                    f" {sent_text(sync_state['last_run_at'], last)}."
                )
                if sync_state["last_error"]:
                    sync_text += f" That sync failed: {sync_state['last_error']}"
            whose = (
                "Syncing with your Hevy API key."
                if own_key
                else "Syncing with the server's Hevy key (HEVY_API_KEY)."
            )
            hevy_body = (
                f'<p class="small">{e(whose)} {e(sync_text)}</p>'
                f'<p class="small">{e(hevy_status)}</p>'
                f"{hevy_sync_form()}{hevy_key_form(own_key, not own_key)}"
                '<details><summary class="small muted">Import a CSV export instead</summary>'
                f"{hevy_import()}</details>"
            )
        else:
            hevy_body = (
                f'<p class="small">{e(hevy_status)}</p>{hevy_key_form(False, False)}'
                '<details><summary class="small muted">No Hevy Pro? Import a CSV export'
                f"</summary>{hevy_import()}</details>"
            )
        parts = [
            back_head(YOU, "You", "Integrations"),
            banner,
            f'<section class="card" id="health"><h2>{icon("heart-pulse")} Apple Health</h2>'
            f'<p class="small">{e(health_status)}</p>{connect_iphone(token, has_token, link)}'
            + (shortcut_link_form(link) if user_id == users.OWNER_ID else "")
            + '<details><summary class="small muted">Import your history (export.zip)'
            f"</summary>{health_import()}</details></section>",
            f'<section class="card" id="hevy"><h2>{icon("dumbbell")} Hevy</h2>'
            f"{hevy_body}</section>",
        ]
        if settings.public_url:
            parts.append(
                f'<section class="card" id="claude"><h2>{icon("sparkles")} Claude</h2>'
                '<p class="small muted">Add this address as a custom connector in Claude'
                " (docs/setup.md, step 6) to log and look things up from a chat.</p>"
                f'<input readonly value="{e(settings.public_url.rstrip("/"))}/mcp"'
                ' aria-label="Connector address"></section>'
            )
            callbacks = "".join(
                f"<li><code>{e(url)}</code></li>"
                for url in CONNECTOR_CALLBACKS
                if url.startswith("https://chatgpt.com/")
            )
            client_id = (
                f'<input readonly value="{e(settings.oauth_client_id)}"'
                ' aria-label="OAuth client ID">'
                if settings.oauth_client_id
                else ""
            )
            parts.append(
                f'<section class="card" id="chatgpt"><h2>{icon("bot")} ChatGPT</h2>'
                '<p class="small muted">Add Chomp as a custom MCP server in ChatGPT'
                ' (docs/setup.md, "Add the ChatGPT connector") to log and look things up from a'
                " chat.</p>"
                f'<input readonly value="{e(settings.public_url.rstrip("/"))}/mcp"'
                ' aria-label="ChatGPT connector address">'
                '<ol class="small">'
                "<li>Use the address above as the server URL.</li>"
                "<li>Pick <b>OAuth</b> with <b>User-Defined OAuth Client</b>.</li>"
                "<li>Enter the same <code>OAUTH_CLIENT_ID</code> and"
                " <code>OAUTH_CLIENT_SECRET</code> Chomp runs with"
                f"{' (the client ID is below)' if client_id else ''}.</li>"
                "<li>Choose <b>client_secret_post</b> and leave scopes blank.</li>"
                "<li>Keep the prefilled callback URL and discovered endpoints.</li>"
                f"</ol>{client_id}"
                "<p class=\"small muted\">ChatGPT's callback URL must be on Chomp's allowlist"
                " (<code>CONNECTOR_CALLBACKS</code> in <code>chomp/auth.py</code>). A new or"
                " recreated connector gets its own callback: add that exact URL and redeploy"
                " before connecting."
                + (f' Allowed now:</p><ul class="small">{callbacks}</ul>' if callbacks else "</p>")
                + "</section>"
            )
        return you_page(request, "Integrations", "".join(parts))

    @server.custom_route(INTEGRATIONS + "/ingest-token", methods=["POST"])
    async def ingest_token(request: Request) -> Response:
        """Connect iPhone (#109): a new token for the signed-in person's phone, shown once on
        Integrations. Any older token stops working."""
        if (no := refused(request, "Connect a phone")) is not None:
            return no
        with connect(request) as conn:
            token = health.new_token(conn, user_of(request))
        log.info("dashboard: user %d made a phone token", user_of(request))
        return integrations_page(request, token)

    @server.custom_route(INTEGRATIONS + "/shortcut-link", methods=["POST"])
    async def shortcut_link(request: Request) -> Response:
        """The owner saves (or clears) the shared Send to Chomp Shortcut's iCloud link."""
        if (no := refused(request, "Change the Shortcut link")) is not None:
            return no
        if user_of(request) != users.OWNER_ID:
            return message("Not allowed", "Only the owner can change this.", 403)
        form = await request.form()
        url = str(form.get("url", "")).strip()
        if url and not SHORTCUT_LINK_RE.fullmatch(url):
            return go(f"{INTEGRATIONS}?problem=shortcut_link#health")
        with connect(request) as conn:
            users.set_setting(conn, SHORTCUT_LINK, url)
        return go(f"{INTEGRATIONS}#health")

    @server.custom_route(INTEGRATIONS + "/hevy-key", methods=["POST"])
    async def hevy_key(request: Request) -> Response:
        """Save your Hevy API key and sync with it now, or remove it (#109). A key Hevy
        refuses isn't kept."""
        if (no := refused(request, "Change your Hevy key")) is not None:
            return no
        form = await request.form()
        user_id = user_of(request)
        if form.get("remove"):
            with connect(request) as conn:
                hevy_sync.remove_key(conn, user_id)
            log.info("dashboard: user %d removed their Hevy key", user_id)
            return go(f"{INTEGRATIONS}#hevy")
        key = str(form.get("key", "")).strip()
        if not HEVY_KEY_RE.fullmatch(key):
            return go(f"{INTEGRATIONS}?problem=hevy_key_bad#hevy")
        with connect(request) as conn:
            hevy_sync.save_key(conn, user_id, key)
        result = await anyio.to_thread.run_sync(hevy_sync.run, settings, user_id)
        if result.key_refused:
            with connect(request) as conn:
                hevy_sync.remove_key(conn, user_id)
            return go(f"{INTEGRATIONS}?problem=hevy_key#hevy")
        log.info("dashboard: user %d saved a Hevy key", user_id)
        if result.error:
            return go(f"{INTEGRATIONS}?problem=hevy_sync#hevy")
        done = f"{result.added}-{result.updated}-{result.deleted}"
        return go(f"{INTEGRATIONS}?hevysync={done}#hevy")

    @server.custom_route(ACTIONS, methods=["GET"])
    async def actions(request: Request) -> Response:
        """The + sheet (#105): log food, scan, a saved meal, a weigh-in, new food or recipe.
        ?part=1 is just the sheet for app.js; without the script it's a page of its own."""
        if not signed_in(request):
            return to_login(request)
        params = request.query_params
        try:
            day = parse_day(params.get("date")) if params.get("date") else today()
        except ValueError:
            day = today()
        on = "" if day == today() else f" to {day_title(day, today())}"
        query = f"?{urlencode({'date': day.isoformat()})}" if on else ""
        add = f"{PREFIX}/add{query}"
        meals = f"{add}{'&' if query else '?'}view=meals"
        back = came_from(request, day_url(day) if on else PREFIX)
        weigh = f"{WEIGHT}/new?{urlencode({'back': back})}"
        rows = "".join(
            (
                item(add, "search", "kcal", "Log food", f"Search your foods{e(on)}", data_sheet=""),
                item(
                    f"{PREFIX}/foods/new?how=scan",
                    "scan-barcode",
                    "kcal",
                    "Scan a barcode",
                    "Find a packaged food by its code",
                ),
                item(
                    meals,
                    "utensils",
                    "protein",
                    "Log a saved meal",
                    f"Your usual meals{e(on)}",
                    data_sheet="",
                ),
                item(weigh, "scale", "steps", "Add a weigh-in", data_sheet=""),
                item(
                    f"{PREFIX}/foods/new",
                    "salad",
                    "carbs",
                    "New food",
                    "Barcode, label photo, search or type it in",
                ),
                item(f"{PREFIX}/recipes/new", "book-open", "fat", "New recipe"),
            )
        )
        sheet = f'<h2 id="log-title">Log</h2><div class="group">{rows}</div>'
        if params.get("part"):
            return HTMLResponse(sheet, headers=SECURITY_HEADERS)
        return render(
            "Log",
            f'<section class="sheet-page">{sheet}</section>',
            "",
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
        photos.backfill_soon(settings.db_path)  # once: photos for barcode foods saved before #74
        serving_fill.backfill_soon(settings.db_path, fdc)
        q = request.query_params.get("q", "").strip()[:100]
        params = request.query_params
        sort, desc, per_serving = foods_order(params, request.cookies.get(FOODS_COOKIE, ""))
        back = f"{PREFIX}/foods" + (f"?{urlencode({'q': q})}" if q else "")
        with connect(request) as conn:
            banner = notice(conn, request.query_params, back)
            if (added := int_param(request.query_params.get("added"))) is not None:
                banner += added_notice(conn, added)
        banner += food_toasts(request.query_params)
        with connect(request) as conn:
            if q:
                rows = store.search_foods(
                    conn, q, FOODS_LIMIT, sort, desc, per_serving, typing=bool(params.get("typing"))
                )
            else:
                rows = store.list_used_foods(conn, FOODS_LIMIT, sort, desc, per_serving)
            # Recipes (#82) have their own page; their food rows are only for logging
            recipe_foods = store.recipe_food_ids(conn)
            rows = [r for r in rows if r["id"] not in recipe_foods]

        parts = [
            foods_switch("foods"),
            banner,
            list_search(
                f"{PREFIX}/foods",
                q,
                "Search your foods",
                "Search your foods",
                data_live_search="",
            ),
        ]
        found = []
        if q and not rows:
            found.append(
                f'<section class="card empty"><p>Nothing in your foods matches “{e(q)}”.</p>'
                + elsewhere(q, typed=True)
                + "</section>"
            )
        if rows:
            heading = "Matches" if q else "Your foods"
            day = today()

            def last(row) -> str:
                if sort != "recent" or not row["last_used"]:
                    return ""
                title = day_title(logic.local_date(row["last_used"], tz), day)
                return title.lower() if title in ("Today", "Yesterday") else title

            items = "".join(
                food_item(
                    r,
                    back,
                    last(r),
                    f"{PREFIX}/foods/{r['id']}/log?{urlencode({'back': back})}",
                    photo_src(r["id"]),
                    per_serving,
                )
                for r in rows
            )
            found.append(
                f'<div class="listbar"><h2 class="section-title">{heading}'
                f"<small>{len(rows)}</small></h2>{sort_button(q, sort, desc, per_serving)}</div>"
                f'<section class="group list"><ul class="entries">{items}</ul></section>'
            )
            if q:
                found.append(elsewhere(q))
        elif not q:
            found.append(
                '<section class="card empty"><p>No saved foods yet.</p><p class="muted small">'
                "Scan a barcode, read a label or search the databases, or tell Claude what you"
                f' ate.</p><a class="button" href="{PREFIX}/foods/new">New food</a></section>'
            )
        results = "".join(found)
        if params.get("part") == "1":  # the script's search as you type (#97)
            return HTMLResponse(results, headers=SECURITY_HEADERS)
        parts.append(f'<div data-results aria-live="polite">{results}</div>')
        response = render(
            "Foods", "".join(parts), "foods", signed_in=provider is not None, user=account(request)
        )
        if any(k in params for k in ("sort", "dir", "per")):
            chosen = f"{sort}.{'down' if desc else 'up'}.{'serving' if per_serving else '100g'}"
            response.set_cookie(
                FOODS_COOKIE, chosen, 365 * 86400, path=PREFIX, secure=secure, samesite="lax"
            )
        return response
