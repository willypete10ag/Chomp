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
Me (phone / browser) ──(HTTPS, /app)──▶ Cloudflare Tunnel ──▶ same server: web dashboard
```
Claude handles conversation, speech, and vision (reading barcodes and nutrition labels from photos). Chomp handles lookup, math, storage, and totals. No LLM runs on the homelab.

## Core flows

### Log from speech
1. "4 scrambled eggs and 300 g egg whites"
2. Claude calls `search_food` per item → picks best match (prefers foods the user has used before).
3. Claude calls `log_entries` with food_ids + grams → server returns per-item and meal totals.
4. Claude shows the breakdown. I correct ("eggs were 220 g") → `update_entry`.

### Log from barcode
1. I send a barcode photo (or read the number). Claude extracts the UPC.
2. `lookup_barcode(barcode)` → cache → FDC Branded (gtinUpc) → Open Food Facts.
3. Miss → Claude asks for a label photo, reads the panel, calls `save_food_from_label`. Cached forever after.

### Food with no label
Restaurant dish: Claude looks up the restaurant's published nutrition (its own site or PDF
first) or uses numbers I give, then `save_food`, never a generic USDA stand-in. Home
cooking: Claude asks for my numbers. Either way it needs the serving weight in grams (asked
for when not published). It is saved unconfirmed unless I approve the numbers;
`confirm_food` approves it later.

### Review
`get_day(date)` and `get_totals(start, end)` for daily/weekly summaries and protein-target checks.

### Dashboard
`/app` (`chomp/web.py`) is a server-rendered web dashboard on the same app as `/mcp`: Today
(any day via arrows, a seven-day strip or a date picker; calories remaining as the big number,
amber up to 100 kcal over the goal and red past that, then protein/carbs/fat with bars; goals
not set count as 2,000 kcal split in even thirds, for the connector's progress too; plus fiber/sugar/sodium; meals led by their
time; tapping the calories or a macro opens that day's Breakdown at `/app/breakdown`: the
protein/carbs/fat calorie split, calories by meal, the top 3 foods per macro, and the full
nutrition facts, where tapping a nutrient lists the foods that gave the most; a food with no
value for a nutrient counts as none of it, and a nutrient no food that day reports is left
out), History (7/30/90-day averages and inline-SVG charts; each day links to its Breakdown)
and Foods (one search over saved
meals and used foods, most logged first, with filters for recent, newest,
unconfirmed and confirmed foods (`store.FOOD_VIEWS`) and sections that fold away, remembered
in the browser. A tap opens a food's page (#72): name, brand and calories, protein, carbs
and fat, nutrition facts per serving and per 100 g, vitamins and minerals folded away, your
portions of it (#75: swipe one left to delete it, press and hold it to change its name or
grams, or + Add portion), then Log it, Confirm, Edit (all values, `/foods/{id}/edit`, with
Undo), Rename and Delete, and how often you've logged it. Press and hold a food in the list
for a menu over the blurred page (Edit, Rename, Confirm on amber foods, Log it, Delete);
swipe it left to Delete, or an amber one right to Confirm). An entry whose
grams were estimated shows a muted "~" before them until new grams are saved, and Add food
offers your portions as chips ("1 handful · 30 g") next to the gram chips. For a food with a serving
size, Add food and entry edits also have a Servings / Grams toggle (#80): Servings is a − / +
stepper in half servings and the server logs servings × the serving size; it is picked when the
amount is a whole or half number of servings, and a food without a serving size shows grams only.

It also works without Claude (#54): Add food (search your foods, portions and saved meals, or
USDA when `FDC_API_KEY` is set; log to a new meal at a time or into an existing one), change an
entry's grams, move it to another meal, edit a meal's name, time, date and note, combine or
delete meals, and set daily goals. Every edit is a form post through `store.py` like the
tools, so macros still come only from the database. Each edit records the rows it touched
before and after (`undo_actions`, migration 0005); the next page shows a confirmation with
Undo, which applies only while those rows are unchanged (so it never overwrites a newer edit
from Claude). A small same-origin script (`static/app.js`) adds sliders beside the number
boxes, quick picks (a serving, your usual, half/double), live previews of the calories and
the day's total, and the goals page's calorie bar (drag the borders between protein, carbs
and fat; the calorie total stays fixed, `logic.fit_macros` is the same split); it repeats `logic.scale` only for the preview, and every page works without
it. A web app manifest lets phones add it to the home screen.

New food (#59, Foods > Add a new food) adds a shared food three ways, all through one form of
per-serving values: a barcode (scanned with the camera by `static/scan.js` and the vendored
ZXing reader `static/zxing.min.js`, since the CSP allows only same-origin scripts; or typed),
looked up through the same chain as `lookup_barcode` (`chomp/barcode.py`); a nutrition label
photo, read on the server by `tesseract` (installed in the image) and parsed by
`chomp/label.py`, with a tick box per value; or typed in. Saving a barcode food confirms that
cached row (only values the person changed are rewritten); a label food is saved
`source='label'`, confirmed only when every filled-in value was ticked; a typed one is
`source='manual'`, confirmed only when its Verified box is ticked (#68). Any food can be
confirmed by hand, or marked unconfirmed again, from its page, its hold menu, or a swipe right on Foods. A barcode given with a label or typed food moves to it, as
with `save_food_from_label`. An Open Food Facts hit Claude looks up stays unconfirmed
until `confirm_food`.

Food photos (#74, `chomp/photos.py`): one picture per food, shown at the top of its page, as
a small square on Foods, Today and Add food rows (a food without one shows its first letter),
and added, replaced or removed from its page or its hold menu. A food with a barcode gets
Open Food Facts' front-of-package photo in the background after any barcode lookup (tool or
dashboard), and a one-time backfill (started by the first lookup or Foods page after a
deploy, then marked done with `.off-backfill-done`) does the same for barcode foods saved
earlier. Only images on Open Food Facts' own HTTPS hosts are downloaded. Label photos are
never used as a food's picture. Photos are files, `PHOTOS_DIR/<food_id>.jpg` (default
`photos/` next to the database), re-encoded on the way in: upright, at most 800 px, no EXIF.
Removing one leaves `<food_id>.none` so Open Food Facts doesn't put it back. Chat can't hand
Chomp a picture, so `save_food` and `save_food_from_label` return `add_photo_url` (the
food's page) when it has none, and the server instructions tell Claude to send that link.

### Users
Each person has their own log (#46, migration 0006): `meals` (and so entries), `targets`,
`portions`, `meal_templates`, `undo_actions`, `web_sessions` and `oauth_tokens` carry a
`user_id`; `foods` are shared, so anyone's saved or label foods turn up in everyone's search
(use counts are per person). User 1 is the owner, holds everything logged before users
existed, and signs in with `AUTH_PASSWORD`. Others are added with `chomp user add NAME` and
have a scrypt-hashed password in `users`. A password alone identifies the person (no two
users may share one; the server won't start if `AUTH_PASSWORD` matches another user's), so the consent page and dashboard sign-in stay a single password box;
the sign-in's user becomes the OAuth token's subject or the dashboard session's user.
`store.py` reads the user from the connection (`db.connect(path, user_id)`) and treats
another user's meal or entry as not found. The static `BEARER_TOKEN` is the owner's; in
local-only mode everything is the owner's.

## MCP tools
| Tool | Purpose | Key args |
|---|---|---|
| `search_food` | Names any saved meal template the phrase matches (so "my protein shake" points to `log_meal_template`), then fuzzy search (SQLite FTS5): foods with a portion the phrase names → foods the user has used (verified, from a label, barcode-scanned (`foods.saved`, migration 0002), logged, with a portion or templated; verified first) → FDC (ranked by query words matched, then how much of the name before its first comma the query names, then Foundation, SR Legacy, Branded). FDC hits are cached into `foods` so every result has a `food_id`, but a cached hit only counts as used once logged. Every hit lists the user's `portions` of it | `query`, `limit` |
| `lookup_barcode` | UPC/EAN lookup through the source chain; caches hits; a miss tells Claude to ask for a label photo | `barcode` |
| `save_food_from_label` | Save a food from a label read; converts per-serving to per-100 g | `name`, `brand?`, `upc?`, `serving_g`, per-serving macros (fat subtypes, cholesterol, added sugars, sugar alcohols optional), `micronutrients?` (amounts), `micronutrients_percent_dv?` (label % DV, converted with FDA Daily Values) |
| `save_food` | Save a food with no label or barcode (restaurant dish from its published nutrition, which Claude may look up on the web; home cooking from the user's numbers); `source='manual'`, confirmed only when `confirmed` | `name`, `brand?`, `serving_g`, per-serving macros (fiber, sugar, sodium, sat/trans fat, cholesterol, added sugars optional), `serving_desc?`, `confirmed?` |
| `log_entries` | Log one or more items as a meal, or add them to a logged meal | `items[{food_id, grams or portion + count, estimated?}]`, `eaten_at` (time or `now`; required for a new meal), `meal_label?`, `meal_id?` |
| `update_entry` | Change grams or swap food; re-snapshots macros | `entry_id`, `grams?`, `food_id?`, `estimated?` (new grams count as exact unless set) |
| `update_meal` | Move a meal to a different time, or change its label or note | `meal_id`, `eaten_at?`, `meal_label?`, `note?` |
| `delete_entry` | Remove a mistaken entry; deletes the meal when its last entry goes | `entry_id` |
| `get_day` | All meals/entries + totals for a local date, with progress against targets | `date` |
| `get_totals` | Aggregates, per-day totals and daily averages over a range (inclusive local dates), with progress against targets | `start`, `end?` |
| `confirm_food` | Mark a food verified once the user approves it (an OFF hit, a restaurant or home-cooked food, anything), optionally correcting its macros | `food_id`, `confirmed?` (false to unconfirm), macros?, `micronutrients?`, `micronutrients_percent_dv?`, `per_serving_g?` |
| `set_targets` | Set daily kcal/protein/carbs/fat targets; only given ones change | `kcal?`, `protein_g?`, `carbs_g?`, `fat_g?`, `clear?` |
| `save_portion` / `forget_portion` | Remember (or forget) what one of the user's portions of a food weighs ("my handful of Cheez-Its = 30 g") | `food_id`, `name`, `grams`, `count?`, `estimated?` / `food_id`, `name` |
| `save_meal_template` / `log_meal_template` | One-phrase repeat meals ("log my usual breakfast") | `name`, `items` / `name`, `eaten_at` |

Tool descriptions matter: they are Claude's only documentation. Each should say when to use it and that macros must come from tools, never estimates.

## Data rules
- Foods store macros **per 100 g**. Entries store grams plus a **snapshot** of all computed nutrients (kcal, protein, carbs, fat, fiber, sugar, sodium, the label details below, and 17 vitamins and minerals), so editing a food never rewrites history.
- Label details (#63, migration 0009): saturated, trans, polyunsaturated and monounsaturated fat, cholesterol, added sugars and sugar alcohols (`logic.DETAILS`), nullable like fiber, sugar and sodium, mapped from FDC and OFF (`polyols`), read by the label OCR and the New food form, and backfilled into cached unconfirmed foods the same way as vitamins. Net carbs (carbs − fiber − sugar alcohols) is computed on the Breakdown, only when sugar alcohols are known.
- Vitamins and minerals (#53, migration 0007): nullable `foods`/`entries` columns named with their unit (`vitamin_d_mcg`, `iron_mg`, ...; list and FDA Daily Values in `logic.MICROS` / `logic.DAILY_VALUES`). NULL means the source didn't report it. Tool results carry the known ones in `micronutrients`; a total names the ones only some items reported in `micronutrients_partial` (the sum is a floor). FDC and OFF amounts are converted from their reported unit (vitamin D IU → mcg; vitamin A IU is skipped). A cached, unconfirmed upstream food that lacks them gains them the next time the same record comes back; logged entries keep their snapshots.
- One `foods` row per upstream record (unique `(source, source_id)`).
- Portions (#75, migration 0010): people log counts ("3 handfuls"), not grams. `portions` holds
  each user's named amount of a food (`name`, `grams` for one, `estimated`). With no portion
  for a count, the agent asks for an exact weight or an estimate; an estimate is the food's
  `serving_g` × its count of servings, so only the amount is ever estimated and nutrients
  still come from the food. Entries logged that way carry `entries.estimated = 1` until new
  grams replace them (update_entry or the dashboard slider). A real weight for a portion
  (`save_portion`) clears its estimate. Portions replaced nicknames (`aliases`): the
  migration turned each nickname with usual grams into a portion ("1 Birria taco" →
  "Birria taco") and dropped the rest.
- Deleting a food (dashboard, Foods > swipe a food left > Delete; migration 0008) is for everyone,
  since foods are shared. Entries still point at it, so the row stays with `deleted = 1`,
  hidden from search and lists, its barcode released; everyone's portions of it go, and it
  leaves every saved meal (an emptied one goes too). If the same USDA or Open Food Facts
  record turns up again it comes back, barcode included.
- Every food records its `source` (`fdc`, `off`, `label`, `manual`) and `source_id`.
- Confirmed (`foods.verified`): checked by the person. That is a label food saved through
  `save_food_from_label`, a food confirmed with `confirm_food` (any food the person approves),
  a `save_food` food saved with `confirmed`, a food confirmed by hand on the dashboard (Foods:
  its page, hold menu or swipe right), a typed-in food saved with Verified ticked, a barcode food saved on the
  dashboard's New food page, or a label photo food whose every value was ticked there.
  Everything else is unconfirmed (amber on the dashboard). `confirm_food(confirmed=False)` or
  Mark unconfirmed on the dashboard takes an approval back.
- Timestamps in UTC; display in America/New_York. Day boundaries are computed in `logic.py` with `zoneinfo` (DST-correct); the `daily_totals` view is an approximate convenience only.
- Migrations: `SCHEMA.sql` is baseline v1. Later changes are numbered files in `migrations/` (`0002_<name>.sql`, ...), applied in order and tracked with `PRAGMA user_version`.

## Gotchas
- **UPC normalization:** UPC-A (12 digits) vs EAN-13 (leading 0) vs FDC's GTIN padding. Normalize to 14-digit GTIN for storage and try variants on lookup.
- **FDC Branded data** is per serving in `labelNutrients` and per 100 g in `foodNutrients`; prefer per-100 g, but when a core value differs from the label by more than 10% (and past a small rounding floor), use the label values and flag it.
- **FDC carbs "by difference"** can be slightly negative (seen: -0.104 g). Carbs down to -1 are stored as 0; any other negative value drops the record.
- **Open Food Facts** quality varies; mark OFF-sourced foods unverified until I confirm one.
- **Rate limits:** FDC key allows ~1,000 req/hr; cache aggressively.

## Security
- Cloudflare Tunnel (cloudflared in Compose, outbound only) exposes Chomp's hostname; Chomp answers only `/mcp`, `/consent`, the OAuth routes and metadata, and the `/app` dashboard. `/health` answers loopback only (the container health check). Zero Trust Access browser login would block Claude's servers, so no interactive SSO sits in front of `/mcp` or the OAuth routes; Access may cover the `/app` path only (README, step 7).
- Dashboard sign-in: a password page (the person's own password, sharing the consent page's lockout) starts a 30-day session for that user. The cookie (`HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/app`) holds a random token stored as a SHA-256 hash in `web_sessions` (migration 0004). Every POST must carry the public origin (CSRF check). Pages send a strict CSP: scripts and styles from Chomp itself only, nothing inline (the ZXing barcode reader is vendored for this); media may come from the camera (`mediastream:`). Label photos (20 MB at most) are read in memory, one at a time, and never stored. Food photos are served only to signed-in people. In local-only mode the dashboard, like `/mcp`, needs no sign-in.
- Auth (`chomp/auth.py`, decided on #24): Chomp is its own minimal OAuth 2.1 server; the password typed on the consent page picks the user (#46). One pre-registered client (ID and secret entered in Claude or ChatGPT's connector dialog; exact callback allowlist includes both Claude domains and the configured ChatGPT connector), PKCE S256, a password-protected consent page (5 wrong tries lock it for 15 min), 1 h access tokens, 90-day rotating refresh tokens. Tokens are stored as SHA-256 hashes in `oauth_tokens` (migration 0003), so restarts keep the connector signed in. Dynamic client registration is off. An optional static `BEARER_TOKEN` is also accepted (Inspector, or the connector's Request headers beta).
- With `PUBLIC_URL` unset Chomp runs local-only: no auth, and it refuses any non-loopback `HOST`. With it set, missing or short secrets stop startup. The public host is added to the SDK's DNS-rebinding allowlist (it answers 421 otherwise).
- Secrets (`FDC_API_KEY`, `AUTH_PASSWORD`, `OAUTH_CLIENT_SECRET`, `BEARER_TOKEN`, `TUNNEL_TOKEN`) in `.env`, never committed or logged.
- The container runs as uid 10001 with a read-only root filesystem and no capabilities; the database lives in the `chomp-data` volume.

## Backup and export
- `chomp backup --daily` (the Compose `backup` service) copies the DB nightly at `BACKUP_AT` with SQLite's online backup API (same as `sqlite3 .backup`), integrity-checks the copy, and keeps the newest `BACKUP_KEEP` in `BACKUP_HOST_DIR`. `chomp restore FILE --yes` replaces the DB with a backup after saving the current one. Food photos are copied to `BACKUP_HOST_DIR/photos` (new and changed files only; none deleted there), and a restore keeps photos only for foods the restored database has (a food added later could otherwise hand its id, and photo, to a new food), filling gaps from the backup and moving the old folder aside.
- `chomp export --format csv|json [--user NAME]`: CSV is one row per entry; JSON holds foods, meals with entries, portions, templates and targets. Both cover one user (the owner by default) and take inclusive local `--start`/`--end`.

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
- `save_meal_template`, `log_meal_template` (`save_alias` was replaced by portions, #75)
- `get_totals`
- Nightly SQLite backup

**Later (label `later`, no milestone)**
- Daily summary push (Discord webhook)
