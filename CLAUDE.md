# CLAUDE.md — Chomp

Self-hosted MCP server (Claude and ChatGPT connectors) and `/app` web dashboard for meal logging. See ARCHITECTURE.md (design, tools, flows) and SCHEMA.sql (source of truth for the DB).

## Stack
Python 3.12 · `mcp` SDK v2 (`MCPServer`, which v1 called FastMCP; streamable HTTP) · SQLite · httpx · pytest · Docker Compose · uv · ruff

## Layout
```
chomp/
  server.py        # MCPServer app + tool registrations, /health
  auth.py          # OAuth 2.1 server + bearer token, consent page (password picks the user)
  users.py         # users and their scrypt passwords (`chomp user add`)
  health.py        # /ingest/health: phone posts Apple Health data (weight, activity) with an ingest token
  health_export.py # Apple Health's export.zip reader (past weigh-ins and daily activity)
  hevy.py          # Hevy workout CSV export parser (workouts are imported, Hevy has no free API)
  cli.py           # `chomp` command: serve, backup, restore, export, user (+ ingest-token), import-hevy, import-health
  backup.py        # nightly SQLite backup with retention, restore
  export.py        # CSV/JSON export
  config.py        # settings from env
  db.py            # connection, migrations (applies SCHEMA.sql)
  store.py         # SQL reads/writes behind the tools (no macro math)
  sources/
    http.py        # shared retries/backoff and TTL response cache
    fdc.py         # USDA FoodData Central client
    off.py         # Open Food Facts client
    gtin.py        # UPC/EAN normalization
    rank.py        # search hit ranking, weak-match trimming and grouping (FDC and OFF)
  barcode.py       # barcode chain (saved -> FDC -> OFF), shared by the tool and dashboard
  label.py         # nutrition label photo OCR (tesseract) and its text parser
  photos.py        # food photos: files, Open Food Facts fill and backfill
  serving_fill.py  # serving sizes for used USDA generic foods from household measures
  logic.py         # per-100g math, snapshots, totals, local-day boundaries
  text.py          # word matching (plural folding) for search ranking
  web.py           # /app web dashboard (server-rendered: Today, History, Foods, Weight, Workouts, Activity, saved meals, recipes, goals)
  static/          # dashboard CSS, JS, icons, vendored ZXing barcode reader
migrations/        # 0002_*.sql and up; SCHEMA.sql is baseline v1
tests/             # fixtures/ holds recorded upstream responses
.github/workflows/ # CI: ruff, pytest, Docker build and health check
Dockerfile         # slim, non-root (uid 10001)
docker-compose.yml # chomp + nightly backup + cloudflared
.env.example       # FDC_API_KEY, PUBLIC_URL + auth secrets, TUNNEL_TOKEN, backups, DB_PATH, PHOTOS_DIR, TZ
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
- Repo: private GitHub repo `Chomp`. Open issues are the source of truth for what's next (milestones M0–M4 are done; `later` marks parked ideas).
- At the start of a session, list the open issues and confirm which one we're working on.
- Branch per issue, and every change goes through a PR to `main` (never stacked on another PR's branch). Commit prefixes: `feat:`, `fix:`, `docs:`, `chore:`, with the issue number (`feat: add search_food (#4)`). Close issues via `Closes #N`.
- Update ARCHITECTURE.md or SCHEMA.sql in the same commit as the change that makes them stale.
- Changes to locked decisions (stack, source chain, hosting, auth) get a `decision` issue first.
- Never commit `.env`, `*.db`, `*.db-wal`, `*.db-shm`, `.venv/`. Check `git status` before committing.

## Commands
- `uv sync` (first time), then `uv run pytest -q`
- `uv run ruff check . && uv run ruff format --check .`
- `uv run --env-file .env chomp` to run the server; `npx @modelcontextprotocol/inspector` for local tool testing
- `docker compose up -d --build`
