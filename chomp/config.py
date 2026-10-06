"""Runtime settings, read from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
# Open Food Facts asks every client to identify itself with an app name and contact.
DEFAULT_OFF_USER_AGENT = "Chomp/0.1 (self-hosted meal log; https://github.com/willypete10ag/chomp)"


def _bundled(name: str) -> Path:
    """A wheel install carries SCHEMA.sql and migrations/ inside the package (see
    pyproject force-include); a source checkout keeps them in the repo root."""
    inside = PACKAGE_DIR / name
    return inside if inside.exists() else REPO_ROOT / name


@dataclass(frozen=True)
class Settings:
    db_path: str
    tz: str
    host: str
    port: int
    schema_path: Path
    migrations_dir: Path
    fdc_api_key: str = field(default="", repr=False)
    off_user_agent: str = DEFAULT_OFF_USER_AGENT

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            db_path=os.environ.get("DB_PATH", "./chomp.db"),
            tz=os.environ.get("TZ", "America/New_York"),
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")),
            schema_path=Path(os.environ.get("SCHEMA_PATH", _bundled("SCHEMA.sql"))),
            migrations_dir=Path(os.environ.get("MIGRATIONS_DIR", _bundled("migrations"))),
            fdc_api_key=os.environ.get("FDC_API_KEY", ""),
            off_user_agent=os.environ.get("OFF_USER_AGENT") or DEFAULT_OFF_USER_AGENT,
        )
