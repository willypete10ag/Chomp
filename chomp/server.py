"""Chomp MCP server: MCPServer (the mcp SDK's FastMCP, renamed in v2) on streamable HTTP.

Tool docstrings and argument descriptions are Claude's only documentation for Chomp, so they
say when to use each tool and what it returns.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import closing, contextmanager
from typing import Annotated, Literal, TypedDict

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from chomp import __version__, auth, db, logic, store, text, users, web
from chomp.barcode import lookup as barcode_lookup
from chomp.config import Settings
from chomp.sources import SourceError
from chomp.sources import gtin as gtins
from chomp.sources.fdc import FDCClient
from chomp.sources.off import OFFClient

log = logging.getLogger("chomp")

INSTRUCTIONS = """\
Chomp is the user's meal log. Use its tools to look up foods, log what they ate, and review totals.
Every calorie and macro number you show must come from a Chomp tool result. Never estimate or
invent nutrition values; if a food can't be found, say so and ask for a barcode or label photo,
or, for a restaurant dish or home cooking, the nutrition numbers and serving weight (save_food).
Typical flow: search_food for each item, pick the best match (prefer portion and saved
matches), log_entries with food_ids, amounts and the time they ate (ask if they haven't said;
don't label meals breakfast/lunch/dinner unless they do), then show the user the per-item
breakdown so they can correct it with update_entry.
Amounts: when the user gives a count or household measure ("3 handfuls", "a taco") rather
than grams, use the food's matching portion from search_food if it has one. If it has none,
ask once: "Do you want to give an exact weight, or should I estimate?" To estimate, grams =
the food's serving_g x how many servings you judge it was; log with estimated=true and
save_portion(estimated=true) so you don't ask again for that food. Only the amount is ever
estimated: calories and macros always come from Chomp. When the user gives a real weight,
correct the entry (update_entry) and the portion (save_portion).\
"""

SEARCH_LIMIT_MAX = 25

# Hints shown to clients (e.g. MCP Inspector, Claude's tool permissions).
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
LOOKUP = ToolAnnotations(  # only caches upstream foods
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
ADDS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
CHANGES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
TOTALS_MAX_DAYS = 366


# --- Output models --------------------------------------------------------------------------


MicroName = Literal[logic.MICROS]  # type: ignore[valid-type]


class Nutrients(BaseModel):
    """Calories in kcal, macros in grams, sodium and cholesterol in mg. Optional values are
    null if unknown."""

    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fiber_g: float | None = None
    sugar_g: float | None = None
    sodium_mg: float | None = None
    sat_fat_g: float | None = None
    trans_fat_g: float | None = None
    poly_fat_g: float | None = None
    mono_fat_g: float | None = None
    cholesterol_mg: float | None = None
    added_sugar_g: float | None = None
    sugar_alcohol_g: float | None = None
    micronutrients: dict[MicroName, float] = Field(
        default_factory=dict,
        description="Vitamins and minerals the data reports, unit in the name (vitamin A as "
        "mcg RAE, folate as mcg DFE); unreported ones are left out",
    )
    micronutrients_partial: list[MicroName] = Field(
        default_factory=list,
        description="In a total: vitamins and minerals known for only some of the items, so "
        "their total understates what was eaten",
    )


def _nutrients(values: logic.Nutrients, rows=()) -> Nutrients:
    """Display values; pass the summed rows for a total so partial micronutrients are named."""
    shown = logic.round_for_display(values)
    return Nutrients(
        **{k: shown[k] for k in logic.CORE + logic.OPTIONAL + logic.DETAILS},
        micronutrients={k: shown[k] for k in logic.MICROS if shown[k] is not None},
        micronutrients_partial=logic.partial_micros(rows),
    )


class Food(BaseModel):
    food_id: int
    name: str
    brand: str | None
    source: Literal["fdc", "off", "label", "manual"]
    verified: bool
    per_100g: Nutrients
    serving_g: float | None
    serving_desc: str | None


def _food(row) -> Food:
    return Food(
        food_id=row["id"],
        name=row["name"],
        brand=row["brand"],
        source=row["source"],
        verified=bool(row["verified"]),
        per_100g=_nutrients(store.nutrients_of(row)),
        serving_g=row["serving_g"],
        serving_desc=row["serving_desc"],
    )


class Portion(BaseModel):
    name: str = Field(description="e.g. 'handful'; log it as portion='handful', count=3")
    grams: float = Field(description="Weight of one portion")
    estimated: bool = Field(
        description="True if this weight was estimated, not weighed; entries logged with it "
        "are marked estimated"
    )


def _portion(row) -> Portion:
    return Portion(name=row["name"], grams=row["grams"], estimated=bool(row["estimated"]))


class SearchHit(Food):
    match: Literal["portion", "saved", "fdc"]
    portion: str | None = Field(None, description="The user's portion this phrase named")
    portions: list[Portion] = Field(
        default_factory=list, description="The user's remembered portions of this food"
    )
    fdc_data_type: str | None = None


class SearchResult(BaseModel):
    query: str
    templates: list[str] = Field(
        description="Saved meal templates this phrase names; log one with log_meal_template"
    )
    results: list[SearchHit]
    notes: list[str]


class Entry(BaseModel):
    entry_id: int
    meal_id: int
    food_id: int
    name: str
    brand: str | None
    grams: float
    estimated: bool = Field(description="The grams were estimated from a count, not weighed")
    nutrients: Nutrients


def _entry(e: store.EntryRow) -> Entry:
    return Entry(
        entry_id=e.id,
        meal_id=e.meal_id,
        food_id=e.food_id,
        name=e.name,
        brand=e.brand,
        grams=e.grams,
        estimated=e.estimated,
        nutrients=_nutrients(e.nutrients),
    )


class Meal(BaseModel):
    meal_id: int
    eaten_at: str = Field(description="Local time, ISO 8601 with UTC offset")
    label: str | None
    note: str | None
    entries: list[Entry]
    totals: Nutrients


class TargetProgress(BaseModel):
    target: float
    eaten: float
    remaining: float
    percent: float | None


class DaySummary(BaseModel):
    date: str
    totals: Nutrients
    progress: dict[str, TargetProgress] = Field(
        description="Against daily targets; nutrients without a target are left out"
    )


class Day(DaySummary):
    meals: list[Meal]


class LogResult(BaseModel):
    meal: Meal
    day: DaySummary


class UpdateResult(BaseModel):
    entry: Entry
    meal: Meal
    day: DaySummary


class MealUpdateResult(BaseModel):
    meal: Meal
    day: DaySummary = Field(description="Totals for the day the meal is now on")
    previous_day: DaySummary | None = Field(
        description="Totals for the day it moved from; null if it stayed on the same day"
    )


class DeleteResult(BaseModel):
    deleted_entry: Entry
    meal_deleted: bool
    meal: Meal | None
    day: DaySummary


class BarcodeResult(BaseModel):
    barcode: str = Field(description="Normalized 14-digit GTIN")
    found: bool
    found_in: Literal["saved", "fdc", "off"] | None
    food: Food | None
    warnings: list[str]
    next_step: str | None


class LabelResult(BaseModel):
    food: Food
    barcode_moved_from_food_id: int | None = Field(
        description="A food that previously held this barcode; it keeps its id for past entries"
    )


class ConfirmResult(BaseModel):
    food: Food
    changed: list[str] = Field(description="Nutrients whose per-100 g value changed")


class PortionResult(BaseModel):
    food: Food
    portion: Portion
    portions: list[Portion] = Field(description="All of the user's portions of this food")
    replaced: bool = Field(description="True if a portion by that name was updated")


class TemplateItem(BaseModel):
    food_id: int
    name: str
    grams: float
    nutrients: Nutrients


class TemplateResult(BaseModel):
    template_id: int
    name: str
    replaced: bool
    items: list[TemplateItem]
    totals: Nutrients


class Targets(BaseModel):
    kcal: float | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None


class DayTotals(BaseModel):
    date: str
    totals: Nutrients


class TotalsResult(BaseModel):
    start: str
    end: str
    days: int = Field(description="Calendar days in the range")
    days_logged: int = Field(description="Days with at least one entry")
    totals: Nutrients
    average_per_logged_day: Nutrients | None = Field(
        description="Daily average over days with entries; null if nothing was logged"
    )
    average_per_day: Nutrients = Field(description="Daily average over every calendar day")
    progress: dict[str, TargetProgress] = Field(
        description="Average per logged day against daily targets"
    )
    per_day: list[DayTotals]


class ServerInfo(TypedDict):
    name: str
    version: str
    schema_version: int
    tz: str
    user: str


class LogItem(BaseModel):
    food_id: int = Field(description="food_id from search_food or lookup_barcode")
    grams: float | None = Field(
        None, gt=0, description="Weight eaten in grams; give this or portion"
    )
    portion: str | None = Field(
        None, description="One of the food's portions from search_food, e.g. 'handful'"
    )
    count: float = Field(1, gt=0, description="How many of the portion, e.g. 3 (with portion)")
    estimated: bool = Field(
        False,
        description="True when grams is your estimate (servings x serving_g), not a weight "
        "the user gave",
    )


# --- Helpers --------------------------------------------------------------------------------


@contextmanager
def tool_errors() -> Iterator[None]:
    """Turn expected failures into ToolError so Claude sees the message, not a crash."""
    try:
        yield
    except (store.NotFound, ValueError, SourceError) as e:
        raise ToolError(str(e)) from e


def _amounts(conn, items: list[LogItem]) -> list[tuple[int, float, bool]]:
    """(food_id, grams, estimated) per item, a portion times its count turned into grams."""
    out = []
    for i in items:
        if (i.grams is None) == (i.portion is None):
            raise ValueError(f"food {i.food_id}: give grams or a portion, not both or neither")
        if i.portion is None:
            if i.count != 1:
                raise ValueError(f"food {i.food_id}: count only applies with a portion")
            out.append((i.food_id, i.grams, i.estimated))
        else:
            p = store.get_portion(conn, i.food_id, i.portion)
            out.append((i.food_id, p["grams"] * i.count, i.estimated or bool(p["estimated"])))
    return out


def _progress(totals: logic.Nutrients, targets: dict) -> dict[str, TargetProgress]:
    out = {}
    for k, p in logic.progress(totals, logic.with_default_targets(targets)).items():
        places = logic.DISPLAY_PLACES[k]
        out[k] = TargetProgress(
            target=p["target"],
            eaten=round(p["eaten"], places),
            remaining=round(p["remaining"], places),
            percent=p["percent"],
        )
    return out


# --- Server ---------------------------------------------------------------------------------


NutrientArg = Annotated[float, Field(ge=0, allow_inf_nan=False)]
OptionalNutrientArg = Annotated[float | None, Field(ge=0, allow_inf_nan=False)]

MicrosArg = Annotated[
    dict[MicroName, NutrientArg] | None,
    Field(
        description="Vitamins and minerals as amounts, unit in the name, e.g. "
        '{"vitamin_d_mcg": 2, "iron_mg": 1.8, "potassium_mg": 240}'
    ),
]
MicrosPercentArg = Annotated[
    dict[MicroName, NutrientArg] | None,
    Field(
        description="Vitamins and minerals the label gives only as % Daily Value, e.g. "
        '{"thiamin_mg": 25} for "Thiamin 25%"; converted with FDA Daily Values'
    ),
]


def _micros(amounts: dict | None, percents: dict | None) -> dict[str, float]:
    """Vitamin and mineral values from a label: amounts as given, % DV converted."""
    amounts, percents = dict(amounts or {}), dict(percents or {})
    both = sorted(set(amounts) & set(percents))
    if both:
        raise ValueError(f"{', '.join(both)} given both as an amount and as % DV; give one")
    return {**amounts, **logic.percent_dv_to_amounts(percents)}


ASK_FOR_LABEL = (
    "Not found in Chomp, FoodData Central or Open Food Facts. Ask the user for a photo of "
    "the nutrition facts panel, then call save_food_from_label with this barcode as upc."
)


def build_server(
    settings: Settings, *, fdc: FDCClient | None = None, off: OFFClient | None = None
) -> MCPServer:
    provider = auth.ChompAuthProvider(settings) if auth.check_settings(settings) else None
    server = MCPServer(
        name="chomp",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth=auth.auth_settings(settings) if provider else None,
        auth_server_provider=provider,
    )
    if provider:
        auth.add_consent_route(server, provider)
    fdc = fdc or FDCClient(settings.fdc_api_key)
    off = off or OFFClient(settings.off_user_agent)
    web.add_routes(server, settings, provider, fdc, off)
    expected_version = db.latest_version(settings.migrations_dir)
    tz = settings.tz

    def current_user() -> int:
        """Whose log this call reads and writes: the user the connector signed in as (#46).
        Local-only mode has no sign-in, so it is the owner's."""
        if provider is None:
            return users.OWNER_ID
        token = get_access_token()
        user_id = auth.user_id_of(token.subject if token else None)
        if user_id is None:
            raise ToolError("Not signed in to Chomp; reconnect the connector.")
        return user_id

    def connect():
        return closing(db.connect(settings.db_path, current_user()))

    def meal_out(conn, meal_id: int) -> Meal:
        meal = store.get_meal(conn, meal_id)
        entries = store.meal_entries(conn, meal_id)
        return Meal(
            meal_id=meal["id"],
            eaten_at=logic.local_iso(meal["eaten_at"], tz),
            label=meal["label"],
            note=meal["note"],
            entries=[_entry(e) for e in entries],
            totals=_nutrients(
                logic.sum_nutrients(e.nutrients for e in entries), [e.nutrients for e in entries]
            ),
        )

    def day_summary(conn, utc: str) -> DaySummary:
        day = logic.local_date(utc, tz)
        start, end = logic.local_day_bounds(day, tz)
        rows = [n for _, n in store.entries_between(conn, start, end)]
        totals = logic.sum_nutrients(rows)
        return DaySummary(
            date=day.isoformat(),
            totals=_nutrients(totals, rows),
            progress=_progress(totals, store.get_targets(conn)),
        )

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        # Once public, only the container's own health check (loopback) may ask; the tunnel
        # then exposes nothing but /mcp and its sign-in routes.
        if provider and not auth.is_loopback(request.client.host if request.client else ""):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        try:
            with closing(db.connect(settings.db_path)) as conn:
                version = db.schema_version(conn)
        except Exception:
            log.exception("health check failed")
            return JSONResponse({"status": "error"}, status_code=503)
        if version != expected_version:
            log.error("health check: schema version %d, expected %d", version, expected_version)
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok", "schema_version": version})

    @server.tool(annotations=READ_ONLY)
    def server_info() -> ServerInfo:
        """Report Chomp's version, database schema version, display timezone, and whose
        meal log this connector reads and writes.

        Use this to check that the Chomp connector is reachable, or when the user asks whose
        log they are signed in to. It returns no nutrition data.
        """
        with connect() as conn:
            version = db.schema_version(conn)
            name = users.name_of(conn, store.user_of(conn)) or ""
        return {
            "name": "chomp",
            "version": __version__,
            "schema_version": version,
            "tz": tz,
            "user": name,
        }

    @server.tool(annotations=LOOKUP)
    async def search_food(
        query: Annotated[
            str, Field(min_length=1, description="Food name as the user said it, e.g. 'egg whites'")
        ],
        limit: Annotated[int, Field(ge=1, le=SEARCH_LIMIT_MAX, description="Max results")] = 8,
    ) -> SearchResult:
        """Find foods to log. Call once per item the user mentions, before log_entries.

        Results are ranked: foods with one of the user's portions named by the phrase first
        ("birria taco"), then foods the user has used before (logged, saved from a label or
        barcode, verified first), then USDA FoodData Central (closest name match first,
        generic Foundation and SR Legacy foods before Branded products). Every result has a
        food_id, per-100 g nutrients from the database, serving_g, and the user's remembered
        `portions` (e.g. handful = 30 g) for turning counts into grams. Prefer portion and
        saved matches; prefer generic FDC foods unless the user named a brand. If
        `templates` names a saved meal template, the user means that meal: log it with
        log_meal_template. Show the user which food you picked. If nothing fits, say so and
        ask for a barcode or label photo rather than guessing.
        """
        notes: list[str] = []
        hits: list[SearchHit] = []
        seen: set[int] = set()

        def add(row, **extra) -> None:
            if row["id"] not in seen and len(hits) < limit:
                seen.add(row["id"])
                hits.append(SearchHit(**_food(row).model_dump(), **extra))

        with connect() as conn:
            templates = store.templates_matching(conn, query)
            name = text.most_specific(query, templates)
            if name is not None:
                notes.append(
                    f"The user has a saved meal template named {name!r}: log it with "
                    "log_meal_template rather than searching for its foods."
                )
            elif templates:
                names = ", ".join(repr(t) for t in templates)
                notes.append(
                    f"Several saved meal templates match ({names}); ask the user which one "
                    "they mean, then log it with log_meal_template."
                )
            for row in store.search_portions(conn, query, limit):
                add(row, match="portion", portion=row["portion"])
            for row in store.search_foods(conn, query, limit):
                add(row, match="saved")
            if len(hits) < limit:
                if not fdc.enabled:
                    notes.append("FoodData Central is not configured; showing saved foods only.")
                else:
                    try:
                        records = await fdc.search_ranked(query, limit)
                    except SourceError as e:
                        log.warning("search_food: %s", e)
                        notes.append(f"{e}; showing saved foods only.")
                        records = []
                    for rec in records:
                        try:
                            row, _ = store.cache_record(conn, rec)
                        except ValueError as e:  # one bad upstream record mustn't sink the search
                            log.warning("search_food: skipped FDC %s: %s", rec.source_id, e)
                            continue
                        add(row, match="fdc", fdc_data_type=rec.data_type)
            portions = store.portions_of(conn, (h.food_id for h in hits))
            for h in hits:
                h.portions = [_portion(p) for p in portions[h.food_id]]
        if not hits and not templates:
            notes.append(
                "No match. Try a simpler or more generic name, or ask for a barcode or label photo."
            )
        return SearchResult(query=query, templates=templates, results=hits, notes=notes)

    @server.tool(annotations=ADDS)
    def log_entries(
        items: Annotated[list[LogItem], Field(min_length=1, description="One item per food eaten")],
        meal_label: Annotated[
            str | None,
            Field(
                description="Only a name the user gave (e.g. post-workout); never guess "
                "breakfast/lunch/dinner from the time or order of meals"
            ),
        ] = None,
        eaten_at: Annotated[
            str | None,
            Field(
                description="Required for a new meal: when it was eaten, ISO 8601 (e.g. "
                "2026-10-05T15:00; no offset means the user's local time), or 'now'"
            ),
        ] = None,
        note: Annotated[str | None, Field(description="Free-text note for the meal")] = None,
        meal_id: Annotated[
            int | None,
            Field(
                description="Add the items to this already-logged meal (from log_entries or "
                "get_day) instead of starting a new one"
            ),
        ] = None,
    ) -> LogResult:
        """Log one meal: one or more foods with the grams eaten.

        Use food_ids from search_food or lookup_barcode. Give each item's grams, or one of
        the food's portions with a count (3 handfuls: portion='handful', count=3). For a
        count with no portion, ask whether the user will give an exact weight or wants an
        estimate; an estimate is the food's serving_g x your count of servings, logged with
        estimated=true (then save_portion so you needn't ask next time). Every new meal
        needs eaten_at: if the
        user hasn't said when they ate, ask before logging; pass 'now' only when they say
        they are eating it now. Leave meal_label empty unless the user names the meal.
        To add something to a meal already logged ("add the cereal to breakfast"), pass
        that meal's meal_id. Nutrients are computed from the database and snapshotted, so
        later food edits never change this log. Returns the meal with per-item nutrients and
        totals, plus the day's running totals and progress against targets. Show the user
        the per-item breakdown so they can correct it.
        """
        with tool_errors(), connect() as conn:
            pairs = _amounts(conn, items)
            if meal_id is not None:
                if eaten_at is not None or meal_label is not None or note is not None:
                    raise ValueError(
                        "eaten_at, meal_label and note only apply to a new meal; "
                        "use update_meal to change an existing one"
                    )
                store.add_to_meal(conn, meal_id, pairs)
                when = store.get_meal(conn, meal_id)["eaten_at"]
            else:
                when = logic.parse_eaten_at(eaten_at, tz)
                meal_id = store.log_meal(conn, pairs, when, meal_label, note)
            return LogResult(meal=meal_out(conn, meal_id), day=day_summary(conn, when))

    @server.tool(annotations=CHANGES)
    def update_entry(
        entry_id: Annotated[int, Field(description="entry_id from log_entries or get_day")],
        grams: Annotated[float | None, Field(gt=0, description="Corrected grams")] = None,
        food_id: Annotated[
            int | None, Field(description="Swap to this food (from search_food)")
        ] = None,
        estimated: Annotated[
            bool, Field(description="True if the new grams are still your estimate")
        ] = False,
    ) -> UpdateResult:
        """Correct a logged item: change its grams, swap it to a different food, or both.

        Use when the user corrects something ("the eggs were 220 g", "that was whole milk").
        New grams count as exact, clearing the entry's estimated mark, unless estimated=true.
        If the corrected entry was a counted portion ("those 3 handfuls were 90 g"), also
        save_portion with the weight and count so the portion is right next time.
        Nutrients are recomputed from the food's current values. Returns the updated entry,
        its meal's new totals, and the day's totals.
        """
        if grams is None and food_id is None:
            raise ToolError("give grams, food_id, or both")
        with tool_errors(), connect() as conn:
            entry = store.update_entry(conn, entry_id, grams, food_id, estimated)
            meal = meal_out(conn, entry.meal_id)
            day = day_summary(conn, store.get_meal(conn, entry.meal_id)["eaten_at"])
            return UpdateResult(entry=_entry(entry), meal=meal, day=day)

    @server.tool(annotations=CHANGES)
    def update_meal(
        meal_id: Annotated[int, Field(description="meal_id from log_entries or get_day")],
        eaten_at: Annotated[
            str | None,
            Field(
                description="New time, ISO 8601 (e.g. 2026-10-05T12:30). No offset means the "
                "user's local time."
            ),
        ] = None,
        meal_label: Annotated[
            str | None, Field(description="New label, e.g. lunch; empty string clears it")
        ] = None,
        note: Annotated[str | None, Field(description="New note; empty string clears it")] = None,
    ) -> MealUpdateResult:
        """Move a logged meal to a different time, or change its label or note.

        Use when the user says a meal was logged at the wrong time or under the wrong name
        ("that was lunch, around 12:30", "move breakfast to yesterday"). Its items and
        nutrients stay as they are. Returns the meal, the totals for the day it is now on,
        and the old day's totals if it moved to a different day.
        """
        if eaten_at is None and meal_label is None and note is None:
            raise ToolError("give eaten_at, meal_label, note, or a combination")
        with tool_errors(), connect() as conn:
            before = store.get_meal(conn, meal_id)["eaten_at"]
            when = logic.parse_eaten_at(eaten_at, tz) if eaten_at is not None else None
            after = store.update_meal(conn, meal_id, when, meal_label, note)["eaten_at"]
            moved = logic.local_date(before, tz) != logic.local_date(after, tz)
            return MealUpdateResult(
                meal=meal_out(conn, meal_id),
                day=day_summary(conn, after),
                previous_day=day_summary(conn, before) if moved else None,
            )

    @server.tool(annotations=CHANGES)
    def delete_entry(
        entry_id: Annotated[int, Field(description="entry_id from log_entries or get_day")],
    ) -> DeleteResult:
        """Remove a mistakenly logged item. If it was the meal's last item, the meal goes too.

        Returns what was deleted, the remaining meal (null if deleted), and the day's totals.
        """
        with tool_errors(), connect() as conn:
            eaten_at = store.get_meal(conn, store.get_entry(conn, entry_id).meal_id)["eaten_at"]
            entry, meal_deleted = store.delete_entry(conn, entry_id)
            return DeleteResult(
                deleted_entry=_entry(entry),
                meal_deleted=meal_deleted,
                meal=None if meal_deleted else meal_out(conn, entry.meal_id),
                day=day_summary(conn, eaten_at),
            )

    @server.tool(annotations=READ_ONLY)
    def get_day(
        date: Annotated[
            str | None,
            Field(description="Local date YYYY-MM-DD, or 'today' / 'yesterday'. Omit for today."),
        ] = None,
    ) -> Day:
        """Review one day: every meal with its items, the day's totals, and progress against
        daily targets. Days run midnight to midnight in the user's timezone.

        Use for "what did I eat today", "how much protein do I have left", and to find
        entry_ids for corrections.
        """
        with tool_errors(), connect() as conn:
            day = logic.parse_local_date(date, tz)
            start, end = logic.local_day_bounds(day, tz)
            meals = [meal_out(conn, m["id"]) for m in store.meals_between(conn, start, end)]
            rows = [n for _, n in store.entries_between(conn, start, end)]
            totals = logic.sum_nutrients(rows)
            return Day(
                date=day.isoformat(),
                meals=meals,
                totals=_nutrients(totals, rows),
                progress=_progress(totals, store.get_targets(conn)),
            )

    @server.tool(annotations=LOOKUP)
    async def lookup_barcode(
        barcode: Annotated[
            str, Field(description="UPC/EAN digits read from the package or a barcode photo")
        ],
    ) -> BarcodeResult:
        """Look up a packaged food by its barcode (UPC-A, EAN-13, EAN-8 or GTIN-14).

        Checks foods already saved in Chomp, then USDA FoodData Central Branded foods, then
        Open Food Facts, and saves any hit so the next scan is instant. Returns the food with
        per-100 g nutrients and serving size. Read `warnings` to the user: Open Food Facts
        data is unverified until they confirm it against the label (confirm_food). If
        nothing is found, follow `next_step`: ask for a label photo and call
        save_food_from_label. Never fill in nutrition values yourself.
        """
        try:
            code = gtins.normalize(barcode)
        except gtins.InvalidBarcode as e:
            raise ToolError(f"{e}. Ask the user to re-read the barcode digits.") from e
        with connect() as conn:
            hit = await barcode_lookup(conn, code, fdc, off)
        if hit.row is not None:
            food = _food(hit.row)
            notes = list(hit.flags)
            if not food.verified and food.source == "off" and not notes:
                notes.append("unverified: Open Food Facts data; confirm against the label")
            return BarcodeResult(
                barcode=code,
                found=True,
                found_in=hit.found_in,
                food=food,
                warnings=hit.warnings + notes,
                next_step=None,
            )
        if hit.unreachable:
            next_step = (
                "Barcode sources could not be reached. Try again shortly, or ask for a photo "
                "of the nutrition facts panel and call save_food_from_label."
            )
        else:
            next_step = ASK_FOR_LABEL
        return BarcodeResult(
            barcode=code,
            found=False,
            found_in=None,
            food=None,
            warnings=hit.warnings,
            next_step=next_step,
        )

    @server.tool(annotations=CHANGES)  # may take a barcode from another food
    def save_food_from_label(
        name: Annotated[str, Field(min_length=1, description="Product name")],
        serving_g: Annotated[
            float, Field(gt=0, description="Serving size in grams, from the label")
        ],
        kcal: Annotated[NutrientArg, Field(description="Calories per serving")],
        protein_g: Annotated[NutrientArg, Field(description="Protein per serving, g")],
        carbs_g: Annotated[NutrientArg, Field(description="Total carbohydrate per serving, g")],
        fat_g: Annotated[NutrientArg, Field(description="Total fat per serving, g")],
        fiber_g: Annotated[OptionalNutrientArg, Field(description="Dietary fiber, g")] = None,
        sugar_g: Annotated[OptionalNutrientArg, Field(description="Total sugars, g")] = None,
        sodium_mg: Annotated[OptionalNutrientArg, Field(description="Sodium, mg")] = None,
        sat_fat_g: Annotated[OptionalNutrientArg, Field(description="Saturated fat, g")] = None,
        trans_fat_g: Annotated[OptionalNutrientArg, Field(description="Trans fat, g")] = None,
        poly_fat_g: Annotated[
            OptionalNutrientArg, Field(description="Polyunsaturated fat, g")
        ] = None,
        mono_fat_g: Annotated[
            OptionalNutrientArg, Field(description="Monounsaturated fat, g")
        ] = None,
        cholesterol_mg: Annotated[OptionalNutrientArg, Field(description="Cholesterol, mg")] = None,
        added_sugar_g: Annotated[
            OptionalNutrientArg, Field(description="Added sugars ('Includes Xg Added Sugars'), g")
        ] = None,
        sugar_alcohol_g: Annotated[
            OptionalNutrientArg, Field(description="Sugar alcohols, g")
        ] = None,
        micronutrients: MicrosArg = None,
        micronutrients_percent_dv: MicrosPercentArg = None,
        brand: Annotated[str | None, Field(description="Brand name")] = None,
        upc: Annotated[
            str | None, Field(description="Barcode digits, if known, so scans find it")
        ] = None,
        serving_desc: Annotated[
            str | None, Field(description="Serving as printed, e.g. '3 tbsp (46 g)'")
        ] = None,
    ) -> LabelResult:
        """Save a food from its nutrition facts label, as read from a photo the user sent.

        Use after lookup_barcode finds nothing, or when the user wants their package's exact
        label values. If no barcode is known, ask the user for the digits under the barcode
        and pass them as upc so future scans find this food. Enter the per-serving numbers
        exactly as printed, including the fat breakdown, cholesterol, added sugars and sugar
        alcohols when listed; Chomp converts them to per 100 g. Include every vitamin and
        mineral the label lists: as an amount when printed (e.g. "Vitamin D 2mcg 10%" is
        vitamin_d_mcg 2), otherwise as % DV. The food is saved as verified
        (it is the user's own label). Returns the saved food with a food_id
        ready for log_entries. Only use numbers read from the label, never estimates.
        """
        with tool_errors(), connect() as conn:
            gtin = gtins.normalize(upc) if upc else None
            per_serving = {
                "kcal": kcal,
                "protein_g": protein_g,
                "carbs_g": carbs_g,
                "fat_g": fat_g,
                "fiber_g": fiber_g,
                "sugar_g": sugar_g,
                "sodium_mg": sodium_mg,
                "sat_fat_g": sat_fat_g,
                "trans_fat_g": trans_fat_g,
                "poly_fat_g": poly_fat_g,
                "mono_fat_g": mono_fat_g,
                "cholesterol_mg": cholesterol_mg,
                "added_sugar_g": added_sugar_g,
                "sugar_alcohol_g": sugar_alcohol_g,
                **_micros(micronutrients, micronutrients_percent_dv),
            }
            row, moved_from = store.save_label_food(
                conn,
                name=name.strip(),
                brand=(brand or "").strip() or None,
                gtin=gtin,
                per_100g=logic.per_serving_to_per_100g(per_serving, serving_g),
                serving_g=serving_g,
                serving_desc=serving_desc,
            )
            return LabelResult(food=_food(row), barcode_moved_from_food_id=moved_from)

    @server.tool(annotations=ADDS)
    def save_food(
        name: Annotated[
            str, Field(min_length=1, description="What the food is, e.g. 'Chipotle chicken bowl'")
        ],
        serving_g: Annotated[
            float,
            Field(gt=0, description="Weight of one serving in grams, as published or weighed"),
        ],
        kcal: Annotated[NutrientArg, Field(description="Calories per serving")],
        protein_g: Annotated[NutrientArg, Field(description="Protein per serving, g")],
        carbs_g: Annotated[NutrientArg, Field(description="Total carbohydrate per serving, g")],
        fat_g: Annotated[NutrientArg, Field(description="Total fat per serving, g")],
        confirmed: Annotated[
            bool,
            Field(
                description="True when the user approves these numbers as right; otherwise "
                "the food is saved unconfirmed (amber on the dashboard)"
            ),
        ] = False,
        fiber_g: Annotated[OptionalNutrientArg, Field(description="Dietary fiber, g")] = None,
        sugar_g: Annotated[OptionalNutrientArg, Field(description="Total sugars, g")] = None,
        sodium_mg: Annotated[OptionalNutrientArg, Field(description="Sodium, mg")] = None,
        sat_fat_g: Annotated[OptionalNutrientArg, Field(description="Saturated fat, g")] = None,
        trans_fat_g: Annotated[OptionalNutrientArg, Field(description="Trans fat, g")] = None,
        cholesterol_mg: Annotated[OptionalNutrientArg, Field(description="Cholesterol, mg")] = None,
        added_sugar_g: Annotated[OptionalNutrientArg, Field(description="Added sugars, g")] = None,
        brand: Annotated[
            str | None, Field(description="Restaurant or maker, if any (e.g. 'Chipotle')")
        ] = None,
        serving_desc: Annotated[
            str | None, Field(description="The serving in words, e.g. '1 bowl' or '1 cup'")
        ] = None,
    ) -> Food:
        """Save a food that has no package label or barcode: a restaurant dish from the
        restaurant's published nutrition information, or a home-cooked dish or recipe from
        numbers the user gives (for a whole recipe, its totals divided into servings by the
        user, with the weight of one serving).

        Use when search_food and lookup_barcode don't have the food. Every number must come
        from the user or from nutrition information they point you to, never estimates; if
        the serving weight in grams isn't published, ask the user for it. Pass confirmed=True
        only when the user says the numbers are right. Returns the saved food with a food_id
        ready for log_entries; to approve it later, use confirm_food.
        """
        with tool_errors(), connect() as conn:
            if not name.strip():
                raise ValueError("a food needs a name")
            per_serving = {
                "kcal": kcal,
                "protein_g": protein_g,
                "carbs_g": carbs_g,
                "fat_g": fat_g,
                "fiber_g": fiber_g,
                "sugar_g": sugar_g,
                "sodium_mg": sodium_mg,
                "sat_fat_g": sat_fat_g,
                "trans_fat_g": trans_fat_g,
                "cholesterol_mg": cholesterol_mg,
                "added_sugar_g": added_sugar_g,
            }
            row, _ = store.save_label_food(
                conn,
                name=name.strip(),
                brand=(brand or "").strip() or None,
                gtin=None,
                per_100g=logic.per_serving_to_per_100g(per_serving, serving_g),
                serving_g=serving_g,
                serving_desc=(serving_desc or "").strip() or None,
                source="manual",
                verified=confirmed,
            )
            return _food(row)

    @server.tool(annotations=CHANGES)
    def confirm_food(
        food_id: Annotated[int, Field(description="The food to confirm")],
        confirmed: Annotated[
            bool,
            Field(
                description="False marks the food unconfirmed again (the user takes back "
                "their approval)"
            ),
        ] = True,
        kcal: OptionalNutrientArg = None,
        protein_g: OptionalNutrientArg = None,
        carbs_g: OptionalNutrientArg = None,
        fat_g: OptionalNutrientArg = None,
        fiber_g: OptionalNutrientArg = None,
        sugar_g: OptionalNutrientArg = None,
        sodium_mg: OptionalNutrientArg = None,
        sat_fat_g: OptionalNutrientArg = None,
        trans_fat_g: OptionalNutrientArg = None,
        poly_fat_g: OptionalNutrientArg = None,
        mono_fat_g: OptionalNutrientArg = None,
        cholesterol_mg: OptionalNutrientArg = None,
        added_sugar_g: OptionalNutrientArg = None,
        sugar_alcohol_g: OptionalNutrientArg = None,
        micronutrients: MicrosArg = None,
        micronutrients_percent_dv: MicrosPercentArg = None,
        per_serving_g: Annotated[
            float | None,
            Field(
                gt=0,
                description="If the corrected values are per serving (as on a label), the "
                "serving size in grams. Omit when the values are per 100 g.",
            ),
        ] = None,
    ) -> ConfirmResult:
        """Mark a food as confirmed (verified) once the user approves its numbers, optionally
        correcting nutrient values.

        Use when the user approves or confirms a food, whatever it is: an Open Food Facts
        hit checked against the package, a restaurant dish, a home-cooked food or anything
        else they vouch for. Also use when they report that a value is wrong, or to add
        vitamins and minerals from the label to a food that lacks them (% DV figures need
        per_serving_g). Only pass nutrients that need correcting or adding; the rest are
        kept. Corrections apply to future logs only; past entries keep their values (use
        update_entry to re-snapshot one). To take back an approval, pass confirmed=False.
        Returns the food and which nutrients changed.
        """
        given = {
            k: v
            for k, v in {
                "kcal": kcal,
                "protein_g": protein_g,
                "carbs_g": carbs_g,
                "fat_g": fat_g,
                "fiber_g": fiber_g,
                "sugar_g": sugar_g,
                "sodium_mg": sodium_mg,
                "sat_fat_g": sat_fat_g,
                "trans_fat_g": trans_fat_g,
                "poly_fat_g": poly_fat_g,
                "mono_fat_g": mono_fat_g,
                "cholesterol_mg": cholesterol_mg,
                "added_sugar_g": added_sugar_g,
                "sugar_alcohol_g": sugar_alcohol_g,
            }.items()
            if v is not None
        }
        with tool_errors(), connect() as conn:
            if micronutrients_percent_dv and per_serving_g is None:
                # % DV is always per serving, so it can't be read as per 100 g
                raise ValueError(
                    "micronutrients_percent_dv is per serving: pass per_serving_g with the "
                    "label's serving size in grams"
                )
            given |= _micros(micronutrients, micronutrients_percent_dv)
            if per_serving_g is not None:
                if not given:
                    raise ValueError("per_serving_g only applies when correcting values")
                given = logic.partial_per_serving_to_per_100g(given, per_serving_g)
            row, changed = store.confirm_food(conn, food_id, given, confirmed=confirmed)
            return ConfirmResult(food=_food(row), changed=changed)

    @server.tool(annotations=CHANGES)
    def save_portion(
        food_id: Annotated[int, Field(description="The food, from search_food")],
        name: Annotated[
            str, Field(min_length=1, description="The portion, e.g. 'handful', 'slice', 'taco'")
        ],
        grams: Annotated[
            float, Field(gt=0, description="Total weight of `count` portions, in grams")
        ],
        count: Annotated[
            float, Field(gt=0, description="How many portions `grams` covers, e.g. 3")
        ] = 1,
        estimated: Annotated[
            bool,
            Field(
                description="True when grams is your estimate rather than a weight the user gave"
            ),
        ] = False,
    ) -> PortionResult:
        """Remember what one of the user's portions of a food weighs ("my handful of
        Cheez-Its is 30 g"), so counts turn into grams without asking next time.

        Use when the user gives a real weight for a count ("those 3 handfuls were 90 g":
        grams=90, count=3), states a portion ("a scoop is 32 g"), or after you log an
        estimate (estimated=true) so you don't ask again. A real weight replaces an
        estimate. Portions belong to this user and show up in search_food results; log one
        with log_entries(portion=..., count=...). Returns the portion and all the user's
        portions of the food.
        """
        with tool_errors(), connect() as conn:
            row, replaced = store.save_portion(conn, food_id, name, grams / count, estimated)
            portions = store.portions_of(conn, [food_id])[food_id]
            return PortionResult(
                food=_food(store.get_food(conn, food_id)),
                portion=_portion(row),
                portions=[_portion(p) for p in portions],
                replaced=replaced,
            )

    @server.tool(annotations=CHANGES)
    def forget_portion(
        food_id: Annotated[int, Field(description="The food, from search_food")],
        name: Annotated[str, Field(min_length=1, description="The portion's name")],
    ) -> PortionResult:
        """Forget one of the user's portions of a food, when they say it's wrong or unwanted.

        Past entries keep their grams. Returns the forgotten portion and the ones left.
        """
        with tool_errors(), connect() as conn:
            row = store.delete_portion(conn, food_id, name)
            portions = store.portions_of(conn, [food_id])[food_id]
            return PortionResult(
                food=_food(store.get_food(conn, food_id)),
                portion=_portion(row),
                portions=[_portion(p) for p in portions],
                replaced=False,
            )

    def template_out(conn, template_id: int, name: str, replaced: bool, items) -> TemplateResult:
        foods = store.get_foods(conn, (i["food_id"] for i in items))
        rows = [
            TemplateItem(
                food_id=i["food_id"],
                name=foods[i["food_id"]]["name"],
                grams=i["grams"],
                nutrients=_nutrients(
                    logic.snapshot(store.nutrients_of(foods[i["food_id"]]), i["grams"])
                ),
            )
            for i in items
        ]
        snaps = [logic.snapshot(store.nutrients_of(foods[i["food_id"]]), i["grams"]) for i in items]
        totals = logic.sum_nutrients(snaps)
        return TemplateResult(
            template_id=template_id,
            name=name,
            replaced=replaced,
            items=rows,
            totals=_nutrients(totals, snaps),
        )

    @server.tool(annotations=CHANGES)
    def save_meal_template(
        name: Annotated[str, Field(min_length=1, description="Short name, e.g. 'usual breakfast'")],
        items: Annotated[list[LogItem], Field(min_length=1, description="Foods and grams")],
    ) -> TemplateResult:
        """Save a repeat meal under a name so it can be logged in one step later.

        Use when the user says "save this as my usual breakfast" (take the items from the
        meal just logged) or describes a meal they eat often. Saving an existing name
        replaces it. Returns the template's items with nutrients at current food values.
        """
        name = name.strip()
        with tool_errors(), connect() as conn:
            pairs = [(f, g) for f, g, _ in _amounts(conn, items)]
            template_id, replaced = store.save_template(conn, name, pairs)
            row, rows = store.get_template(conn, name)
            return template_out(conn, template_id, row["name"], replaced, rows)

    @server.tool(annotations=ADDS)
    def log_meal_template(
        name: Annotated[str, Field(min_length=1, description="Template name, any case")],
        eaten_at: Annotated[
            str,
            Field(
                description="When it was eaten, ISO 8601 (no offset means local time), or "
                "'now'. Ask the user if they haven't said."
            ),
        ],
        meal_label: Annotated[
            str | None, Field(description="Defaults to the template name")
        ] = None,
    ) -> LogResult:
        """Log a saved meal template as a new meal in one call ("log my usual breakfast").

        Logs every item at its saved grams; correct single items afterwards with
        update_entry. If the name is unknown, the error lists the saved templates. Returns
        the same as log_entries.
        """
        with tool_errors(), connect() as conn:
            template, items = store.get_template(conn, name.strip())
            when = logic.parse_eaten_at(eaten_at, tz)
            meal_id = store.log_meal(
                conn,
                [(i["food_id"], i["grams"]) for i in items],
                when,
                meal_label or template["name"],
            )
            return LogResult(meal=meal_out(conn, meal_id), day=day_summary(conn, when))

    @server.tool(annotations=READ_ONLY)
    def get_totals(
        start: Annotated[str, Field(description="First local date, YYYY-MM-DD")],
        end: Annotated[
            str | None, Field(description="Last local date, inclusive. Omit for today.")
        ] = None,
    ) -> TotalsResult:
        """Totals and daily averages over a date range, with progress against daily targets.

        Use for weekly or monthly reviews ("how was my protein this week?"). For the last
        7 days, start is 6 days before today. Averages are given both per logged day and
        per calendar day; progress compares the per-logged-day average with targets. For
        a single day's meals use get_day.
        """
        with tool_errors(), connect() as conn:
            first = logic.parse_local_date(start, tz)
            last = logic.parse_local_date(end, tz)
            days = logic.date_range(first, last)
            if len(days) > TOTALS_MAX_DAYS:
                raise ValueError(f"range is {len(days)} days; the maximum is {TOTALS_MAX_DAYS}")
            lo, _ = logic.local_day_bounds(first, tz)
            _, hi = logic.local_day_bounds(last, tz)
            entries = store.entries_between(conn, lo, hi)
            rows = [n for _, n in entries]
            day_totals = logic.totals_by_day(entries, tz)
            day_rows: dict = {}
            for eaten_at, n in entries:
                day_rows.setdefault(logic.local_date(eaten_at, tz), []).append(n)
            totals = logic.sum_nutrients(day_totals.values())
            per_logged = logic.average(totals, len(day_totals)) if day_totals else None
            targets = store.get_targets(conn)
            return TotalsResult(
                start=first.isoformat(),
                end=last.isoformat(),
                days=len(days),
                days_logged=len(day_totals),
                totals=_nutrients(totals, rows),
                average_per_logged_day=_nutrients(per_logged, rows) if per_logged else None,
                average_per_day=_nutrients(logic.average(totals, len(days)), rows),
                progress=_progress(per_logged, targets) if per_logged else {},
                per_day=[
                    DayTotals(
                        date=d.isoformat(),
                        totals=_nutrients(
                            day_totals.get(d) or logic.sum_nutrients([]), day_rows.get(d, [])
                        ),
                    )
                    for d in days
                ],
            )

    @server.tool(annotations=CHANGES)
    def set_targets(
        kcal: Annotated[float | None, Field(gt=0, description="Daily calories")] = None,
        protein_g: Annotated[float | None, Field(gt=0, description="Daily protein, g")] = None,
        carbs_g: Annotated[float | None, Field(gt=0, description="Daily carbs, g")] = None,
        fat_g: Annotated[float | None, Field(gt=0, description="Daily fat, g")] = None,
        clear: Annotated[
            list[Literal["kcal", "protein_g", "carbs_g", "fat_g"]] | None,
            Field(description="Targets to remove"),
        ] = None,
    ) -> Targets:
        """Set or change the user's daily targets. Only the targets given change; use clear
        to remove one. A target that isn't set counts as the default in progress: 2,000 kcal,
        and a third of the calorie goal each for protein, carbs and fat.

        Use when the user states a goal ("my protein target is 180 g"). get_day,
        get_totals and log results then report progress against these. Returns all
        current targets.
        """
        changes: dict[str, float | None] = {
            k: v
            for k, v in {
                "kcal": kcal,
                "protein_g": protein_g,
                "carbs_g": carbs_g,
                "fat_g": fat_g,
            }.items()
            if v is not None
        }
        for k in clear or ():
            if k in changes:
                raise ToolError(f"{k} is both set and cleared")
            changes[k] = None
        if not changes:
            raise ToolError("give at least one target to set or clear")
        with connect() as conn:
            return Targets(**store.set_targets(conn, changes))

    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request URLs at INFO; keep upstream request details out of the log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()
    auth.check_settings(settings)  # fail on bad auth config before touching the database
    with closing(db.connect(settings.db_path)) as conn:
        version = db.migrate(conn, settings.schema_path, settings.migrations_dir)
        if settings.public_url and (name := users.password_clash(conn, settings.auth_password)):
            raise SystemExit(
                f"AUTH_PASSWORD is also {name}'s password; change one of them"
                f" (chomp user password {name})"
            )
    log.info("database ready at schema version %d", version)
    if not settings.fdc_api_key:
        log.warning("FDC_API_KEY is not set; search_food will only find saved foods")
    server = build_server(settings)
    if settings.public_url:
        log.info("auth on: OAuth sign-in%s", " plus bearer token" if settings.bearer_token else "")
    else:
        log.info("local-only mode: no auth, listening on %s only", settings.host)
    server.run(
        "streamable-http",
        host=settings.host,
        port=settings.port,
        transport_security=auth.transport_security(settings),
    )


if __name__ == "__main__":
    main()
