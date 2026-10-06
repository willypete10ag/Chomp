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
        tools = await client.list_tools()
        assert [t.name for t in tools.tools] == ["server_info"]
        assert "connector is reachable" in tools.tools[0].description
        result = await client.call_tool("server_info", {})
    assert not result.is_error
    assert result.structured_content == {
        "name": "chomp",
        "version": __version__,
        "schema_version": 1,
        "tz": "America/New_York",
    }


def test_instructions_forbid_estimates(server):
    assert "Never estimate" in server.instructions


def test_health_route(server):
    with TestClient(server.streamable_http_app()) as http:
        r = http.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "schema_version": 1}


def test_health_route_reports_db_failure(settings, tmp_path):
    from dataclasses import replace

    broken = replace(settings, db_path=str(tmp_path / "missing-dir" / "x.db"))
    with TestClient(build_server(broken).streamable_http_app()) as http:
        r = http.get("/health")
    assert r.status_code == 503
    assert r.json() == {"status": "error"}
