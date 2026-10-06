"""Chomp MCP server: MCPServer (the mcp SDK's FastMCP, renamed in v2) on streamable HTTP.

Tool docstrings and argument descriptions are Claude's only documentation for Chomp, so they
say when to use each tool and what it returns.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import closing, contextmanager
from typing import Annotated, Literal, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from chomp import __version__, db, logic, store
from chomp.config import Settings
from chomp.sources import SourceError
from chomp.sources.fdc import FDCClient

log = logging.getLogger("chomp")

INSTRUCTIONS = """\
Chomp is the user's meal log. Use its tools to look up foods, log what they ate, and review totals.
Every calorie and macro number you show must come from a Chomp tool result. Never estimate or
invent nutrition values; if a food can't be found, say so and ask for a barcode or label photo.
Typical flow: search_food for each item, pick the best match (prefer alias and saved matches),
log_entries with food_ids and grams, then show the user the per-item breakdown so they can
correct it with update_entry.\
"""

SEARCH_LIMIT_MAX = 25


# --- Output models --------------------------------------------------------------------------


class Nutrients(BaseModel):
    """Calories in kcal, macros in grams, sodium in mg. Optional values are null if unknown."""

    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fiber_g: float | None = None
    sugar_g: float | None = None
    sodium_mg: float | None = None


def _nutrients(values: logic.Nutrients) -> Nutrients:
    return Nutrients(**logic.round_for_display(values))


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


class SearchHit(Food):
    match: Literal["alias", "saved", "fdc"]
    alias: str | None = None
    default_grams: float | None = None
    fdc_data_type: str | None = None


class SearchResult(BaseModel):
    query: str
    results: list[SearchHit]
    notes: list[str]


class Entry(BaseModel):
    entry_id: int
    meal_id: int
    food_id: int
    name: str
    brand: str | None
    grams: float
    nutrients: Nutrients


def _entry(e: store.EntryRow) -> Entry:
    return Entry(
        entry_id=e.id,
        meal_id=e.meal_id,
        food_id=e.food_id,
        name=e.name,
        brand=e.brand,
        grams=e.grams,
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


class DeleteResult(BaseModel):
    deleted_entry: Entry
    meal_deleted: bool
    meal: Meal | None
    day: DaySummary


class ServerInfo(TypedDict):
    name: str
    version: str
    schema_version: int
    tz: str


class LogItem(BaseModel):
    food_id: int = Field(description="food_id from search_food or lookup_barcode")
    grams: float = Field(gt=0, description="Weight eaten in grams")


# --- Helpers --------------------------------------------------------------------------------


@contextmanager
def tool_errors() -> Iterator[None]:
    """Turn expected failures into ToolError so Claude sees the message, not a crash."""
    try:
        yield
    except (store.NotFound, ValueError, SourceError) as e:
        raise ToolError(str(e)) from e


def _progress(totals: logic.Nutrients, targets: dict) -> dict[str, TargetProgress]:
    out = {}
    for k, p in logic.progress(totals, targets).items():
        places = logic.DISPLAY_PLACES[k]
        out[k] = TargetProgress(
            target=p["target"],
            eaten=round(p["eaten"], places),
            remaining=round(p["remaining"], places),
            percent=p["percent"],
        )
    return out


# --- Server ---------------------------------------------------------------------------------


def build_server(settings: Settings, *, fdc: FDCClient | None = None) -> MCPServer:
    server = MCPServer(name="chomp", version=__version__, instructions=INSTRUCTIONS)
    expected_version = db.latest_version(settings.migrations_dir)
    tz = settings.tz
    fdc = fdc or FDCClient(settings.fdc_api_key)

    def connect():
        return closing(db.connect(settings.db_path))

    def meal_out(conn, meal_id: int) -> Meal:
        meal = store.get_meal(conn, meal_id)
        entries = store.meal_entries(conn, meal_id)
        return Meal(
            meal_id=meal["id"],
            eaten_at=logic.local_iso(meal["eaten_at"], tz),
            label=meal["label"],
            note=meal["note"],
            entries=[_entry(e) for e in entries],
            totals=_nutrients(logic.sum_nutrients(e.nutrients for e in entries)),
        )

    def day_summary(conn, utc: str) -> DaySummary:
        day = logic.local_date(utc, tz)
        start, end = logic.local_day_bounds(day, tz)
        totals = logic.sum_nutrients(n for _, n in store.entries_between(conn, start, end))
        return DaySummary(
            date=day.isoformat(),
            totals=_nutrients(totals),
            progress=_progress(totals, store.get_targets(conn)),
        )

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        try:
            with connect() as conn:
                version = db.schema_version(conn)
        except Exception:
            log.exception("health check failed")
            return JSONResponse({"status": "error"}, status_code=503)
        if version != expected_version:
            log.error("health check: schema version %d, expected %d", version, expected_version)
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok", "schema_version": version})

    @server.tool()
    def server_info() -> ServerInfo:
        """Report Chomp's version, database schema version, and display timezone.

        Use this to check that the Chomp connector is reachable. It returns no nutrition data.
        """
        with connect() as conn:
            version = db.schema_version(conn)
        return {
            "name": "chomp",
            "version": __version__,
            "schema_version": version,
            "tz": tz,
        }

    @server.tool()
    async def search_food(
        query: Annotated[
            str, Field(min_length=1, description="Food name as the user said it, e.g. 'egg whites'")
        ],
        limit: Annotated[int, Field(ge=1, le=SEARCH_LIMIT_MAX, description="Max results")] = 8,
    ) -> SearchResult:
        """Find foods to log. Call once per item the user mentions, before log_entries.

        Results are ranked: the user's aliases first (with default_grams), then foods already
        saved in Chomp (verified first), then USDA FoodData Central (generic Foundation and
        SR Legacy foods before Branded products). Every result has a food_id and per-100 g
        nutrients from the database. Prefer alias and saved matches; prefer generic FDC
        foods unless the user named a brand. Show the user which food you picked. If nothing
        fits, say so and ask for a barcode or label photo rather than guessing.
        """
        notes: list[str] = []
        hits: list[SearchHit] = []
        seen: set[int] = set()

        def add(row, **extra) -> None:
            if row["id"] not in seen and len(hits) < limit:
                seen.add(row["id"])
                hits.append(SearchHit(**_food(row).model_dump(), **extra))

        with connect() as conn:
            for row in store.search_aliases(conn, query, limit):
                add(
                    row,
                    match="alias",
                    alias=row["alias"],
                    default_grams=row["default_grams"],
                )
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
                        row, _ = store.cache_record(conn, rec)
                        add(row, match="fdc", fdc_data_type=rec.data_type)
        if not hits:
            notes.append(
                "No match. Try a simpler or more generic name, or ask for a barcode or label photo."
            )
        return SearchResult(query=query, results=hits, notes=notes)

    @server.tool()
    def log_entries(
        items: Annotated[list[LogItem], Field(min_length=1, description="One item per food eaten")],
        meal_label: Annotated[
            str | None, Field(description="e.g. breakfast, lunch, post-workout")
        ] = None,
        eaten_at: Annotated[
            str | None,
            Field(
                description="When it was eaten, ISO 8601 (e.g. 2026-10-05T08:30). No offset "
                "means the user's local time. Omit for now."
            ),
        ] = None,
        note: Annotated[str | None, Field(description="Free-text note for the meal")] = None,
    ) -> LogResult:
        """Log one meal: one or more foods with the grams eaten.

        Use food_ids from search_food or lookup_barcode; convert counts and household
        measures to grams first (use the food's serving_g or an alias's default_grams when
        they apply, and ask the user when unsure). Nutrients are computed from the database
        and snapshotted, so later food edits never change this log. Returns the meal with
        per-item nutrients and totals, plus the day's running totals and progress against
        targets. Show the user the per-item breakdown so they can correct it.
        """
        with tool_errors(), connect() as conn:
            when = logic.parse_eaten_at(eaten_at, tz)
            meal_id = store.log_meal(
                conn, [(i.food_id, i.grams) for i in items], when, meal_label, note
            )
            return LogResult(meal=meal_out(conn, meal_id), day=day_summary(conn, when))

    @server.tool()
    def update_entry(
        entry_id: Annotated[int, Field(description="entry_id from log_entries or get_day")],
        grams: Annotated[float | None, Field(gt=0, description="Corrected grams")] = None,
        food_id: Annotated[
            int | None, Field(description="Swap to this food (from search_food)")
        ] = None,
    ) -> UpdateResult:
        """Correct a logged item: change its grams, swap it to a different food, or both.

        Use when the user corrects something ("the eggs were 220 g", "that was whole milk").
        Nutrients are recomputed from the food's current values. Returns the updated entry,
        its meal's new totals, and the day's totals.
        """
        if grams is None and food_id is None:
            raise ToolError("give grams, food_id, or both")
        with tool_errors(), connect() as conn:
            entry = store.update_entry(conn, entry_id, grams, food_id)
            meal = meal_out(conn, entry.meal_id)
            day = day_summary(conn, store.get_meal(conn, entry.meal_id)["eaten_at"])
            return UpdateResult(entry=_entry(entry), meal=meal, day=day)

    @server.tool()
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

    @server.tool()
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
            totals = logic.sum_nutrients(n for _, n in store.entries_between(conn, start, end))
            return Day(
                date=day.isoformat(),
                meals=meals,
                totals=_nutrients(totals),
                progress=_progress(totals, store.get_targets(conn)),
            )

    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request URLs at INFO; keep upstream request details out of the log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()
    with closing(db.connect(settings.db_path)) as conn:
        version = db.migrate(conn, settings.schema_path, settings.migrations_dir)
    log.info("database ready at schema version %d", version)
    if not settings.fdc_api_key:
        log.warning("FDC_API_KEY is not set; search_food will only find saved foods")
    server = build_server(settings)
    server.run("streamable-http", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
