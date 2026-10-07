"""Endpoint auth (#24): a minimal single-user OAuth 2.1 authorization server plus an optional
static bearer token, both checked by the MCP SDK's bearer middleware on /mcp.

OAuth: one pre-registered client (shared by Claude and ChatGPT), PKCE S256 (enforced by the SDK),
a password-protected consent page instead of a user login, and refresh-token rotation.
Issued tokens are stored hashed in SQLite (migration 0003) so a restart doesn't sign connectors
out. Authorization codes and pending consent pages live in memory; they last minutes.

With PUBLIC_URL unset Chomp runs in local-only mode: no auth, loopback bind only.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import secrets
import time
from contextlib import closing
from ipaddress import ip_address
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from chomp import db
from chomp.config import Settings

log = logging.getLogger("chomp.auth")

SCOPE = "chomp"
SUBJECT = "owner"
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 90 * 86400  # rotation resets it, so only 90 idle days force a new sign-in
CODE_TTL_S = 300
CONSENT_TTL_S = 600
MAX_PENDING = 20
# Consent-page lockout: after this many wrong passwords within the window, refuse every
# attempt until the window passes. Single user, so one global counter is enough.
MAX_FAILURES = 5
LOCKOUT_S = 900
MIN_PASSWORD_LEN = 12
MIN_SECRET_LEN = 32
CONNECTOR_CALLBACKS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector/oauth/e3AEiu3ZZRkR",
)
STATIC_CLIENT = "static-bearer"


class AuthConfigError(ValueError):
    pass


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def check_settings(settings: Settings) -> bool:
    """Validate the auth settings. Returns True when auth is on (PUBLIC_URL set).

    Refuses to start an unauthenticated server on anything but loopback, and refuses weak
    or missing secrets once Chomp is public."""
    if not settings.public_url:
        if not is_loopback(settings.host):
            raise AuthConfigError(
                f"HOST={settings.host} is reachable from other machines, so auth is required: "
                "set PUBLIC_URL, AUTH_PASSWORD, OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET"
            )
        return False
    url = urlsplit(settings.public_url)
    if url.scheme != "https" or not url.hostname or url.path not in ("", "/"):
        raise AuthConfigError("PUBLIC_URL must be https://<hostname> with no path")
    problems = []
    if len(settings.auth_password) < MIN_PASSWORD_LEN:
        problems.append(f"AUTH_PASSWORD must be at least {MIN_PASSWORD_LEN} characters")
    if not settings.oauth_client_id:
        problems.append("OAUTH_CLIENT_ID is required")
    if len(settings.oauth_client_secret) < MIN_SECRET_LEN:
        problems.append(f"OAUTH_CLIENT_SECRET must be at least {MIN_SECRET_LEN} characters")
    if settings.bearer_token and len(settings.bearer_token) < MIN_SECRET_LEN:
        problems.append(f"BEARER_TOKEN must be blank or at least {MIN_SECRET_LEN} characters")
    if problems:
        raise AuthConfigError("; ".join(problems))
    return True


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ChompAuthProvider:
    """Single-user OAuth authorization server backed by Chomp's SQLite database."""

    def __init__(self, settings: Settings):
        self.db_path = settings.db_path
        self.password = settings.auth_password
        self.bearer = settings.bearer_token
        self.client = OAuthClientInformationFull(
            client_id=settings.oauth_client_id,
            client_secret=settings.oauth_client_secret,
            redirect_uris=list(CONNECTOR_CALLBACKS),
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=SCOPE,
            client_name="Chomp connector",
        )
        self.consent_base = f"{settings.public_url}/consent"
        self.pending: dict[str, tuple[float, AuthorizationParams]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.failures: list[float] = []

    def _connect(self):
        return closing(db.connect(self.db_path))

    # --- clients (one, pre-registered; dynamic registration is off) ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if hmac.compare_digest(client_id, self.client.client_id):
            return self.client
        return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("dynamic client registration is disabled")

    # --- authorization: hand off to the consent page ---

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v[0] > now}
        if len(self.pending) >= MAX_PENDING:  # drop the oldest rather than grow without bound
            del self.pending[min(self.pending, key=lambda k: self.pending[k][0])]
        session = secrets.token_urlsafe(24)
        self.pending[session] = (now + CONSENT_TTL_S, params)
        return f"{self.consent_base}?{urlencode({'session': session})}"

    def pending_params(self, session: str) -> AuthorizationParams | None:
        item = self.pending.get(session)
        if item is None or item[0] < time.time():
            return None
        return item[1]

    def locked_out(self) -> bool:
        cutoff = time.time() - LOCKOUT_S
        self.failures = [t for t in self.failures if t > cutoff]
        return len(self.failures) >= MAX_FAILURES

    def check_password(self, password: str) -> bool:
        if hmac.compare_digest(password.encode(), self.password.encode()):
            return True
        self.failures.append(time.time())
        return False

    def approve(self, session: str) -> str | None:
        """Issue a code for an approved consent session; returns Claude's redirect URL."""
        item = self.pending.pop(session, None)
        if item is None or item[0] < time.time():
            return None
        _, params = item
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + CODE_TTL_S,
            client_id=self.client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=SUBJECT,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        now = time.time()
        self.codes = {k: v for k, v in self.codes.items() if v.expires_at > now}
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    # --- tokens ---

    def _issue(
        self, client_id: str, scopes: list[str], resource: str | None, grant_id: str
    ) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = int(time.time())
        rows = [
            (_hash(access), "access", grant_id, client_id, " ".join(scopes), resource,
             now + ACCESS_TTL_S),
            (_hash(refresh), "refresh", grant_id, client_id, " ".join(scopes), resource,
             now + REFRESH_TTL_S),
        ]  # fmt: skip
        with self._connect() as conn, db.transaction(conn):
            conn.execute("DELETE FROM oauth_tokens WHERE grant_id = ?", (grant_id,))
            conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (now,))
            conn.executemany(
                "INSERT INTO oauth_tokens"
                " (token_hash, kind, grant_id, client_id, scopes, resource, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL_S,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    def _load(self, token: str, kind: str):
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = ? AND expires_at > ?",
                (_hash(token), kind, int(time.time())),
            ).fetchone()

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.codes.pop(authorization_code.code, None)
        log.info("connector signed in")
        return self._issue(
            client.client_id,
            authorization_code.scopes,
            authorization_code.resource,
            secrets.token_urlsafe(16),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self._load(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject=SUBJECT,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        row = self._load(refresh_token.token, "refresh")
        grant_id = row["grant_id"] if row else secrets.token_urlsafe(16)
        # _issue replaces every token in the grant, so the old refresh token stops working.
        return self._issue(
            client.client_id, scopes or refresh_token.scopes, refresh_token.resource, grant_id
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self.bearer and hmac.compare_digest(token.encode(), self.bearer.encode()):
            return AccessToken(token=token, client_id=STATIC_CLIENT, scopes=[SCOPE])
        row = self._load(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject=SUBJECT,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if token.client_id == STATIC_CLIENT:
            return  # the static bearer is revoked by changing BEARER_TOKEN
        with self._connect() as conn, db.transaction(conn):
            conn.execute(
                "DELETE FROM oauth_tokens WHERE grant_id IN"
                " (SELECT grant_id FROM oauth_tokens WHERE token_hash = ?)",
                (_hash(token.token),),
            )


CONSENT_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect to Chomp</title>
<body style="font-family:system-ui;max-width:28rem;margin:3rem auto;padding:0 1rem">
<h1>Connect to Chomp</h1>
<p>This connector is asking to read and write your meal log.<br>
After you allow it, you'll return to <b>{redirect}</b>.</p>
{error}
<form method="post" action="/consent">
<input type="hidden" name="session" value="{session}">
<p><label>Chomp password<br><input type="password" name="password" autofocus
 autocomplete="current-password" style="width:100%;padding:.5rem"></label></p>
<button type="submit" style="padding:.5rem 1.5rem">Allow</button>
</form></body>"""
EXPIRED = "This sign-in expired or is unknown. Start again from your connector settings."
LOCKED = "Too many wrong passwords. Wait 15 minutes, then start again from your connector."
NO_STORE = {"Cache-Control": "no-store", "X-Frame-Options": "DENY"}


def add_consent_route(server: MCPServer, provider: ChompAuthProvider) -> None:
    def page(session: str, params: AuthorizationParams, error: str = "") -> HTMLResponse:
        return HTMLResponse(
            CONSENT_PAGE.format(
                redirect=html.escape(urlsplit(str(params.redirect_uri)).netloc),
                session=html.escape(session),
                error=f'<p style="color:#b00">{html.escape(error)}</p>' if error else "",
            ),
            headers=NO_STORE,
        )

    @server.custom_route("/consent", methods=["GET", "POST"])
    async def consent(request: Request) -> Response:
        if request.method == "GET":
            session = request.query_params.get("session", "")
        else:
            form = await request.form()
            session = str(form.get("session", ""))
        params = provider.pending_params(session)
        if params is None:
            return HTMLResponse(EXPIRED, status_code=400, headers=NO_STORE)
        if provider.locked_out():
            return HTMLResponse(LOCKED, status_code=429, headers=NO_STORE)
        if request.method == "GET":
            return page(session, params)
        if not provider.check_password(str(form.get("password", ""))):
            log.warning("consent: wrong password")
            if provider.locked_out():
                return HTMLResponse(LOCKED, status_code=429, headers=NO_STORE)
            return page(session, params, "Wrong password.")
        redirect = provider.approve(session)
        if redirect is None:
            return HTMLResponse(EXPIRED, status_code=400, headers=NO_STORE)
        return RedirectResponse(redirect, status_code=302, headers=NO_STORE)


def auth_settings(settings: Settings) -> AuthSettings:
    return AuthSettings(
        issuer_url=settings.public_url,
        resource_server_url=f"{settings.public_url}/mcp",
        # The provider stores each token's resource; the SDK compares it to /mcp.
        validate_token_resource=False,
        revocation_options=RevocationOptions(enabled=True),
        # Like the #23 spike Claude connected to: no scope is advertised or required. The
        # client may still ask for "chomp"; every token gets it.
        required_scopes=[],
    )


def transport_security(settings: Settings) -> TransportSecuritySettings | None:
    """Behind the tunnel the Host header is the public hostname. The SDK's DNS-rebinding
    check only allows localhost by default (421 otherwise), so name the public host."""
    if not settings.public_url:
        return None  # the SDK's loopback defaults apply
    host = urlsplit(settings.public_url).netloc
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[
            f"https://{host}",
            "https://claude.ai",
            "https://claude.com",
            "http://localhost:*",
        ],
    )
