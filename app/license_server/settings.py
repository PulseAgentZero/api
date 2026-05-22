"""License server configuration (env-only, no Pulse app settings dependency)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_project_root / ".env")

DEFAULT_SELF_HOSTED_FEATURES = (
    "audit_log",
    "sso",
    "white_label",
    "priority_support",
    "log_streaming",
    "ldap_sync",
    "high_concurrency",
)
DEFAULT_PLAN = "pro"
DEFAULT_SEAT_LIMIT: int | None = None
DEFAULT_LICENSE_LIMITS: dict[str, int] = {"concurrent_pipeline_runs": 5}
LICENSE_VALIDITY_DAYS = int(os.getenv("LICENSE_VALIDITY_DAYS", "365"))


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is required for the license server")
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def get_signing_private_key() -> str:
    key = (os.getenv("LICENSE_SIGNING_PRIVATE_KEY") or "").strip()
    if not key:
        raise RuntimeError("LICENSE_SIGNING_PRIVATE_KEY is not configured")
    return key.replace("\\n", "\n")


def get_api_key() -> str | None:
    key = (os.getenv("LICENSE_SERVER_API_KEY") or "").strip()
    return key or None


def get_jwt_issuer() -> str:
    return (os.getenv("LICENSE_JWT_ISSUER") or "https://license.entivia.online").strip()
