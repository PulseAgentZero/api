"""Local filesystem storage backend.

Files are written to LOCAL_STORAGE_PATH on disk and served by FastAPI's
StaticFiles mount at /assets (or LOCAL_STORAGE_URL_PREFIX).

Best for: development, air-gapped self-hosted deployments, single-server setups.
Not suitable for: multi-replica deployments (files are not shared between instances).
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from uuid import UUID

from app.infrastructure.storage.base import StorageBackend, build_object_key

logger = logging.getLogger(__name__)


class LocalBackend(StorageBackend):
    """Stores files on the local filesystem. Serves them at ``url_prefix``."""

    def __init__(
        self,
        storage_path: str = "/data/uploads",
        public_base_url: str = "http://localhost:8000/assets",
        prefix: str = "assets",
    ) -> None:
        self._storage_path = Path(storage_path)
        self._public_base_url = public_base_url.rstrip("/")
        self._prefix = prefix

    def is_configured(self) -> bool:
        return True  # always available — no external service required

    def _public_url(self, key: str) -> str:
        return f"{self._public_base_url}/{key}"

    def _write(self, data: bytes, full_path: Path) -> None:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(data)

    def _remove(self, full_path: Path) -> None:
        try:
            full_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Local storage: could not delete %s: %s", full_path, exc)

    async def upload(self, data: bytes, *, org_id: UUID, category: str, filename: str, content_type: str = "application/octet-stream") -> tuple[str, str]:
        key = build_object_key(org_id=org_id, category=category, filename=filename, prefix=self._prefix)
        full_path = self._storage_path / key
        await asyncio.to_thread(self._write, data, full_path)
        logger.debug("Local storage: wrote %d bytes to %s", len(data), full_path)
        return self._public_url(key), key

    async def delete(self, object_key: str) -> None:
        full_path = self._storage_path / object_key
        await asyncio.to_thread(self._remove, full_path)

    @property
    def storage_path(self) -> Path:
        """The filesystem path where files are stored. Mount this with StaticFiles."""
        return self._storage_path
