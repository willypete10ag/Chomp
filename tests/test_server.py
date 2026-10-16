from __future__ import annotations

import pytest
from mcp import Client
from starlette.testclient import TestClient

from chomp import __version__
from chomp.server import build_server


@pytest.fixture
def server(settings, conn):
    # `conn` has already migrated the DB at settings.db_path.
    return build_server(settings)


@pytest.mark.anyio
async def test_lists_and_calls_server_info(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "connector is reachable" in tools["server_info"].description
        result = await client.call_tool("server_info", {})
    assert not result.is_error
    assert result.structured_content == {
        "name": "chomp",
        "version": __version__,
        "schema_version": 20,
        "tz": "America/New_York",
        "user": "owner",
    }


def test_instructions_forbid_estimates(server):
    assert "Never estimate" in server.instructions


def test_health_route(server):
    with TestClient(server.streamable_http_app()) as http:
        r = http.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "schema_version": 20}


def test_health_route_reports_db_failure(settings, tmp_path):
    from dataclasses import replace

    broken = replace(settings, db_path=str(tmp_path / "missing-dir" / "x.db"))
    with TestClient(build_server(broken).streamable_http_app()) as http:
        r = http.get("/health")
    assert r.status_code == 503
    assert r.json() == {"status": "error"}


def test_default_paths_resolve_in_checkout(monkeypatch):
    from chomp.config import Settings

    for var in ("SCHEMA_PATH", "MIGRATIONS_DIR"):
        monkeypatch.delenv(var, raising=False)
    s = Settings.from_env()
    assert s.schema_path.is_file()
    assert s.migrations_dir.is_dir()


def test_health_rejects_uninitialized_db(settings, tmp_path):
    from dataclasses import replace

    empty = replace(settings, db_path=str(tmp_path / "empty.db"))
    with TestClient(build_server(empty).streamable_http_app()) as http:
        r = http.get("/health")
    assert r.status_code == 503
    assert r.json() == {"status": "error"}
