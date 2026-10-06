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
entries, aliases, templates, targets). `--start` and `--end` are inclusive local dates.

```bash
docker compose exec -T chomp chomp export --format csv > chomp.csv
docker compose exec -T chomp chomp export --format json --start 2026-10-01 > october.json
```

Locally, `uv run --env-file .env chomp export --format csv` does the same.
