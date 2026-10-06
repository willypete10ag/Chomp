# CLAUDE.md — Chomp

Self-hosted MCP server that Claude uses as a meal-logging backend. See ARCHITECTURE.md (design, tools, flows) and SCHEMA.sql (source of truth for the DB).

## Stack
Python 3.12 · `mcp` SDK (FastMCP, streamable HTTP) · SQLite · httpx · pytest · Docker Compose · uv · ruff

## Layout
```
chomp/
  server.py        # FastMCP app + tool registrations
  db.py            # connection, migrations (applies SCHEMA.sql)
  sources/
    fdc.py         # USDA FoodData Central client
    off.py         # Open Food Facts client
    gtin.py        # UPC/EAN normalization
  logic.py         # per-100g math, snapshots, totals, local-day boundaries
migrations/        # 0002_*.sql and up; SCHEMA.sql is baseline v1
tests/
.github/workflows/ # CI: pytest on push
docker-compose.yml
.env.example       # FDC_API_KEY, AUTH_SECRET, DB_PATH, TZ
.gitignore         # .env, *.db*, .venv/, __pycache__/
README.md
```

## Rules
- Never let tools return estimated macros; numbers come from the DB or upstream sources only.
- All macro math lives in `logic.py` and is unit-tested (per-serving → per-100 g, gram scaling, rounding).
- Upstream API calls go through `sources/` with timeouts, retries, and caching into `foods`.
- Tool docstrings are user-facing to Claude: state when to use the tool and what it returns.
- Secrets only via env. Nothing sensitive in logs.

## Git and progress tracking
- Repo: private GitHub repo `Chomp`. Issues and Milestones (M0–M4) are the source of truth for progress; the milestone breakdown is in ARCHITECTURE.md.
- At the start of a session, run `gh issue list --milestone "<current>"` and confirm which issue we're working on.
- Branch per issue: `m1/search-food`. Commit prefixes: `feat:`, `fix:`, `docs:`, `chore:`, with the issue number (`feat: add search_food (#4)`). Close issues via `Closes #N`.
- Update ARCHITECTURE.md or SCHEMA.sql in the same commit as the change that makes them stale.
- Changes to locked decisions (stack, source chain, hosting, auth) get a `decision` issue first.
- Never commit `.env`, `*.db`, `*.db-wal`, `*.db-shm`, `.venv/`. Check `git status` before committing.

## Commands
- `uv sync` (first time), then `uv run pytest -q`
- `uv run ruff check . && uv run ruff format --check .`
- `npx @modelcontextprotocol/inspector` for local tool testing
- `docker compose up -d --build`
