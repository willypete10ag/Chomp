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
    # Endpoint auth (see chomp/auth.py). Blank public_url = local-only mode, no auth.
    public_url: str = ""
    auth_password: str = field(default="", repr=False)
    oauth_client_id: str = ""
    oauth_client_secret: str = field(default="", repr=False)
    bearer_token: str = field(default="", repr=False)
    # Nightly backups (see chomp/backup.py)
    backup_dir: str = "./backups"
    backup_keep: int = 14
    backup_at: str = "03:30"
    # Food photos (#74); blank = a photos/ folder next to the database
    photos_dir: str = ""

    @property
    def photos_path(self) -> Path:
        return Path(self.photos_dir) if self.photos_dir else Path(self.db_path).parent / "photos"

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
            public_url=os.environ.get("PUBLIC_URL", "").strip().rstrip("/"),
            auth_password=os.environ.get("AUTH_PASSWORD", ""),
            oauth_client_id=os.environ.get("OAUTH_CLIENT_ID", "").strip(),
            oauth_client_secret=os.environ.get("OAUTH_CLIENT_SECRET", ""),
            bearer_token=os.environ.get("BEARER_TOKEN", ""),
            backup_dir=os.environ.get("BACKUP_DIR", "./backups"),
            backup_keep=int(os.environ.get("BACKUP_KEEP", "14")),
            backup_at=os.environ.get("BACKUP_AT", "03:30"),
            photos_dir=os.environ.get("PHOTOS_DIR", ""),
        )
