"""Web dashboard at /app: today's meals against targets, history charts, and saved foods.

Server-rendered HTML on the same Starlette app as /mcp, with no JavaScript: charts are
inline SVG and edits are plain form posts. Phones can add it to the home screen (web app
manifest at /app/manifest.webmanifest).

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
import secrets
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from chomp import db, logic, store
from chomp.auth import ChompAuthProvider
from chomp.config import Settings

log = logging.getLogger("chomp.web")

PREFIX = "/app"
COOKIE = "chomp_session"
SESSION_TTL_S = 30 * 86400
SUBJECT = "owner"
HISTORY_RANGES = (7, 30, 90)
FOODS_LIMIT = 200
MAX_GRAMS = 10_000
MIN_YEAR, MAX_YEAR = 1900, 2999
STATIC_DIR = Path(__file__).resolve().parent / "static"
ICONS = {
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
}
THEME = "#1f7a4d"

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self'; manifest-src 'self';"
        " form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

NUTRIENT_LABELS = {"kcal": "Calories", "protein_g": "Protein", "carbs_g": "Carbs", "fat_g": "Fat"}
UNITS = {"kcal": "kcal", "protein_g": "g", "carbs_g": "g", "fat_g": "g"}
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
    unit = UNITS.get(key, "")
    places = logic.DISPLAY_PLACES[key]
    return f"{num(value, places)} {unit}" if key == "kcal" else f"{num(value, places)}{unit}"


def clock(local_iso: str) -> str:
    dt = datetime.fromisoformat(local_iso)
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def day_title(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    fmt = f"%a, %b {day.day}" + (", %Y" if day.year != today.year else "")
    return day.strftime(fmt)


def macros_line(n: logic.Nutrients) -> str:
    return (
        f'<span class="p">P {num(n["protein_g"], 1)}</span>'
        f' <span class="c">C {num(n["carbs_g"], 1)}</span>'
        f' <span class="f">F {num(n["fat_g"], 1)}</span>'
    )


def meter(percent: float | None, kind: str) -> str:
    width = 0.0 if percent is None else max(0.0, min(percent, 100.0))
    over = " over" if percent is not None and percent > 100 else ""
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
<link rel="stylesheet" href="{p}/static/app.css">
<title>{title}</title></head><body>"""

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
        HEAD.format(theme=THEME, p=PREFIX, title=e(f"{title} · Chomp"))
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
    return f" · {num(grams, 1)} g" if grams else ""


def parse_grams(value: str) -> float:
    try:
        grams = float(value)
    except ValueError:
        raise ValueError("Enter the grams as a number, like 150.") from None
    if not math.isfinite(grams) or grams <= 0 or grams > MAX_GRAMS:
        raise ValueError(f"Grams must be more than 0 and at most {MAX_GRAMS:,}.")
    return grams


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


def add_routes(server: MCPServer, settings: Settings, provider: ChompAuthProvider | None) -> None:
    """Register the dashboard. provider is None in local-only mode (no sign-in)."""
    tz = settings.tz
    sessions = Sessions(settings.db_path)
    secure = bool(settings.public_url)
    css = (STATIC_DIR / "app.css").read_text()

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

    def back_to_day(day: date, anchor: str = "") -> RedirectResponse:
        url = f"{PREFIX}?date={day.isoformat()}" + (f"#{anchor}" if anchor else "")
        return RedirectResponse(url, status_code=303, headers=SECURITY_HEADERS)

    # --- static files (no sign-in: the home-screen installer fetches these) ---

    @server.custom_route(f"{PREFIX}/static/app.css", methods=["GET"])
    async def stylesheet(request: Request) -> Response:
        return Response(css, media_type="text/css", headers={"Cache-Control": "max-age=3600"})

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

    @server.custom_route(PREFIX, methods=["GET"])
    async def day_view(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        now = today()
        try:
            day = logic.parse_local_date(request.query_params.get("date"), tz)
            if not MIN_YEAR <= day.year <= MAX_YEAR:  # keeps day ± 1 and its bounds in range
                raise ValueError(day)
        except ValueError:
            return message("Unknown date", "Dates look like 2026-10-06.", 400, signed_in=True)
        start, end = logic.local_day_bounds(day, tz)
        with connect() as conn:
            meals = [
                (m, store.meal_entries(conn, m["id"]))
                for m in store.meals_between(conn, start, end)
            ]
            targets = store.get_targets(conn)
        totals = logic.sum_nutrients(x.nutrients for _, entries in meals for x in entries)
        progress = logic.progress(totals, targets)
        shown = logic.round_for_display(totals)

        prev_day, next_day = day - timedelta(days=1), day + timedelta(days=1)
        head = [
            '<header class="daynav">',
            f'<a class="step" href="{PREFIX}?date={prev_day.isoformat()}" aria-label="Previous'
            ' day">&lsaquo;</a>',
            f"<h1>{e(day_title(day, now))}</h1>",
            f'<a class="step" href="{PREFIX}?date={next_day.isoformat()}" aria-label="Next'
            ' day">&rsaquo;</a>'
            if day < now
            else '<span class="step"></span>',
            "</header>",
        ]
        if day != now:
            head.append(f'<p class="center"><a href="{PREFIX}">Jump to today</a></p>')

        rows = []
        for key in logic.CORE:
            p = progress.get(key)
            eaten = amount(key, shown[key])
            if p:
                left = p["remaining"]
                note = (
                    f"{amount(key, abs(round(left, logic.DISPLAY_PLACES[key])))}"
                    f" {'left' if left >= 0 else 'over'}"
                )
                value = f"{eaten} <span class='muted'>/ {amount(key, p['target'])}</span>"
                rows.append(
                    f'<div class="stat {key}"><div class="row"><span>{NUTRIENT_LABELS[key]}'
                    f"</span><span>{value}</span></div>{meter(p['percent'], key)}"
                    f'<div class="muted small">{note}</div></div>'
                )
            else:
                rows.append(
                    f'<div class="stat {key}"><div class="row"><span>{NUTRIENT_LABELS[key]}'
                    f"</span><span>{eaten}</span></div></div>"
                )
        summary = f'<section class="card summary">{"".join(rows)}'
        if not any(progress.values()):
            summary += (
                '<p class="muted small">No daily targets yet. Tell Claude something like'
                " “my protein target is 180 g”.</p>"
            )
        summary += "</section>"

        cards = []
        for meal, entries in meals:
            meal_totals = logic.round_for_display(logic.sum_nutrients(x.nutrients for x in entries))
            items = []
            for x in entries:
                n = logic.round_for_display(x.nutrients)
                brand = f' <span class="muted">{e(x.brand)}</span>' if x.brand else ""
                items.append(
                    f'<li id="entry-{x.id}"><div class="row"><span class="name">{e(x.name)}'
                    f'{brand}</span><span class="kcal">{num(n["kcal"])}</span></div>'
                    f'<div class="row small muted"><span>{num(x.grams, 1)} g</span>'
                    f"<span>{macros_line(n)}</span></div>"
                    f"<details><summary>Edit</summary>"
                    f'<form class="inline" method="post" action="{PREFIX}/entries/{x.id}">'
                    f'<label>Grams <input name="grams" inputmode="decimal" required'
                    f' value="{num(x.grams, 1).replace(",", "")}"></label>'
                    f'<button type="submit">Save</button></form>'
                    f'<form method="post" action="{PREFIX}/entries/{x.id}/delete">'
                    f'<button class="danger" type="submit">Delete {e(x.name)}</button></form>'
                    f"</details></li>"
                )
            label = meal["label"] or "Meal"
            when = clock(logic.local_iso(meal["eaten_at"], tz))
            note = f'<p class="muted small">{e(meal["note"])}</p>' if meal["note"] else ""
            cards.append(
                f'<section class="card meal" id="meal-{meal["id"]}"><div class="row mealhead">'
                f"<h2>{e(label.capitalize())} <span class='muted small'>{when}</span></h2>"
                f'<span class="kcal">{num(meal_totals["kcal"])} kcal</span></div>{note}'
                f'<ul class="entries">{"".join(items)}</ul>'
                f'<div class="row small muted total"><span>Meal total</span>'
                f"<span>{macros_line(meal_totals)}</span></div></section>"
            )
        if not cards:
            cards.append(
                '<section class="card empty"><p>Nothing logged on this day.</p>'
                '<p class="muted small">Tell Claude what you ate and it shows up here.</p>'
                "</section>"
            )
        return render(
            day_title(day, now),
            "".join(head) + summary + "".join(cards),
            "today",
            signed_in=provider is not None,
        )

    # --- Entry edits ---

    @server.custom_route(PREFIX + "/entries/{entry_id:int}", methods=["POST"])
    async def edit_entry(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        if not same_origin(request):
            return message("Not allowed", "Edit entries from the dashboard.", 403)
        form = await request.form()
        try:
            grams = parse_grams(str(form.get("grams", "")).replace(",", "").strip())
        except ValueError as err:
            return message("Couldn't save", str(err), 400, signed_in=True)
        with connect() as conn:
            try:
                entry = store.update_entry(conn, request.path_params["entry_id"], grams, None)
            except store.NotFound:
                return message("Not found", "That entry no longer exists.", 404, signed_in=True)
            eaten_at = store.get_meal(conn, entry.meal_id)["eaten_at"]
        log.info("dashboard: updated entry %d", entry.id)
        return back_to_day(logic.local_date(eaten_at, tz), f"entry-{entry.id}")

    @server.custom_route(PREFIX + "/entries/{entry_id:int}/delete", methods=["POST"])
    async def remove_entry(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        if not same_origin(request):
            return message("Not allowed", "Delete entries from the dashboard.", 403)
        with connect() as conn:
            try:
                entry = store.get_entry(conn, request.path_params["entry_id"])
                eaten_at = store.get_meal(conn, entry.meal_id)["eaten_at"]
                store.delete_entry(conn, entry.id)
            except store.NotFound:
                return message("Not found", "That entry no longer exists.", 404, signed_in=True)
        log.info("dashboard: deleted entry %d", entry.id)
        return back_to_day(logic.local_date(eaten_at, tz))

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

    # --- Foods, aliases and templates ---

    @server.custom_route(f"{PREFIX}/foods", methods=["GET"])
    async def foods(request: Request) -> Response:
        if not signed_in(request):
            return to_login(request)
        q = request.query_params.get("q", "").strip()[:100]
        with connect() as conn:
            rows = (
                store.search_foods(conn, q, FOODS_LIMIT)
                if q
                else store.list_used_foods(conn, FOODS_LIMIT)
            )
            aliases = store.list_aliases(conn)
            templates = store.list_templates(conn)

        parts = [
            "<h1>Foods</h1>",
            f'<form class="search" method="get" action="{PREFIX}/foods">'
            f'<input type="search" name="q" value="{e(q)}" placeholder="Search your foods"'
            ' aria-label="Search your foods"></form>',
        ]
        items = []
        for row in rows:
            n = logic.round_for_display(store.nutrients_of(row))
            badges = f'<span class="badge">{SOURCES[row["source"]]}</span>'
            if row["verified"]:
                badges += ' <span class="badge ok">Verified</span>'
            serving = (
                f" · serving {e(row['serving_desc'] or num(row['serving_g'], 1) + ' g')}"
                if row["serving_g"]
                else ""
            )
            brand = f' <span class="muted">{e(row["brand"])}</span>' if row["brand"] else ""
            items.append(
                f'<li><div class="row"><span class="name">{e(row["name"])}{brand}</span>'
                f'<span class="kcal">{num(n["kcal"])}</span></div>'
                f'<div class="row small muted"><span>{badges}</span>'
                f"<span>{macros_line(n)}</span></div>"
                f'<div class="small muted">per 100 g{serving}</div></li>'
            )
        if items:
            heading = f"Matches for “{e(q)}”" if q else "Your foods"
            parts.append(
                f'<section class="card"><h2>{heading}</h2><p class="muted small">kcal'
                f' per 100 g</p><ul class="entries">{"".join(items)}</ul></section>'
            )
        else:
            text = f"No saved foods match “{e(q)}”." if q else "No saved foods yet."
            parts.append(f'<section class="card empty"><p>{text}</p></section>')

        if aliases and not q:
            lis = "".join(
                f'<li class="row"><span>“{e(a["alias"])}”</span><span class="muted">'
                f"{e(a['name'])}{grams_note(a['default_grams'])}</span></li>"
                for a in aliases
            )
            parts.append(
                f'<section class="card"><h2>Aliases</h2><ul class="plain">{lis}</ul></section>'
            )
        if templates and not q:
            cards = []
            for t, t_items in templates:
                snaps = [logic.snapshot(store.nutrients_of(i), i["grams"]) for i in t_items]
                t_totals = logic.round_for_display(logic.sum_nutrients(snaps))
                lis = "".join(
                    f'<li class="row small"><span>{e(i["name"])}</span>'
                    f'<span class="muted">{num(i["grams"], 1)} g</span></li>'
                    for i in t_items
                )
                cards.append(
                    f'<div class="template"><div class="row"><strong>{e(t["name"])}</strong>'
                    f'<span class="kcal">{num(t_totals["kcal"])} kcal</span></div>'
                    f'<ul class="plain">{lis}</ul><div class="small muted">'
                    f"{macros_line(t_totals)}</div></div>"
                )
            parts.append(f'<section class="card"><h2>Meal templates</h2>{"".join(cards)}</section>')
        return render("Foods", "".join(parts), "foods", signed_in=provider is not None)
