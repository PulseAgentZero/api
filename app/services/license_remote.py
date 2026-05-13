"""HTTP calls to the Pulse license server."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx

from app.config.settings import settings

logger = logging.getLogger(__name__)


async def post_validate_license(
    license_key: str,
    org_id: UUID,
    *,
    version: str = "1.0.0",
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """POST /validate. Returns (status_code, json_or_none, error_text)."""
    url = f"{settings.LICENSE_SERVER_URL}/validate"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                json={
                    "license_key": license_key,
                    "org_id": str(org_id),
                    "version": version,
                },
            )
    except httpx.RequestError as exc:
        logger.warning("License server unreachable: %s", exc)
        return 0, None, str(exc)

    data: dict[str, Any] | None = None
    if resp.content:
        try:
            data = resp.json()
        except Exception:
            data = None
    return resp.status_code, data, None if resp.status_code < 400 else (resp.text or "error")
