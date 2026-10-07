"""#24: endpoint auth. Walks the flow Claude's connector performs against Chomp itself:
401 + metadata discovery -> /authorize with PKCE S256 -> password consent -> /token ->
MCP call -> refresh with rotation, plus the static bearer, lockout and config checks."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from chomp import auth, db
from chomp.server import build_server

PUBLIC = "https://chomp.example.com"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CLIENT_ID = "claude-chomp"
CLIENT_SECRET = "c" * 40
PASSWORD = "correct horse battery"
BEARER = "b" * 43
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def public_settings(settings):
    s = dataclasses.replace(
        settings,
        host="0.0.0.0",
        public_url=PUBLIC,
        auth_password=PASSWORD,
        oauth_client_id=CLIENT_ID,
        oauth_client_secret=CLIENT_SECRET,
        bearer_token=BEARER,
    )
    with db.open_db(s.db_path, s.schema_path, s.migrations_dir):
        pass
    return s


def make_client(s):
    app = build_server(s).streamable_http_app(
        host=s.host, transport_security=auth.transport_security(s)
    )
    return TestClient(app, base_url=PUBLIC)


@pytest.fixture
def http(public_settings):
    with make_client(public_settings) as client:
        yield client


def pkce():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def start(http, challenge, client_id=CLIENT_ID):
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": f"{PUBLIC}/mcp",
        },
        follow_redirects=False,
    )
    return r


def consent(http, challenge, password=PASSWORD):
    r = start(http, challenge)
    assert r.status_code == 302, r.text
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{PUBLIC}/consent"
    session = parse_qs(loc.query)["session"][0]
    page = http.get(f"/consent?session={session}")
    assert (
        page.status_code == 200
        and "Chomp password" in page.text
        and urlparse(CALLBACK).netloc in page.text
    )
    return http.post(
        "/consent", data={"session": session, "password": password}, follow_redirects=False
    )


def sign_in(http):
    verifier, challenge = pkce()
    r = consent(http, challenge)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyz"]
    tok = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": CALLBACK,
            "code_verifier": verifier,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    assert tok.status_code == 200, tok.text
    return tok.json()


def mcp(http, token):
    return http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})


def refresh(http, refresh_token):
    return http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )


def test_unauthenticated_call_gets_401_with_metadata_pointer(http):
    r = http.post("/mcp", json=INIT, headers=MCP_HEADERS)
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    meta = http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == f"{PUBLIC}/mcp"
    asm = http.get("/.well-known/oauth-authorization-server").json()
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert "registration_endpoint" not in asm  # dynamic registration is off


def test_wrong_bearer_gets_401(http):
    assert mcp(http, "not-a-token").status_code == 401


def test_health_is_only_for_the_local_container_check(public_settings, http):
    assert http.get("/health").status_code == 404  # a request from the tunnel
    app = build_server(public_settings).streamable_http_app(
        host="0.0.0.0", transport_security=auth.transport_security(public_settings)
    )
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 5000)) as local:
        assert local.get("/health").json()["status"] == "ok"


@pytest.mark.parametrize("callback", auth.CONNECTOR_CALLBACKS)
def test_full_oauth_flow_with_refresh_rotation(http, monkeypatch, callback):
    monkeypatch.setitem(globals(), "CALLBACK", callback)
    tok = sign_in(http)
    assert mcp(http, tok["access_token"]).status_code == 200

    new = refresh(http, tok["refresh_token"])
    assert new.status_code == 200
    new = new.json()
    assert new["refresh_token"] != tok["refresh_token"]
    assert mcp(http, new["access_token"]).status_code == 200
    # Rotation retires the whole old pair.
    assert refresh(http, tok["refresh_token"]).json()["error"] == "invalid_grant"
    assert mcp(http, tok["access_token"]).status_code == 401


def test_tokens_survive_a_restart_and_are_stored_hashed(public_settings, http):
    tok = sign_in(http)
    with make_client(public_settings) as again:
        assert mcp(again, tok["access_token"]).status_code == 200
    with db.connect(public_settings.db_path) as conn:
        stored = {r[0] for r in conn.execute("SELECT token_hash FROM oauth_tokens")}
    assert tok["access_token"] not in stored and tok["refresh_token"] not in stored
    assert hashlib.sha256(tok["access_token"].encode()).hexdigest() in stored


def test_expired_access_token_is_refused(public_settings, http):
    tok = sign_in(http)
    with db.connect(public_settings.db_path) as conn:
        conn.execute("UPDATE oauth_tokens SET expires_at = 1 WHERE kind = 'access'")
    assert mcp(http, tok["access_token"]).status_code == 401


def test_revoke_ends_the_grant(http):
    tok = sign_in(http)
    r = http.post(
        "/revoke",
        data={
            "token": tok["refresh_token"],
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    assert r.status_code == 200
    assert mcp(http, tok["access_token"]).status_code == 401


def test_static_bearer_is_accepted(http):
    assert mcp(http, BEARER).status_code == 200


def test_bad_verifier_and_wrong_secret_are_refused(http):
    _verifier, challenge = pkce()
    r = consent(http, challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    base = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK,
        "client_id": CLIENT_ID,
    }
    bad = http.post("/token", data={**base, "code_verifier": "x" * 50, "client_secret": "nope"})
    assert bad.status_code in (400, 401)
    bad = http.post(
        "/token", data={**base, "code_verifier": "x" * 50, "client_secret": CLIENT_SECRET}
    )
    assert bad.status_code == 400


def test_unknown_client_and_foreign_redirect_are_refused(http):
    _verifier, challenge = pkce()
    assert start(http, challenge, client_id="someone-else").status_code == 400
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": "https://evil.example.com/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    reg = http.post("/register", json={"redirect_uris": [CALLBACK]})
    assert reg.status_code == 404


def test_wrong_passwords_lock_the_consent_page(http):
    _verifier, challenge = pkce()
    for _ in range(auth.MAX_FAILURES - 1):
        r = consent(http, challenge, password="wrong password!")
        assert r.status_code == 200 and "Wrong password" in r.text
    r = consent(http, challenge, password="wrong password!")
    assert r.status_code == 429
    r = start(http, challenge)
    session = parse_qs(urlparse(r.headers["location"]).query)["session"][0]
    r = http.post("/consent", data={"session": session, "password": PASSWORD})
    assert r.status_code == 429  # even the right password waits out the lockout


def test_unknown_consent_session_is_refused(http):
    assert http.get("/consent?session=nope").status_code == 400


def test_tunnel_host_is_allowed_but_other_hosts_are_not(public_settings):
    with make_client(public_settings) as client:
        r = client.post(
            "/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {BEARER}"}
        )
        assert r.status_code == 200
        r = client.post(
            "/mcp",
            json=INIT,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {BEARER}", "Host": "evil.example"},
        )
        assert r.status_code == 421


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"public_url": ""}, "auth is required"),
        ({"public_url": "http://chomp.example.com"}, "https"),
        ({"public_url": "https://chomp.example.com/mcp"}, "no path"),
        ({"auth_password": "short"}, "AUTH_PASSWORD"),
        ({"oauth_client_id": ""}, "OAUTH_CLIENT_ID"),
        ({"oauth_client_secret": "short"}, "OAUTH_CLIENT_SECRET"),
        ({"bearer_token": "short"}, "BEARER_TOKEN"),
    ],
)
def test_bad_auth_config_refuses_to_start(public_settings, changes, message):
    with pytest.raises(auth.AuthConfigError, match=message):
        auth.check_settings(dataclasses.replace(public_settings, **changes))


def test_local_mode_needs_no_auth_on_loopback(settings):
    assert auth.check_settings(settings) is False
    assert auth.transport_security(settings) is None
