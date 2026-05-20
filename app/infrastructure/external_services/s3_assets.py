"""Asset upload helpers — delegates to the configured storage backend.

Supported backends (set STORAGE_BACKEND env var):
  s3     → AWS S3
  minio  → MinIO or any S3-compatible provider (Cloudflare R2, DO Spaces, etc.)
  local  → local filesystem (served at /assets by the API)

Auto-detection: MinIO if MINIO_ENDPOINT_URL is set, S3 if ASSETS_S3_BUCKET is set,
otherwise local.

These helpers are thin wrappers kept for backward compatibility with existing route code.
New code should import from app.infrastructure.storage directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from uuid import UUID

from app.infrastructure.storage.factory import get_storage_backend

logger = logging.getLogger(__name__)


def build_object_key(
    *,
    org_id: UUID,
    category: str,
    filename: str,
    prefix: str | None = None,
) -> str:
    from app.infrastructure.storage.base import build_object_key as _build
    return _build(
        org_id=org_id,
        category=category,
        filename=filename,
        prefix=prefix or os.getenv("ASSETS_S3_PREFIX", "assets"),
    )


def upload_bytes_to_s3(
    data: bytes,
    *,
    org_id: UUID,
    category: str,
    filename: str,
    content_type: str | None = None,
    object_key: str | None = None,
) -> str:
    """Synchronous upload wrapper (runs the async backend in a new event loop).

    Returns the public URL. Raises RuntimeError on failure.
    """
    backend = get_storage_backend()
    if not backend.is_configured():
        raise RuntimeError(
            f"Storage backend is not configured. "
            f"Set STORAGE_BACKEND and the required variables for your chosen backend."
        )

    async def _upload():
        url, key = await backend.upload(
            data,
            org_id=org_id,
            category=category,
            filename=filename,
            content_type=content_type or "application/octet-stream",
        )
        return url

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Already inside an async context (FastAPI) — schedule as task
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _upload())
                return future.result()
        else:
            return loop.run_until_complete(_upload())
    except Exception as exc:
        raise RuntimeError(f"Upload failed: {exc}") from exc


async def upload_bytes(
    data: bytes,
    *,
    org_id: UUID,
    category: str,
    filename: str,
    content_type: str | None = None,
) -> tuple[str, str]:
    """Async upload. Returns (public_url, object_key). Preferred over upload_bytes_to_s3."""
    backend = get_storage_backend()
    if not backend.is_configured():
        raise RuntimeError("Storage backend is not configured")
    return await backend.upload(
        data,
        org_id=org_id,
        category=category,
        filename=filename,
        content_type=content_type or "application/octet-stream",
    )
