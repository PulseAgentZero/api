"""Offline verification of `plc_…` Pulse license JWTs (LICENSE_SYSTEM.md)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import jwt

from app.config.settings import settings

logger = logging.getLogger(__name__)


def normalize_license_public_key(pem: str) -> str:
    return pem.strip().replace("\\n", "\n")


def strip_plc_prefix(raw_key: str) -> str:
    s = (raw_key or "").strip()
    if s.startswith("plc_"):
        return s[4:]
    return s


def decode_license_jwt_payload(raw_key: str) -> dict[str, Any]:
    """Verify RS256 signature and return JWT payload.

    Raises jwt.PyJWTError if verification fails or public key is not configured.
    """
    pem = settings.PULSE_LICENSE_PUBLIC_KEY
    if not pem:
        raise jwt.InvalidKeyError("PULSE_LICENSE_PUBLIC_KEY is not configured")
    key = normalize_license_public_key(pem)
    token = strip_plc_prefix(raw_key)
    options = {"verify_aud": False}
    kwargs: dict[str, Any] = {"algorithms": ["RS256"], "options": options}
    iss = settings.LICENSE_JWT_ISSUER
    if iss:
        kwargs["issuer"] = iss
    return jwt.decode(token, key, **kwargs)


def jwt_expired(payload: dict[str, Any], *, now: datetime | None = None) -> bool:
    exp = payload.get("exp")
    if exp is None:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        exp_ts = int(float(exp))
    except (TypeError, ValueError):
        return True
    return datetime.fromtimestamp(exp_ts, tz=timezone.utc) < now
