# Set up Chomp

This guide takes you from nothing to Chomp running on your own server, with Claude (or
ChatGPT) connected and the dashboard on your phone. It assumes a small always-on Linux machine
with Docker, and a domain on Cloudflare.

Chomp runs as three containers: `chomp` (the MCP server), `backup` (nightly SQLite copies),
and `cloudflared` (an outbound tunnel to Cloudflare). No router ports are opened and nothing
listens on the host. You need a domain on Cloudflare (free plan is fine).

## 1. A small Linux server

Any always-on Linux machine that runs Docker will do: a mini PC, a VPS or
a VM. Chomp needs about 1 vCPU, 1 GB RAM and a few GB of disk. The steps below use a
Debian VM on Proxmox as the example.

On Proxmox, create a VM from the Debian 13 netinst ISO: 1 vCPU, 1 GB RAM, 16 GB disk, SSH server ticked
in the installer, no desktop. A VM is simpler than an LXC for Docker (no nesting settings).
Then, over SSH as your user:

```bash
sudo apt update && sudo apt install -y curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # then log out and back in
```

## 2. Get the code

```bash
git clone https://github.com/willypete10ag/chomp.git ~/chomp
cd ~/chomp
```

## 3. Cloudflare Tunnel

In the Cloudflare dashboard: Zero Trust > Networks > Tunnels > Create a tunnel > Cloudflared.
Name it `chomp`, pick Docker, and copy the token (the long string after `--token`). Add a
public hostname: subdomain `chomp` (or anything), your domain, service type **HTTP**, URL
**`chomp:8000`**. Do not add a Cloudflare Access policy in front of it: Claude's servers
can't do an interactive login. Chomp's own OAuth protects the endpoint, and it answers only
`/mcp`, its sign-in routes and OAuth metadata (everything else is 404).

## 4. Configure

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # run once per secret
nano .env
```

Set `FDC_API_KEY`, `PUBLIC_URL=https://chomp.<your-domain>`, `AUTH_PASSWORD` (something you
can type, 12+ characters), `OAUTH_CLIENT_SECRET` and `BEARER_TOKEN` (generated), and
`TUNNEL_TOKEN`. Chomp refuses to start if `PUBLIC_URL` is set without the auth settings.

Pick the backup folder. Ideally it's a different disk from the VM's own (a second disk
or a NAS mount); `./backups` inside the checkout is the default. A one-shot
`backup-init` container hands the folder to the container user (uid 10001) on every start:

```bash
sudo mkdir -p /mnt/backup/chomp
# then in .env: BACKUP_HOST_DIR=/mnt/backup/chomp
```

## 5. Start it

```bash
docker compose up -d --build
docker compose ps                      # chomp should say (healthy)
docker compose logs -f cloudflared     # look for "Registered tunnel connection"
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://chomp.<your-domain>/mcp   # 401
```

The 401 is right: the endpoint is up and wants auth.

## 6. Add the connector in Claude

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

## Add the ChatGPT connector

In ChatGPT's custom MCP server form, use your public `/mcp` URL and **OAuth** with
**User-Defined OAuth Client**. Enter the same `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET`
configured on Chomp, choose **client_secret_post**, and leave scopes blank. Keep the
prefilled callback URL and discovered endpoints unchanged.

ChatGPT's callback must be on the exact allowlist, `CONNECTOR_CALLBACKS` in
`chomp/auth.py` (it already holds one ChatGPT connector's and both Claude callbacks). A new
or recreated ChatGPT connector gets its own callback URL: add that exact URL there, never a
wildcard, and redeploy before connecting. ChatGPT voice-mode tool use is not verified.

## 7. Open the dashboard

Go to `https://chomp.<your-domain>/app` and sign in with your password (`AUTH_PASSWORD`
for the owner). A sign-in lasts 30 days; the same five-wrong-passwords lockout applies. It
works without Claude too (see [Features](features.md)); searching USDA from the
dashboard needs `FDC_API_KEY`. The barcode camera needs the HTTPS address and asks for
camera access on the first scan. Deleting a food deletes it for everyone, but meals already
logged keep their numbers.

On a phone, add it to the home screen so it opens like an app:

- iPhone (Safari): Share button > **Add to Home Screen**.
- Android (Chrome): ⋮ menu > **Add to Home screen** (or **Install app**).

### Optional: Cloudflare Access in front of the dashboard

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

## Next steps

- [Add another person](people.md), each with their own log and password.
- [Send weight and activity from an iPhone](apple-health.md) (Apple Health).
- [Sync workouts from Hevy](hevy.md).
- [Backups, restore and export](backups.md).

## Updating

```bash
cd ~/chomp && git pull && docker compose up -d --build
```

Migrations run automatically on start. Take a backup first if the release notes or pull request mention one.
If the update changed any tools, start a new chat: an open chat keeps the connector's old tool
list (ChatGPT may need its connector refreshed).
