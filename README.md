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

Deploy, auth, and connector setup are documented as part of M3.
