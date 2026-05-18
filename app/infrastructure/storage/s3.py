"""AWS S3 storage backend."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from uuid import UUID

from app.infrastructure.storage.base import StorageBackend, build_object_key

logger = logging.getLogger(__name__)


class S3Backend(StorageBackend):
    """AWS S3. Uses the default AWS credential chain (env vars, IAM role, ~/.aws)."""

    def __init__(
        self,
        bucket: str,
        region: str = "us-east-1",
        prefix: str = "assets",
        public_base_url: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._prefix = prefix
        self._public_base_url = (public_base_url or "").rstrip("/") or None

    def is_configured(self) -> bool:
        return bool(self._bucket)

    def _public_url(self, key: str) -> str:
        if self._public_base_url:
            return f"{self._public_base_url}/{key}"
        if self._region == "us-east-1":
            return f"https://{self._bucket}.s3.amazonaws.com/{key}"
        return f"https://{self._bucket}.s3.{self._region}.amazonaws.com/{key}"

    def _put(self, data: bytes, key: str, content_type: str) -> None:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        client = boto3.client("s3", region_name=self._region)
        try:
            client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(f"S3 upload failed: {exc}") from exc

    def _remove(self, key: str) -> None:
        import boto3
        client = boto3.client("s3", region_name=self._region)
        client.delete_object(Bucket=self._bucket, Key=key)

    async def upload(self, data: bytes, *, org_id: UUID, category: str, filename: str, content_type: str = "application/octet-stream") -> tuple[str, str]:
        if not self._bucket:
            raise RuntimeError("ASSETS_S3_BUCKET is not configured")
        ct = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        key = build_object_key(org_id=org_id, category=category, filename=filename, prefix=self._prefix)
        await asyncio.to_thread(self._put, data, key, ct)
        return self._public_url(key), key

    async def delete(self, object_key: str) -> None:
        await asyncio.to_thread(self._remove, object_key)
