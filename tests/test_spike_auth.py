"""#23 spike: walk the OAuth flow Claude's connector performs, against spikes/connector_auth.py.

Discovery (401 + protected resource metadata) -> authorization server metadata ->
/authorize with PKCE S256 -> password consent -> /token (form-encoded) -> MCP call ->
refresh with rotation. Also the static bearer path and DCR.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spikes"))
import connector_auth

PUBLIC = "https://spike.example.com"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
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
def http():
    server, _ = connector_auth.build(
        {
            "SPIKE_PUBLIC_URL": PUBLIC,
            "SPIKE_PASSWORD": "pw",
            "SPIKE_CLIENT_ID": "claude-chomp",
            "SPIKE_CLIENT_SECRET": "s3cret",
            "SPIKE_BEARER": "static-token",
        }
    )
    app = server.streamable_http_app(
        host="127.0.0.1", transport_security=connector_auth.security(PUBLIC)
    )
    with TestClient(app, base_url=PUBLIC) as client:
        yield client


def pkce():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize(http, client_id, challenge, password="pw"):
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
    assert r.status_code == 302, r.text
    consent = urlparse(r.headers["location"])
    assert consent.path == "/consent"
    session = parse_qs(consent.query)["session"][0]
    page = http.get(f"/consent?session={session}")
    assert "Password" in page.text and "claude.ai" in page.text
    return http.post(
        "/consent", data={"session": session, "password": password}, follow_redirects=False
    )


def test_unauthenticated_mcp_call_gets_401_with_metadata_pointer(http):
    r = http.post("/mcp", json=INIT, headers=MCP_HEADERS)
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    meta = http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == f"{PUBLIC}/mcp"
    assert meta["authorization_servers"] == [f"{PUBLIC}/"] or meta["authorization_servers"] == [
        PUBLIC
    ]
    asm = http.get("/.well-known/oauth-authorization-server").json()
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["registration_endpoint"].endswith("/register")


def test_full_flow_with_preregistered_client(http):
    verifier, challenge = pkce()
    r = authorize(http, "claude-chomp", challenge)
    assert r.status_code == 302
    back = urlparse(r.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
    q = parse_qs(back.query)
    assert q["state"] == ["xyz"]
    token = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": CALLBACK,
            "code_verifier": verifier,
            "client_id": "claude-chomp",
            "client_secret": "s3cret",
        },
    )
    assert token.status_code == 200, token.text
    tok = token.json()
    r = http.post(
        "/mcp",
        json=INIT,
        headers={**MCP_HEADERS, "Authorization": f"Bearer {tok['access_token']}"},
    )
    assert r.status_code == 200

    refreshed = http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": "claude-chomp",
            "client_secret": "s3cret",
        },
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] != tok["refresh_token"]
    reused = http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": "claude-chomp",
            "client_secret": "s3cret",
        },
    )
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"


def test_wrong_password_and_bad_verifier_are_refused(http):
    _verifier, challenge = pkce()
    r = authorize(http, "claude-chomp", challenge, password="nope")
    assert r.status_code == 200 and "Wrong password" in r.text

    _verifier, challenge = pkce()
    r = authorize(http, "claude-chomp", challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    bad = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "code_verifier": "x" * 50,
            "client_id": "claude-chomp",
            "client_secret": "s3cret",
        },
    )
    assert bad.status_code == 400


def test_dynamic_client_registration_flow(http):
    reg = http.post(
        "/register",
        json={
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "Claude",
        },
    )
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier, challenge = pkce()
    r = authorize(http, client_id, challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "code_verifier": verifier,
            "client_id": client_id,
        },
    )
    assert tok.status_code == 200, tok.text


def test_tunnel_host_is_rejected_without_allowlist():
    """What a tunnel to a localhost-bound server sees with the SDK defaults: 421."""
    server, _ = connector_auth.build(
        {"SPIKE_PUBLIC_URL": PUBLIC, "SPIKE_PASSWORD": "pw", "SPIKE_BEARER": "t"}
    )
    with TestClient(server.streamable_http_app(host="127.0.0.1"), base_url=PUBLIC) as client:
        auth = {**MCP_HEADERS, "Authorization": "Bearer t"}
        r = client.post("/mcp", json=INIT, headers=auth)
    assert r.status_code == 421


def test_static_bearer_is_accepted(http):
    r = http.post(
        "/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer static-token"}
    )
    assert r.status_code == 200
    r = http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer wrong"})
    assert r.status_code == 401
