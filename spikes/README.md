# Spikes

Throwaway experiments. Nothing in `chomp/` imports from here.

## connector_auth.py: does Claude's custom connector accept our auth? (#23)

A one-tool MCP server (`spike_whoami`) that is also a minimal single-user OAuth 2.1
authorization server: PKCE S256, one pre-registered client, dynamic client registration,
refresh-token rotation, and a password page instead of a login. It also accepts one fixed
bearer token. `tests/test_spike_auth.py` walks the same flow Claude's connector does, so the
server side is proven; what's left is Claude's side, which needs a real public URL.

What Claude's docs say (claude.com/docs/connectors, read 2026-10-06):

- Custom connectors on Free/Pro/Max: Customize > Connectors > Add custom connector, with an
  optional OAuth client ID and secret. Claude always sends PKCE S256 and uses the callback
  `https://claude.ai/api/mcp/auth_callback`.
- Discovery starts from a 401 whose `WWW-Authenticate` points at
  `/.well-known/oauth-protected-resource/...`; the MCP SDK does this for us.
- A fixed bearer token is possible through "Request headers" in the same dialog, but that is
  a beta available to a limited set of accounts. If the section isn't in your dialog, OAuth
  is the only option.

### Run it (about 10 minutes, on any machine with uv)

1. Install `cloudflared` (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/).
   A quick tunnel needs no Cloudflare account or domain.
2. Terminal 1: `cloudflared tunnel --url http://localhost:8000` and copy the
   `https://<random>.trycloudflare.com` URL it prints.
3. Terminal 2, from the repo root (PowerShell: use `$env:NAME="value"` lines instead):

   ```bash
   export SPIKE_PUBLIC_URL=https://<random>.trycloudflare.com
   export SPIKE_PASSWORD=pick-anything
   export SPIKE_CLIENT_ID=chomp-spike
   export SPIKE_CLIENT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(24))")
   export SPIKE_BEARER=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
   echo "secret: $SPIKE_CLIENT_SECRET  bearer: $SPIKE_BEARER"
   uv run python spikes/connector_auth.py
   ```

4. In Claude (web or desktop): Customize > Connectors > Add custom connector.
   URL `https://<random>.trycloudflare.com/mcp`. Under the OAuth / Advanced settings enter
   client ID `chomp-spike` and the secret printed above. Add, then Connect. A "Connect
   Claude to Chomp (spike)" page opens; enter your password and click Allow.
5. Start a chat, turn the connector on from the + menu, and ask Claude to call
   `spike_whoami`. Expected: `"via": "oauth"`.
6. Note whether the Add custom connector dialog has a **Request headers** section. If it
   does, add a second connector with the same URL, choose **No sign-in**, and add header
   `authorization` with value `Bearer <the bearer printed above>`. `spike_whoami` should
   then say `"via": "static bearer"`.
7. Stop both terminals and remove the spike connectors. The quick-tunnel URL dies with it.

Report back: did step 5 work, does the dialog show Request headers, and did step 6 work.

### Finding for M3 already

Bound to 127.0.0.1, the MCP SDK only accepts `Host: localhost` and answers **421** to
anything else. A tunnel forwards the public hostname, so Chomp must list its public host
in `TransportSecuritySettings.allowed_hosts` (see `security()` here) rather than turning
the check off.
