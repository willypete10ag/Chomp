"""Spike for #23: does Claude's custom connector accept Chomp-style auth?

Throwaway. Runs a one-tool MCP server that is its own minimal OAuth 2.1 authorization
server (PKCE S256, a pre-registered client, dynamic client registration, refresh-token
rotation, a password-protected consent page) and also accepts one static bearer token.
Nothing here touches Chomp's database. See spikes/README.md for how to run it.

Env:
  SPIKE_PUBLIC_URL     https URL the tunnel gives you, no trailing slash (required)
  SPIKE_PASSWORD       consent-page password (required)
  SPIKE_CLIENT_ID      pre-registered OAuth client id (default chomp-spike)
  SPIKE_CLIENT_SECRET  its secret (optional; blank = public client)
  SPIKE_BEARER         static token accepted as "Authorization: Bearer <token>" (optional)
  PORT                 default 8000
"""

from __future__ import annotations

import hmac
import html
import logging
import os
import secrets
import time
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

log = logging.getLogger("spike")

ACCESS_TTL_S = 3600
CODE_TTL_S = 300
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


class SpikeProvider:
    """In-memory, single-user OAuth authorization server."""

    def __init__(self, public_url: str, client_id: str, client_secret: str | None, bearer: str):
        self.public_url = public_url
        self.bearer = bearer
        self.clients: dict[str, OAuthClientInformationFull] = {
            client_id: OAuthClientInformationFull(
                client_id=client_id,
                client_secret=client_secret or None,
                redirect_uris=[CLAUDE_CALLBACK],
                token_endpoint_auth_method="client_secret_post" if client_secret else "none",
                grant_types=["authorization_code", "refresh_token"],
                client_name="Claude (pre-registered)",
            )
        }
        self.pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        log.info("DCR: registered %s (%s)", client_info.client_id, client_info.client_name)
        self.clients[client_info.client_id] = client_info

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        session = secrets.token_urlsafe(24)
        self.pending[session] = (client, params)
        log.info("authorize: client %s -> consent page", client.client_id)
        return f"{self.public_url}/consent?{urlencode({'session': session})}"

    def approve(self, session: str) -> str | None:
        """Issue a code for an approved consent session; returns the redirect URL."""
        item = self.pending.pop(session, None)
        if item is None:
            return None
        client, params = item
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or ["chomp"],
            expires_at=time.time() + CODE_TTL_S,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="will",
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, authorization_code: str):
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.access[access] = AccessToken(
            token=access,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + ACCESS_TTL_S,
            resource=resource,
            subject="will",
        )
        self.refresh[refresh] = RefreshToken(
            token=refresh, client_id=client_id, scopes=scopes, resource=resource, subject="will"
        )
        log.info("issued tokens to %s", client_id)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL_S,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode):
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token: str):
        token = self.refresh.get(refresh_token)
        return token if token and token.client_id == client.client_id else None

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes):
        self.refresh.pop(refresh_token.token, None)  # rotate
        log.info("refresh for %s", client.client_id)
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self.bearer and hmac.compare_digest(token, self.bearer):
            return AccessToken(token=token, client_id="static-bearer", scopes=["chomp"])
        found = self.access.get(token)
        if found and found.expires_at and found.expires_at < time.time():
            return None
        return found

    async def revoke_token(self, token) -> None:
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)


CONSENT_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chomp spike sign-in</title>
<body style="font-family:system-ui;max-width:28rem;margin:3rem auto;padding:0 1rem">
<h1>Connect Claude to Chomp (spike)</h1>
<p>Client: <b>{client}</b><br>Will redirect to: <b>{redirect}</b></p>
{error}
<form method="post" action="/consent">
<input type="hidden" name="session" value="{session}">
<label>Password <input type="password" name="password" autofocus></label>
<button type="submit">Allow</button>
</form></body>"""


def build(env: dict[str, str] | None = None) -> tuple[MCPServer, SpikeProvider]:
    env = dict(os.environ if env is None else env)
    public = env["SPIKE_PUBLIC_URL"].rstrip("/")
    password = env["SPIKE_PASSWORD"]
    provider = SpikeProvider(
        public,
        env.get("SPIKE_CLIENT_ID") or "chomp-spike",
        env.get("SPIKE_CLIENT_SECRET") or None,
        env.get("SPIKE_BEARER", ""),
    )
    server = MCPServer(
        name="chomp-spike",
        auth=AuthSettings(
            issuer_url=public,
            resource_server_url=f"{public}/mcp",
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["chomp"], default_scopes=["chomp"]
            ),
            required_scopes=[],
        ),
        auth_server_provider=provider,
    )

    def page(session: str, error: str = "") -> HTMLResponse:
        client, params = provider.pending[session]
        return HTMLResponse(
            CONSENT_PAGE.format(
                client=html.escape(client.client_name or client.client_id),
                redirect=html.escape(str(params.redirect_uri)),
                session=html.escape(session),
                error=f'<p style="color:#b00">{html.escape(error)}</p>' if error else "",
            )
        )

    @server.custom_route("/consent", methods=["GET", "POST"])
    async def consent(request: Request) -> Response:
        if request.method == "GET":
            session = request.query_params.get("session", "")
            if session not in provider.pending:
                return HTMLResponse("Unknown or expired sign-in. Start again from Claude.", 400)
            return page(session)
        form = await request.form()
        session = str(form.get("session", ""))
        if session not in provider.pending:
            return HTMLResponse("Unknown or expired sign-in. Start again from Claude.", 400)
        if not hmac.compare_digest(str(form.get("password", "")), password):
            log.warning("consent: wrong password")
            return page(session, "Wrong password.")
        return RedirectResponse(provider.approve(session), status_code=302)

    @server.tool()
    def spike_whoami() -> dict:
        """Report that the Chomp auth spike connector works. Returns no nutrition data."""
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        return {
            "ok": True,
            "authenticated_as": token.client_id if token else None,
            "via": "static bearer" if token and token.client_id == "static-bearer" else "oauth",
        }

    return server, provider


def security(public_url: str) -> TransportSecuritySettings:
    """Bound to 127.0.0.1, the SDK only accepts Host: localhost and answers 421 to anything
    else. A tunnel forwards the public Host header, so allow it explicitly."""
    host = public_url.split("://", 1)[1].split("/", 1)[0]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[f"https://{host}", "https://claude.ai", "http://localhost:*"],
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server, _ = build()
    server.run(
        "streamable-http",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
        transport_security=security(os.environ["SPIKE_PUBLIC_URL"]),
    )


if __name__ == "__main__":
    main()
