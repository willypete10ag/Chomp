# Chomp

Self-hosted MCP server that Claude uses as a meal-logging backend. You talk to Claude (voice, text, photos of barcodes and labels); Claude calls Chomp's tools to look up macros and write entries to a SQLite database on your homelab.

Design lives in [ARCHITECTURE.md](ARCHITECTURE.md) and [SCHEMA.sql](SCHEMA.sql). Progress lives in GitHub Issues and Milestones (M0 to M4).

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync                       # create .venv and install deps
cp .env.example .env          # then fill in FDC_API_KEY
uv run pytest -q              # tests
uv run ruff check . && uv run ruff format --check .
```

Run the server locally on http://127.0.0.1:8000/mcp (health check at `/health`):

```bash
uv run --env-file .env chomp  # DB_PATH, TZ, HOST, PORT come from .env or the environment
npx @modelcontextprotocol/inspector   # connect to http://127.0.0.1:8000/mcp
```

### Try the tools in MCP Inspector

With the server running and `FDC_API_KEY` set, open Inspector, choose transport
"Streamable HTTP", URL `http://127.0.0.1:8000/mcp`, then Connect and open the Tools tab:

1. `search_food` with `{"query": "scrambled eggs"}` and again with `{"query": "egg whites"}`; note a `food_id` from each.
2. `log_entries` with `{"items": [{"food_id": <eggs>, "grams": 200}, {"food_id": <whites>, "grams": 300}], "meal_label": "breakfast"}`.
3. `update_entry` with the eggs' `entry_id` and `{"grams": 220}`.
4. `get_day` with no arguments: the meal, its corrected totals, and the day's totals.

Deploy, auth, and connector setup are documented as part of M3.
