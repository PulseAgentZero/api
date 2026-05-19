"""Issue and verify plc_* license JWTs."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization

from app.license_server.settings import (
    DEFAULT_PLAN,
    DEFAULT_SEAT_LIMIT,
    DEFAULT_SELF_HOSTED_FEATURES,
    LICENSE_VALIDITY_DAYS,
    get_jwt_issuer,
    get_signing_private_key,
)


def strip_plc_prefix(raw_key: str) -> str:
    s = (raw_key or "").strip()
    if s.startswith("plc_"):
        return s[4:]
    return s


def format_license_key(token: str) -> str:
    return f"plc_{token}" if not token.startswith("plc_") else token


@lru_cache(maxsize=1)
def _public_key_pem() -> str:
    private_pem = get_signing_private_key().encode()
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    public_key = private_key.public_key()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def issue_license_jwt(
    *,
    jti: str | None = None,
    plan: str = DEFAULT_PLAN,
    features: list[str] | None = None,
    seat_limit: int | None = DEFAULT_SEAT_LIMIT,
    expires_at: datetime | None = None,
) -> tuple[str, datetime, str]:
    """Return (full plc_* key, expires_at, jti)."""
    now = datetime.now(timezone.utc)
    if expires_at is None:
        expires_at = now + timedelta(days=LICENSE_VALIDITY_DAYS)
    jti_val = jti or str(uuid.uuid4())
    feat = list(features if features is not None else DEFAULT_SELF_HOSTED_FEATURES)
    payload: dict[str, Any] = {
        "jti": jti_val,
        "plan": plan,
        "features": feat,
        "sub": "self_hosted",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if seat_limit is not None:
        payload["seat_limit"] = seat_limit
    iss = get_jwt_issuer()
    if iss:
        payload["iss"] = iss
    private_key = get_signing_private_key()
    token = jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
    )
    if isinstance(token, bytes):
        token = token.decode()
    return format_license_key(token), expires_at, jti_val


def decode_license_jwt(raw_key: str) -> dict[str, Any]:
    token = strip_plc_prefix(raw_key)
    kwargs: dict[str, Any] = {"algorithms": ["RS256"], "options": {"verify_aud": False}}
    iss = get_jwt_issuer()
    if iss:
        kwargs["issuer"] = iss
    return jwt.decode(token, _public_key_pem(), **kwargs)
