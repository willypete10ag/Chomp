# Chomp

Self-hosted meal log you talk to. Tell Claude (or ChatGPT) what you ate by voice, text, or a
photo of a barcode or nutrition label; it calls Chomp's MCP tools to look up the food, log it
and report your totals. A web dashboard on the same server shows your day against your goals
and lets you log and edit without a chat. Everything lives in one SQLite database on your
homelab, reached through a Cloudflare Tunnel.

Chomp never estimates nutrition: every calorie and macro comes from the database, USDA
FoodData Central, Open Food Facts, a label, or numbers you give. Only amounts ("a handful")
can be estimated, and those are marked.

## What it does

**In chat** (custom connector, 20 tools):

- Log meals with the time you ate them, correct grams or swap a food, move or delete a meal.
- Find foods in your own list first, then USDA (including dishes as eaten) and Open Food
  Facts. Barcodes go through your saved foods, USDA Branded, then Open Food Facts; a miss asks
  for a label photo.
- Save foods from a label, a restaurant's published nutrition, or your own numbers. Foods
  from Open Food Facts or the web stay unconfirmed until you approve them.
- Remember your portions ("my handful of Cheez-Its is 30 g"), saved meals ("log my usual
  breakfast") and homemade recipes (logged by the serving).
- Daily and range totals with averages, progress against calorie and macro goals, fiber,
  sugar, sodium, label details (fat types, cholesterol, added sugars) and 17 vitamins and
  minerals.

**On the dashboard** (`/app`, works on a phone and installs to the home screen):

- **Today:** calories left as the big number, macro bars, meals by time. Arrows, a week strip
  or a month calendar (dots on logged days, stars on days within 10% of every goal) move
  between days. Tap a number for that day's Breakdown: calories by macro and meal, top foods,
  full nutrition facts.
- **Log and edit:** Add food in servings or grams with your portions as chips; change, move
  or delete entries; rename, retime, combine or delete meals. Every change has Undo.
- **Goals:** daily calories split between protein, carbs and fat on one draggable bar.
- **History:** 7, 30 and 90-day averages and charts.
- **Foods:** your foods with photos, sorted by most logged, recent, calories and more; each
  food has a page with nutrition facts, portions, Log it, Confirm, Edit and Delete. Search
  filters as you type; a food that isn't there is one tap from a database search or typing it
  in. Add a new food by searching USDA (as you type) and Open Food Facts, with the plain food
  first, scanning its barcode with the camera, reading a label photo (tesseract on the
  server, no AI), or typing it in.
- **Saved meals and Recipes:** build, edit and log them from the Foods tab.

**Running it:** several people, each with their own log, goals, portions, saved meals and
recipes (the food list is shared); OAuth sign-in for the Claude and ChatGPT connectors;
nightly backups with photos; CSV and JSON export.

Design lives in [ARCHITECTURE.md](ARCHITECTURE.md) and [SCHEMA.sql](SCHEMA.sql). Work is
tracked in GitHub Issues; every change lands through a pull request to `main`.

## Development

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

### Try the tools in MCP Inspector

With the server running and `FDC_API_KEY` set, open Inspector, choose transport
"Streamable HTTP", URL `http://127.0.0.1:8000/mcp`, then Connect and open the Tools tab:

1. `search_food` with `{"query": "scrambled eggs"}` and again with `{"query": "egg whites"}`; note a `food_id` from each.
2. `log_entries` with `{"items": [{"food_id": <eggs>, "grams": 200}, {"food_id": <whites>, "grams": 300}], "eaten_at": "now"}`.
3. `update_entry` with the eggs' `entry_id` and `{"grams": 220}`.
4. `get_day` with no arguments: the meal, its corrected totals, and the day's totals.

## Deploy (Proxmox + Docker Compose + Cloudflare Tunnel)

Chomp runs as three containers: `chomp` (the MCP server), `backup` (nightly SQLite copies),
and `cloudflared` (an outbound tunnel to Cloudflare). No router ports are opened and nothing
listens on the host. You need a domain on Cloudflare (free plan is fine).

### 1. A Debian VM on Proxmox

Create a VM from the Debian 13 netinst ISO: 1 vCPU, 1 GB RAM, 16 GB disk, SSH server ticked
in the installer, no desktop. A VM is simpler than an LXC for Docker (no nesting settings).
Then, over SSH as your user:

```bash
sudo apt update && sudo apt install -y curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # then log out and back in
```

### 2. Get the code

The repo is private, so give the VM a read-only deploy key:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/chomp_deploy -C chomp-vm
cat ~/.ssh/chomp_deploy.pub
```

On GitHub: repo Settings > Deploy keys > Add deploy key, paste it, leave write access off.

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/chomp_deploy" git clone git@github.com:willypete10ag/chomp.git ~/chomp
cd ~/chomp && git config core.sshCommand "ssh -i ~/.ssh/chomp_deploy"
```

### 3. Cloudflare Tunnel

In the Cloudflare dashboard: Zero Trust > Networks > Tunnels > Create a tunnel > Cloudflared.
Name it `chomp`, pick Docker, and copy the token (the long string after `--token`). Add a
public hostname: subdomain `chomp` (or anything), your domain, service type **HTTP**, URL
**`chomp:8000`**. Do not add a Cloudflare Access policy in front of it: Claude's servers
can't do an interactive login. Chomp's own OAuth protects the endpoint, and it answers only
`/mcp`, its sign-in routes and OAuth metadata (everything else is 404).

### 4. Configure

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # run once per secret
nano .env
```

Set `FDC_API_KEY`, `PUBLIC_URL=https://chomp.<your-domain>`, `AUTH_PASSWORD` (something you
can type, 12+ characters), `OAUTH_CLIENT_SECRET` and `BEARER_TOKEN` (generated), and
`TUNNEL_TOKEN`. Chomp refuses to start if `PUBLIC_URL` is set without the auth settings.

Pick the backup folder. Ideally it's a different disk from the VM's own (a second Proxmox
disk or a NAS mount); `./backups` inside the checkout is the default. A one-shot
`backup-init` container hands the folder to the container user (uid 10001) on every start:

```bash
sudo mkdir -p /mnt/backup/chomp
# then in .env: BACKUP_HOST_DIR=/mnt/backup/chomp
```

### 5. Start it

```bash
docker compose up -d --build
docker compose ps                      # chomp should say (healthy)
docker compose logs -f cloudflared     # look for "Registered tunnel connection"
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://chomp.<your-domain>/mcp   # 401
```

The 401 is right: the endpoint is up and wants auth.

### 6. Add the connector in Claude

Claude (web or desktop) > Customize > Connectors > Add custom connector:

- Name: `Chomp`. URL: `https://chomp.<your-domain>/mcp`
- Advanced settings: OAuth Client ID = your `OAUTH_CLIENT_ID`, OAuth Client Secret = your
  `OAUTH_CLIENT_SECRET`.
- Add, then Connect. A "Connect Claude to Chomp" page opens: enter `AUTH_PASSWORD`, Allow.

Turn Chomp on in a chat (+ menu > Connectors) and ask Claude to call `server_info`. The
connector stays signed in across restarts (tokens are stored hashed in the database and
refresh themselves). Five wrong passwords lock the sign-in page for 15 minutes.

Alternative: if your dialog has a **Request headers** section, you can choose No sign-in and
add header `authorization` = `Bearer <BEARER_TOKEN>` instead. Claude allows only one
connector per URL.

### Add the ChatGPT connector

In ChatGPT's custom MCP server form, use your public `/mcp` URL and **OAuth** with
**User-Defined OAuth Client**. Enter the same `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET`
configured on Chomp, choose **client_secret_post**, and leave scopes blank. Keep the
prefilled callback URL and discovered endpoints unchanged.

ChatGPT's callback must be on the exact allowlist, `CONNECTOR_CALLBACKS` in
`chomp/auth.py` (it already holds one ChatGPT connector's and both Claude callbacks). A new
or recreated ChatGPT connector gets its own callback URL: add that exact URL there, never a
wildcard, and redeploy before connecting. ChatGPT voice-mode tool use is not verified.

### 7. Open the dashboard

Go to `https://chomp.<your-domain>/app` and sign in with your password (`AUTH_PASSWORD`
for the owner). A sign-in lasts 30 days; the same five-wrong-passwords lockout applies. It
works without Claude too (see [What it does](#what-it-does)); searching USDA from the
dashboard needs `FDC_API_KEY`. The barcode camera needs the HTTPS address and asks for
camera access on the first scan. Deleting a food deletes it for everyone, but meals already
logged keep their numbers.

On a phone, add it to the home screen so it opens like an app:

- iPhone (Safari): Share button > **Add to Home Screen**.
- Android (Chrome): ⋮ menu > **Add to Home screen** (or **Install app**).

#### Optional: Cloudflare Access in front of the dashboard

This adds a second lock (an emailed code) before the Chomp password. It must cover only
`/app`: if it covered `/mcp` or the sign-in routes, Claude's servers couldn't get through
and the connector would stop working.

1. Cloudflare dashboard > **Zero Trust** > **Access** > **Applications** > **Add an
   application** > **Self-hosted**.
2. Name `Chomp dashboard`. Session duration: **1 month**.
3. Public hostname: subdomain `chomp`, domain `<your-domain>`, path `app`. Leave every other
   path alone.
4. Add a policy: name `Me`, action **Allow**, include **Emails** and list your email
   addresses (one per person who should get in).
5. Login method: **One-time PIN** is enough. Save.
6. Check both sides: open `https://chomp.<your-domain>/app` in a private window (you should
   get Cloudflare's email-code page first), then ask Claude to call `server_info` (it must
   still work).

### 8. Add another person

Each person gets their own meal log, targets, portions and saved meals; the food list is
shared. You are the owner (everything logged before this existed is yours) and keep signing
in with `AUTH_PASSWORD`. Add someone else on the server, then type their password twice:

```bash
cd ~/chomp
docker compose exec chomp chomp user add Sam
docker compose exec chomp chomp user rename owner Will   # optional: your name on the dashboard
docker compose exec chomp chomp user list
```

Their password (12+ characters) must differ from everyone else's: Chomp tells people apart by
password alone, so the sign-in pages stay a single box. Then, in **their own** Claude account,
they add the connector exactly as in step 6 (same URL, same `OAUTH_CLIENT_ID` and
`OAUTH_CLIENT_SECRET`) and type **their** password on the "Connect Claude to Chomp" page. The
dashboard is the same address; their password opens their log. To change someone's password
(it signs them out everywhere): `docker compose exec chomp chomp user password Sam`. The
`BEARER_TOKEN` header, if you use it, is always the owner's.

If they use ChatGPT, their connector gets its own callback URL, which must be added to
`CONNECTOR_CALLBACKS` in `chomp/auth.py` first (see "Add the ChatGPT connector").

### 9. Send weight from your iPhone (Apple Health)

A smart scale that writes to Apple Health (VeSync does) can reach Chomp on its own: an iPhone
app posts new readings to `https://chomp.<your-domain>/ingest/health`. Make each person's
token on the server; it is shown once, and running it again replaces the old one:

```bash
docker compose exec chomp chomp user ingest-token Will
docker compose exec chomp chomp user ingest-token Will --revoke   # stop accepting it
```

The token can only add that person's health data; it opens nothing else. Then on that
person's iPhone, pick one:

- **Health Auto Export**: add a REST API automation with the URL above, JSON format, the
  Weight & Body Mass metric, and a header `Authorization` = `Bearer <token>`.
- **iOS Shortcut**: find the latest Weight samples, build
  `{"weight": [{"date": "<ISO 8601 date>", "value": <number>, "unit": "lb"}]}`, and send it
  with Get Contents of URL (POST, same header), from a daily personal automation.

Sending the same readings again is harmless: Chomp keeps one weigh-in per time. The reply
says how many were added, updated or unchanged. Don't put Cloudflare Access in front of
`/ingest` (the phone can't sign in to it). Ask Claude "what do I weigh?" to check.

### 10. Import workouts from Hevy

In Hevy: Profile › Settings › Export & Import Data › Export Workouts. Then on the dashboard's
**Workouts** tab, open Import from Hevy and pick the CSV. Import again whenever you like; only
new or changed workouts are added. A big first import can also be done on the server:

```bash
docker compose exec -T chomp chomp import-hevy - --user Will < workout_data.csv
```

Ask Claude "what did I bench last time?" or "what did I do at the gym this week?".

### Updating

```bash
cd ~/chomp && git pull && docker compose up -d --build
```

Migrations run automatically on start. Take a backup first if the pull request mentions one.
If the update changed any tools, start a new chat: an open chat keeps the connector's old tool
list (ChatGPT may need its connector refreshed).

## Backups and restore

The `backup` container copies the database every night at `BACKUP_AT` (local `TZ`) using
SQLite's online backup, integrity-checks the copy, and keeps the newest `BACKUP_KEEP`.
Food photos (kept in the data volume at `/data/photos`) go to a `photos/` folder beside the
backups. A restore keeps photos only for foods in the restored database (taking any missing
ones from the backup) and moves the old photo folder aside as `photos.pre-restore-<time>`.
Back up right now with `docker compose exec backup chomp backup`.

To restore (this replaces the whole database; the current one is kept as
`chomp.db.pre-restore-<time>` in the data volume):

```bash
ls /mnt/backup/chomp                    # pick a file
docker compose stop chomp backup
docker compose run --rm --no-deps backup chomp restore /backups/chomp-YYYYMMDD-HHMMSS.db --yes
docker compose up -d
```

## Export

CSV is one row per logged entry (for spreadsheets); JSON is everything (foods, meals and
entries, portions, templates, targets). `--start` and `--end` are inclusive local dates. An
export is the owner's log unless you add `--user NAME`.

```bash
docker compose exec -T chomp chomp export --format csv > chomp.csv
docker compose exec -T chomp chomp export --format json --start 2026-10-01 > october.json
```

Locally, `uv run --env-file .env chomp export --format csv` does the same.
