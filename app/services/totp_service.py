"""TOTP 2FA — setup, verification, recovery codes."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

import pyotp
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.passwords import hash_password, verify_password
from app.api.errors import bad_request, forbidden
from app.infrastructure.crypto import decrypt_dsn, encrypt_dsn
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.user import User

if TYPE_CHECKING:
    pass

RECOVERY_CODE_COUNT = 8
RECOVERY_CODE_LENGTH = 10
ISSUER_NAME = "Pulse"


def user_totp_enabled(user: User) -> bool:
    return user.totp_enabled_at is not None and bool(user.totp_secret_enc)


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def build_otpauth_uri(*, secret: str, email: str) -> str:
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=ISSUER_NAME)


def _decrypt_secret(user: User) -> str:
    if not user.totp_secret_enc:
        raise bad_request("BAD_REQUEST", "Two-factor authentication is not configured")
    return decrypt_dsn(user.totp_secret_enc)


def verify_totp_code(user: User, code: str) -> bool:
    secret = _decrypt_secret(user)
    normalized = (code or "").strip().replace(" ", "")
    if not normalized:
        return False
    totp = pyotp.TOTP(secret)
    if totp.verify(normalized, valid_window=1):
        return True
    return _verify_recovery_code(user, normalized)


def _verify_recovery_code(user: User, code: str) -> bool:
    stored = list(user.totp_recovery_codes or [])
    if not stored:
        return False
    for idx, hashed in enumerate(stored):
        if verify_password(code, hashed):
            stored.pop(idx)
            user.totp_recovery_codes = stored
            return True
    return False


def _generate_recovery_codes() -> tuple[list[str], list[str]]:
    plain: list[str] = []
    hashed: list[str] = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = secrets.token_hex(RECOVERY_CODE_LENGTH // 2).upper()
        plain.append(raw)
        hashed.append(hash_password(raw))
    return plain, hashed


async def begin_totp_setup(user: User) -> tuple[str, str]:
    """Store pending secret (not enabled until verified). Returns secret + otpauth URI."""
    secret = generate_totp_secret()
    user.totp_secret_enc = encrypt_dsn(secret)
    user.totp_enabled_at = None
    user.totp_recovery_codes = None
    return secret, build_otpauth_uri(secret=secret, email=user.email)


async def enable_totp(user: User, code: str) -> list[str]:
    if not user.totp_secret_enc:
        raise bad_request("BAD_REQUEST", "Call setup first before enabling two-factor authentication")
    secret = _decrypt_secret(user)
    normalized = (code or "").strip().replace(" ", "")
    if not pyotp.TOTP(secret).verify(normalized, valid_window=1):
        raise bad_request("INVALID_TOTP", "Invalid verification code")
    plain, hashed = _generate_recovery_codes()
    user.totp_recovery_codes = hashed
    user.totp_enabled_at = datetime.now(timezone.utc)
    return plain


async def disable_totp(
    user: User,
    org: Organization | None,
    *,
    code: str,
    password: str | None = None,
) -> None:
    if org and org.require_2fa:
        raise forbidden(
            "TWO_FACTOR_REQUIRED_BY_ORG",
            "Your organization requires two-factor authentication.",
        )
    if not user_totp_enabled(user):
        return
    if user.password_hash:
        if not password or not verify_password(password, user.password_hash):
            raise bad_request("INVALID_PASSWORD", "Password is required to disable two-factor authentication")
    if not verify_totp_code(user, code):
        raise bad_request("INVALID_TOTP", "Invalid verification code")
    user.totp_secret_enc = None
    user.totp_enabled_at = None
    user.totp_recovery_codes = None


def clear_totp_fields(user: User) -> None:
    user.totp_secret_enc = None
    user.totp_enabled_at = None
    user.totp_recovery_codes = None
