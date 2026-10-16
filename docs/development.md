# Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync                       # create .venv and install deps
cp .env.example .env          # then fill in FDC_API_KEY
uv run pytest -q              # tests
uv run ruff check . && uv run ruff format --check .
```

Run the server locally on http://127.0.0.1:8000/mcp (health check at `/health`). With
`PUBLIC_URL` blank it runs without auth and only on 127.0.0.1:

```bash
uv run --env-file .env chomp  # DB_PATH, TZ, HOST, PORT come from .env or the environment
npx @modelcontextprotocol/inspector   # connect to http://127.0.0.1:8000/mcp
```

## Try the tools in MCP Inspector

With the server running and `FDC_API_KEY` set, open Inspector, choose transport
"Streamable HTTP", URL `http://127.0.0.1:8000/mcp`, then Connect and open the Tools tab:

1. `search_food` with `{"query": "scrambled eggs"}` and again with `{"query": "egg whites"}`; note a `food_id` from each.
2. `log_entries` with `{"items": [{"food_id": <eggs>, "grams": 200}, {"food_id": <whites>, "grams": 300}], "eaten_at": "now"}`.
3. `update_entry` with the eggs' `entry_id` and `{"grams": 220}`.
4. `get_day` with no arguments: the meal, its corrected totals, and the day's totals.

## How it is built

- [ARCHITECTURE.md](../ARCHITECTURE.md): design, the MCP tools, flows and decisions.
- [SCHEMA.sql](../SCHEMA.sql): the database, the source of truth for every table.
- [migrations/](../migrations/README.md): how schema changes ship.
- [CLAUDE.md](../CLAUDE.md): conventions for coding agents working in this repo.

Work is tracked in GitHub Issues, and every change lands through a pull request to `main`.
CI runs ruff and the test suite, then builds the Docker image and checks it starts.
