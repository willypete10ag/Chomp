from __future__ import annotations

from pathlib import Path

import pytest

from chomp import db
from chomp.config import REPO_ROOT, Settings

SCHEMA = REPO_ROOT / "SCHEMA.sql"
MIGRATIONS = REPO_ROOT / "migrations"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "chomp.db"),
        tz="America/New_York",
        host="127.0.0.1",
        port=8000,
        schema_path=SCHEMA,
        migrations_dir=MIGRATIONS,
    )


@pytest.fixture
def conn(settings: Settings):
    c = db.open_db(settings.db_path, settings.schema_path, settings.migrations_dir)
    yield c
    c.close()


FIXTURES = Path(__file__).parent / "fixtures"


def fixture_json(name: str):
    import json

    return json.loads((FIXTURES / name).read_text())


class FakeUpstream:
    """Routes httpx requests to canned responses and records what was asked."""

    def __init__(self) -> None:
        self.routes: list[tuple] = []
        self.requests: list = []

    def add(self, path: str, response, *, status: int = 200, match: dict | None = None):
        """Respond to `path` (and query params containing `match`) with JSON `response`.

        `response` may be a list to return successive responses (e.g. a 429 then a 200)."""
        self.routes.append(
            (path, match or {}, response if isinstance(response, list) else None, response, status)
        )
        return self

    def handler(self, request):
        import httpx

        self.requests.append(request)
        for path, match, seq, response, status in self.routes:
            if request.url.path != path:
                continue
            if any(request.url.params.get(k) != v for k, v in match.items()):
                continue
            if seq is not None:
                item = seq.pop(0) if len(seq) > 1 else seq[0]
                code, body = item
                return httpx.Response(code, json=body)
            return httpx.Response(status, json=response)
        return httpx.Response(404, json={"error": "not found"})

    def client(self):
        import httpx

        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def fdc(upstream):
    from chomp.sources.fdc import FDCClient

    return FDCClient("test-key", http=upstream.client(), backoff_s=0)
