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
