"""Configuration from environment variables. No secrets live in code."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


@dataclass(frozen=True)
class Settings:
    workspace: str  # "live" or "demo"
    database_url: str
    host: str
    port: int
    http_timeout_s: float
    max_fetch_bytes: int

    @property
    def is_demo(self) -> bool:
        return self.workspace == "demo"


def get_settings() -> Settings:
    _load_dotenv()
    workspace = os.environ.get("DEAL_HUNTER_WORKSPACE", "live").lower()
    if workspace not in ("live", "demo"):
        raise ValueError("DEAL_HUNTER_WORKSPACE must be 'live' or 'demo'")
    default_db = f"sqlite:///{ROOT / 'data' / (workspace + '.sqlite')}"
    url = os.environ.get("DATABASE_URL_DEMO" if workspace == "demo" else "DATABASE_URL", default_db)
    return Settings(
        workspace=workspace,
        database_url=url,
        host=os.environ.get("DEAL_HUNTER_HOST", "127.0.0.1"),
        port=int(os.environ.get("DEAL_HUNTER_PORT", "8000")),
        http_timeout_s=float(os.environ.get("DEAL_HUNTER_HTTP_TIMEOUT", "8")),
        max_fetch_bytes=int(os.environ.get("DEAL_HUNTER_MAX_FETCH_BYTES", str(2_000_000))),
    )
