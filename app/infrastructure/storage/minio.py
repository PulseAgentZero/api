"""MinIO storage backend (S3-compatible, self-hosted).

MinIO is fully S3-compatible — same boto3 API, just different endpoint_url.
Works with any S3-compatible storage: MinIO, Cloudflare R2, DigitalOcean Spaces,
Backblaze B2, Wasabi, etc.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from uuid import UUID

from app.infrastructure.storage.base import StorageBackend, build_object_key

logger = logging.getLogger(__name__)


class MinIOBackend(StorageBackend):
    """S3-compatible storage via MinIO or any other S3-compatible provider."""

    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        prefix: str = "assets",
        use_ssl: bool = False,
        public_base_url: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        self._endpoint_url = endpoint_url.rstrip("/")
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._prefix = prefix
        self._use_ssl = use_ssl
        self._public_base_url = (public_base_url or "").rstrip("/") or None
        self._region = region

    def is_configured(self) -> bool:
        return bool(self._endpoint_url and self._access_key and self._secret_key and self._bucket)

    def _get_client(self):
        import boto3
        return boto3.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
        )

    def _ensure_bucket(self, client) -> None:
        """Create bucket if it does not exist."""
        try:
            client.head_bucket(Bucket=self._bucket)
        except Exception:
            try:
                client.create_bucket(Bucket=self._bucket)
                logger.info("MinIO: created bucket '%s'", self._bucket)
            except Exception as exc:
                logger.warning("MinIO: could not create bucket '%s': %s", self._bucket, exc)

    def _public_url(self, key: str) -> str:
        if self._public_base_url:
            return f"{self._public_base_url}/{key}"
        return f"{self._endpoint_url}/{self._bucket}/{key}"

    def _put(self, data: bytes, key: str, content_type: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError
        client = self._get_client()
        self._ensure_bucket(client)
        try:
            client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(f"MinIO upload failed: {exc}") from exc

    def _remove(self, key: str) -> None:
        client = self._get_client()
        client.delete_object(Bucket=self._bucket, Key=key)

    async def upload(self, data: bytes, *, org_id: UUID, category: str, filename: str, content_type: str = "application/octet-stream") -> tuple[str, str]:
        if not self.is_configured():
            raise RuntimeError("MinIO is not fully configured (check MINIO_ENDPOINT_URL, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_BUCKET)")
        ct = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        key = build_object_key(org_id=org_id, category=category, filename=filename, prefix=self._prefix)
        await asyncio.to_thread(self._put, data, key, ct)
        return self._public_url(key), key

    async def delete(self, object_key: str) -> None:
        await asyncio.to_thread(self._remove, object_key)
