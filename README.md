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
2. `log_entries` with `{"items": [{"food_id": <eggs>, "grams": 200}, {"food_id": <whites>, "grams": 300}], "meal_label": "breakfast"}`.
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

The allowlist includes `https://chatgpt.com/connector/oauth/e3AEiu3ZZRkR` alongside both
Claude callbacks. Deploy this version before connecting. A recreated connector with a
different callback needs its exact URL added to the allowlist; do not use a wildcard.
This enables OAuth authentication; ChatGPT voice-mode tool support is not verified.

### 7. Open the dashboard

Go to `https://chomp.<your-domain>/app` and sign in with `AUTH_PASSWORD`. It shows today's
meals against your targets (the arrows, the row of days and the date box move between days),
7/30/90-day history charts, and your saved meals, nicknames and foods. It works without
Claude too: **+ Add food** logs a food or saved meal (and searches USDA when `FDC_API_KEY` is
set), tapping an item gives a grams slider, a move to another meal and delete, **Edit meal…**
changes a meal's name, time or date or combines it with another, **Edit goals** sets your
daily targets, and every change shows an **Undo** button. A sign-in lasts 30 days; the same
five-wrong-passwords lockout applies.

**Foods > Add a new food** adds a food three ways, all ending in the same form: scan its
barcode with the phone camera (or type the number under it), take a photo of the Nutrition
Facts label, or type the values in. A barcode is looked up in USDA and Open Food Facts; check
the values and save, and it counts as confirmed. A label photo is read on the server
(tesseract, no AI) and each value gets a tick box: fix anything misread, tick each value, and
only a food with every value ticked counts as confirmed. Typed-in foods stay unconfirmed
(amber). The camera needs the HTTPS address; the first scan asks for camera access.

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

Each person gets their own meal log, targets, nicknames and saved meals; the food list is
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

### Updating

```bash
cd ~/chomp && git pull && docker compose up -d --build
```

Migrations run automatically on start. Take a backup first if the release notes mention one.

## Backups and restore

The `backup` container copies the database every night at `BACKUP_AT` (local `TZ`) using
SQLite's online backup, integrity-checks the copy, and keeps the newest `BACKUP_KEEP`.
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
entries, aliases, templates, targets). `--start` and `--end` are inclusive local dates. An
export is the owner's log unless you add `--user NAME`.

```bash
docker compose exec -T chomp chomp export --format csv > chomp.csv
docker compose exec -T chomp chomp export --format json --start 2026-10-01 > october.json
```

Locally, `uv run --env-file .env chomp export --format csv` does the same.
