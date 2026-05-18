"""Storage backend factory. Returns the configured backend based on STORAGE_BACKEND."""

from __future__ import annotations

import logging
import os

from app.infrastructure.storage.base import StorageBackend

logger = logging.getLogger(__name__)

_backend: StorageBackend | None = None


def get_storage_backend() -> StorageBackend:
    """Return the singleton storage backend. Cached after first call."""
    global _backend
    if _backend is None:
        _backend = _create_backend()
    return _backend


def _create_backend() -> StorageBackend:
    backend_type = os.getenv("STORAGE_BACKEND", "").strip().lower()

    # Auto-detect: if MinIO is configured use it, then S3, then local
    if not backend_type:
        if os.getenv("MINIO_ENDPOINT_URL"):
            backend_type = "minio"
        elif os.getenv("ASSETS_S3_BUCKET"):
            backend_type = "s3"
        else:
            backend_type = "local"

    if backend_type == "s3":
        from app.infrastructure.storage.s3 import S3Backend
        backend = S3Backend(
            bucket=os.getenv("ASSETS_S3_BUCKET", ""),
            region=os.getenv("AWS_REGION", "us-east-1"),
            prefix=os.getenv("ASSETS_S3_PREFIX", "assets"),
            public_base_url=os.getenv("ASSETS_PUBLIC_BASE_URL"),
        )
        logger.info("Storage backend: AWS S3 (bucket=%s)", os.getenv("ASSETS_S3_BUCKET"))
        return backend

    if backend_type == "minio":
        from app.infrastructure.storage.minio import MinIOBackend
        backend = MinIOBackend(
            endpoint_url=os.getenv("MINIO_ENDPOINT_URL", "http://minio:9000"),
            access_key=os.getenv("MINIO_ACCESS_KEY", ""),
            secret_key=os.getenv("MINIO_SECRET_KEY", ""),
            bucket=os.getenv("MINIO_BUCKET", "pulse-assets"),
            prefix=os.getenv("ASSETS_S3_PREFIX", "assets"),
            use_ssl=os.getenv("MINIO_USE_SSL", "false").lower() == "true",
            public_base_url=os.getenv("ASSETS_PUBLIC_BASE_URL"),
            region=os.getenv("AWS_REGION", "us-east-1"),
        )
        logger.info("Storage backend: MinIO (endpoint=%s)", os.getenv("MINIO_ENDPOINT_URL"))
        return backend

    # Default: local filesystem
    from app.infrastructure.storage.local import LocalBackend
    storage_path = os.getenv("LOCAL_STORAGE_PATH", "/app/uploads")
    public_base = os.getenv("LOCAL_STORAGE_URL_BASE", "").rstrip("/")
    if not public_base:
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:8000").rstrip("/")
        public_base = f"{frontend_url}/assets"
    backend = LocalBackend(
        storage_path=storage_path,
        public_base_url=public_base,
        prefix=os.getenv("ASSETS_S3_PREFIX", "assets"),
    )
    logger.info("Storage backend: local filesystem (path=%s)", storage_path)
    return backend
