"""FastAPI dependencies for the license server."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.session import async_session_factory
from app.license_server.settings import get_api_key


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def require_license_api_key(
    x_license_api_key: str | None = Header(None, alias="X-License-Api-Key"),
) -> None:
    expected = get_api_key()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail={"code": "NOT_CONFIGURED", "message": "LICENSE_SERVER_API_KEY is not configured"},
        )
    if (x_license_api_key or "").strip() != expected:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "Invalid license server API key"},
        )
