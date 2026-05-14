"""Upload arbitrary bytes to S3 for org-scoped assets (avatars, logos, CSVs, etc.)."""

from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from typing import BinaryIO
from uuid import UUID

from app.config.settings import settings

logger = logging.getLogger(__name__)


def _guess_content_type(filename: str, fallback: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or fallback


def build_object_key(
    *,
    org_id: UUID,
    category: str,
    filename: str,
    prefix: str | None = None,
) -> str:
    """Return S3 object key: ``{prefix}/org/{org_id}/{category}/{uuid}_{safe_name}``."""
    base = (prefix or settings.ASSETS_S3_PREFIX or "assets").strip("/")
    safe = os.path.basename(filename).replace(" ", "_")[:200] or "file"
    uid = uuid.uuid4().hex[:12]
    return f"{base}/org/{org_id}/{category}/{uid}_{safe}"


def upload_bytes_to_s3(
    data: bytes,
    *,
    org_id: UUID,
    category: str,
    filename: str,
    content_type: str | None = None,
    object_key: str | None = None,
) -> str:
    """Upload ``data`` to S3 and return a public or virtual-host URL.

    Requires ``ASSETS_S3_BUCKET`` and AWS credentials (env, IAM role, or default chain).

    :param category: Logical folder, e.g. ``profile``, ``logo``, ``data``, ``csv``.
    :raises RuntimeError: If bucket is not configured or boto3 / upload fails.
    """
    bucket = (settings.ASSETS_S3_BUCKET or "").strip()
    if not bucket:
        raise RuntimeError("ASSETS_S3_BUCKET is not configured")

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:
        raise RuntimeError("boto3 is required for S3 uploads") from exc

    key = object_key or build_object_key(org_id=org_id, category=category, filename=filename)
    ct = content_type or _guess_content_type(filename, "application/octet-stream")
    region = (settings.AWS_REGION or os.getenv("AWS_REGION") or "us-east-1").strip()

    client = boto3.client("s3", region_name=region)
    try:
        client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=ct)
    except (BotoCoreError, ClientError) as exc:
        logger.exception("S3 put_object failed bucket=%s key=%s", bucket, key)
        raise RuntimeError(f"S3 upload failed: {exc}") from exc

    if settings.ASSETS_PUBLIC_BASE_URL:
        base = settings.ASSETS_PUBLIC_BASE_URL.rstrip("/")
        return f"{base}/{key}"
    if region == "us-east-1":
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def upload_fileobj_to_s3(
    fileobj: BinaryIO,
    *,
    org_id: UUID,
    category: str,
    filename: str,
    content_type: str | None = None,
) -> str:
    data = fileobj.read()
    if not isinstance(data, bytes):
        raise TypeError("file object must yield bytes")
    return upload_bytes_to_s3(
        data, org_id=org_id, category=category, filename=filename, content_type=content_type
    )
