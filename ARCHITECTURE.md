# Chomp — Architecture

## Overview
```
Me (Claude app: voice / text / photos)
        │  conversation
        ▼
Claude ──(custom connector, MCP over HTTPS)──▶ Cloudflare Tunnel ──▶ Chomp MCP server (Docker)
                                                                    ├── SQLite (chomp.db)
                                                                    ├── USDA FoodData Central API
                                                                    └── Open Food Facts API
```
Claude handles conversation, speech, and vision (reading barcodes and nutrition labels from photos). Chomp handles lookup, math, storage, and totals. No LLM runs on the homelab.

## Core flows

### Log from speech
1. "4 scrambled eggs and 300 g egg whites"
2. Claude calls `search_food` per item → picks best match (prefers cached/saved foods).
3. Claude calls `log_entries` with food_ids + grams → server returns per-item and meal totals.
4. Claude shows the breakdown. I correct ("eggs were 220 g") → `update_entry`.

### Log from barcode
1. I send a barcode photo (or read the number). Claude extracts the UPC.
2. `lookup_barcode(upc)` → cache → FDC Branded (gtinUpc) → Open Food Facts.
3. Miss → Claude asks for a label photo, reads the panel, calls `save_food_from_label`. Cached forever after.

### Review
`get_day(date)` and `get_totals(start, end)` for daily/weekly summaries and protein-target checks.

## MCP tools
| Tool | Purpose | Key args |
|---|---|---|
| `search_food` | Fuzzy search (SQLite FTS5): aliases → saved foods (verified first) → FDC (Foundation, SR Legacy, Branded). FDC hits are cached into `foods` so every result has a `food_id` | `query`, `limit` |
| `lookup_barcode` | UPC lookup through the source chain; caches hits | `upc` |
| `save_food_from_label` | Save a food from a label read; converts per-serving to per-100 g | `name`, `brand?`, `upc?`, `serving_g`, per-serving macros |
| `log_entries` | Log one or more items as a meal | `items[{food_id, grams}]`, `meal_label?`, `eaten_at?` |
| `update_entry` | Change grams or swap food; re-snapshots macros | `entry_id`, `grams?`, `food_id?` |
| `delete_entry` | Remove a mistaken entry; deletes the meal when its last entry goes | `entry_id` |
| `get_day` | All meals/entries + totals for a local date, with progress against targets | `date` |
| `get_totals` | Aggregates and daily averages over a range, with progress against targets | `start`, `end` |
| `confirm_food` | Mark a food verified (e.g. an OFF hit), optionally correcting its macros | `food_id`, macros? |
| `set_targets` | Set daily kcal/protein/carbs/fat targets | `kcal?`, `protein_g?`, `carbs_g?`, `fat_g?` |
| `save_alias` | "my usual eggs" → food_id + default grams | `alias`, `food_id`, `default_grams?` |
| `save_meal_template` / `log_meal_template` | One-phrase repeat meals | `name`, `items` |

Tool descriptions matter: they are Claude's only documentation. Each should say when to use it and that macros must come from tools, never estimates.

## Data rules
- Foods store macros **per 100 g**. Entries store grams plus a **snapshot** of all computed nutrients (kcal, protein, carbs, fat, fiber, sugar, sodium), so editing a food never rewrites history.
- One `foods` row per upstream record (unique `(source, source_id)`).
- Every food records its `source` (`fdc`, `off`, `label`, `manual`) and `source_id`.
- Timestamps in UTC; display in America/New_York. Day boundaries are computed in `logic.py` with `zoneinfo` (DST-correct); the `daily_totals` view is an approximate convenience only.
- Migrations: `SCHEMA.sql` is baseline v1. Later changes are numbered files in `migrations/` (`0002_<name>.sql`, ...), applied in order and tracked with `PRAGMA user_version`.

## Gotchas
- **UPC normalization:** UPC-A (12 digits) vs EAN-13 (leading 0) vs FDC's GTIN padding. Normalize to 14-digit GTIN for storage and try variants on lookup.
- **FDC Branded data** is per serving in `labelNutrients` and per 100 g in `foodNutrients`; prefer per-100 g, verify against serving size.
- **Open Food Facts** quality varies; mark OFF-sourced foods unverified until I confirm one.
- **Rate limits:** FDC key allows ~1,000 req/hr; cache aggressively.

## Security
- Cloudflare Tunnel exposes only the MCP endpoint. Auth via OAuth (supported by Claude custom connectors) or a long random bearer token if the plan's connector setup allows it. Zero Trust Access browser login will block Claude's servers, so don't put interactive SSO in front of the endpoint.
- Secrets (`FDC_API_KEY`, auth secret) in `.env`, never committed.
- Nightly SQLite backup (`sqlite3 .backup`) to the media/backup disk.

## Repo and progress tracking
Private GitHub repo: `Chomp`. Progress lives in GitHub, not in chat history or a markdown checklist.

- **Milestones:** one GitHub Milestone per milestone below (M0–M4). `Later` items are issues with the `later` label and no milestone.
- **Issues:** one per bullet below. Closed by commits/PRs with `Closes #N`.
- **Labels:** `tool`, `source`, `infra`, `security`, `bug`, `decision`, `later`.
- **Project board (optional):** a GitHub Project with Todo / In progress / Done, filtered to this repo.
- **Branches:** `m1/search-food`, `m3/cloudflare-tunnel`. Solo repo, so PRs are optional, but PRs give you a clean change record per issue.
- **Commits:** `feat:`, `fix:`, `docs:`, `chore:` prefixes, with the issue number in the message.
- **Tooling:** `uv` for env and lockfile, `ruff` for lint and format; CI runs `ruff check`, `ruff format --check`, and `pytest -q`.
- **Design docs:** this file, `SCHEMA.sql`, and `CLAUDE.md` live in the repo root. A change to a locked decision gets a `decision` issue first.
- **Never committed:** `.env`, `*.db`, `*.db-wal`, `*.db-shm`, `.venv/`, `__pycache__/`. Commit `.env.example` only.

### Bootstrap
```bash
git init -b main
gh repo create Chomp --private --source=. --remote=origin

for l in tool source infra security bug decision later; do gh label create "$l" 2>/dev/null; done

for m in "M0: Repo setup" "M1: Core logging" "M2: Barcode chain" "M3: Deploy and connect" "M4: Quality of life"; do
  gh api repos/:owner/:repo/milestones -f title="$m" >/dev/null
done
```
Then create one issue per bullet below with `gh issue create --milestone "<name>" --label <label> --title "<bullet>"`.

## Milestones
**M0: Repo setup**
- Create the private repo, `.gitignore`, `.env.example`, README stub
- Get a USDA FoodData Central API key (api.data.gov) and store it in `.env`
- GitHub Actions workflow running `pytest -q` on push

**M1: Core logging**
- SQLite schema + migration runner (applies `SCHEMA.sql`)
- FastMCP server skeleton with streamable HTTP transport
- `logic.py` macro math with unit tests (per-serving to per-100 g, gram scaling, rounding)
- `search_food` against FDC
- `log_entries`, `update_entry`, `delete_entry`, `get_day`
- Verify all tools locally with MCP Inspector

**M2: Barcode chain**
- GTIN normalization (UPC-A, EAN-13, 14-digit) with tests
- `lookup_barcode`: cache, then FDC Branded, then Open Food Facts
- `save_food_from_label` with per-serving to per-100 g conversion
- Cache every hit into `foods`; mark OFF-sourced foods unverified

**M3: Deploy and connect**
- Dockerfile + Docker Compose
- Cloudflare Tunnel exposing only the MCP endpoint
- Auth on the endpoint (OAuth or long bearer token)
- Add as a custom connector in Claude and run a real day of logging
- One week of real-world use; file bugs as issues

**M4: Quality of life**
- `save_alias`, `save_meal_template`, `log_meal_template`
- `get_totals`
- Nightly SQLite backup

**Later (label `later`, no milestone)**
- Daily summary push (Discord webhook)
