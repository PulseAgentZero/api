"""Abstract base for all storage backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID
import os
import uuid


def build_object_key(
    *,
    org_id: UUID,
    category: str,
    filename: str,
    prefix: str = "assets",
) -> str:
    """Return a deterministic storage path: ``{prefix}/org/{org_id}/{category}/{uid}_{safe_name}``."""
    safe = os.path.basename(filename).replace(" ", "_")[:200] or "file"
    uid = uuid.uuid4().hex[:12]
    return f"{prefix.strip('/')}/org/{org_id}/{category}/{uid}_{safe}"


class StorageBackend(ABC):
    """Common interface for all asset storage backends."""

    @abstractmethod
    async def upload(
        self,
        data: bytes,
        *,
        org_id: UUID,
        category: str,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> tuple[str, str]:
        """Upload bytes. Returns ``(public_url, object_key)``."""

    @abstractmethod
    async def delete(self, object_key: str) -> None:
        """Delete a file by its object key."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if this backend has all required config."""
