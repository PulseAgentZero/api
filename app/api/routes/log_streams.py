"""Self-hosted log streaming destinations (license: log_streaming)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.role_deps import require_role
from app.api.dependencies.plan_gate import require_feature
from app.api.errors import bad_request, not_found
from app.config.settings import settings
from app.infrastructure.database.models.log_stream import (
    DESTINATION_TYPES,
    LOG_EVENT_CATEGORIES,
    LogStream,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db
from app.infrastructure.logging.streams.config_crypto import (
    encrypt_stream_config,
    redact_stream_config,
)
from app.infrastructure.logging.streams.delivery import deliver_batch
from app.infrastructure.logging.streams.manager import (
    get_log_stream_manager,
    publish_log_streams_changed,
)

router = APIRouter(prefix="/log-streams", tags=["Log Streams"])


def _self_hosted_only() -> None:
    if settings.DEPLOYMENT_MODE != "self_hosted":
        raise not_found("Not found")


def _serialize(row: LogStream) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "destination_type": row.destination_type,
        "is_active": row.is_active,
        "min_level": row.min_level,
        "event_categories": list(row.event_categories or []),
        "config": redact_stream_config(dict(row.config or {})),
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
        "last_error": row.last_error,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


class LogStreamBody(BaseModel):
    name: str
    destination_type: str
    is_active: bool = True
    min_level: str = "INFO"
    event_categories: list[str] = Field(default_factory=lambda: list(LOG_EVENT_CATEGORIES))
    config: dict[str, Any] = Field(default_factory=dict)


def _validate_body(body: LogStreamBody) -> None:
    if body.destination_type not in DESTINATION_TYPES:
        raise bad_request("BAD_REQUEST", f"destination_type must be one of {DESTINATION_TYPES}")
    if body.min_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise bad_request("BAD_REQUEST", "Invalid min_level")
    for cat in body.event_categories:
        if cat not in LOG_EVENT_CATEGORIES:
            raise bad_request("BAD_REQUEST", f"Unknown event category: {cat}")
    cfg = body.config
    if body.destination_type == "http" and not str(cfg.get("url") or "").strip():
        raise bad_request("BAD_REQUEST", "HTTP destination requires config.url")
    if body.destination_type == "syslog" and not str(cfg.get("host") or "").strip():
        raise bad_request("BAD_REQUEST", "Syslog destination requires config.host")
    if body.destination_type == "file" and not str(cfg.get("path") or "").strip():
        raise bad_request("BAD_REQUEST", "File destination requires config.path")


@router.get("")
async def list_log_streams(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "log_streaming")
    rows = (
        await db.execute(
            select(LogStream)
            .where(LogStream.org_id == current_user.org_id)
            .order_by(LogStream.created_at.desc())
        )
    ).scalars().all()
    return {"streams": [_serialize(r) for r in rows]}


@router.post("", status_code=201)
async def create_log_stream(
    body: LogStreamBody,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "log_streaming")
    _validate_body(body)
    row = LogStream(
        org_id=current_user.org_id,
        name=body.name.strip(),
        destination_type=body.destination_type,
        is_active=body.is_active,
        min_level=body.min_level.upper(),
        event_categories=body.event_categories,
        config=encrypt_stream_config(body.config),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await publish_log_streams_changed()
    return _serialize(row)


@router.patch("/{stream_id}")
async def update_log_stream(
    stream_id: UUID,
    body: LogStreamBody,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "log_streaming")
    row = await db.get(LogStream, stream_id)
    if not row or row.org_id != current_user.org_id:
        raise not_found()
    _validate_body(body)
    row.name = body.name.strip()
    row.destination_type = body.destination_type
    row.is_active = body.is_active
    row.min_level = body.min_level.upper()
    row.event_categories = body.event_categories
    row.config = encrypt_stream_config(body.config, previous=dict(row.config or {}))
    await db.commit()
    await db.refresh(row)
    await publish_log_streams_changed()
    return _serialize(row)


@router.delete("/{stream_id}")
async def delete_log_stream(
    stream_id: UUID,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "log_streaming")
    row = await db.get(LogStream, stream_id)
    if not row or row.org_id != current_user.org_id:
        raise not_found()
    await db.delete(row)
    await db.commit()
    await publish_log_streams_changed()
    return {"message": "Log stream deleted"}


@router.post("/{stream_id}/test")
async def test_log_stream(
    stream_id: UUID,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "log_streaming")
    row = await db.get(LogStream, stream_id)
    if not row or row.org_id != current_user.org_id:
        raise not_found()
    from app.infrastructure.logging.streams.config_crypto import decrypt_stream_config

    cfg = decrypt_stream_config(dict(row.config or {}))
    test_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": "INFO",
        "logger": "pulse.log_stream_test",
        "message": "Pulse log stream test event",
        "event_category": "system",
        "org_id": str(current_user.org_id),
        "stream_id": str(row.id),
        "test": True,
    }
    ok, err = await deliver_batch(row.destination_type, cfg, [test_record])
    if ok:
        row.last_success_at = datetime.now(timezone.utc)
        row.last_error = None
    else:
        row.last_error = (err or "delivery failed")[:2000]
    await db.commit()
    return {"success": ok, "error": err}


@router.get("/{stream_id}/health")
async def log_stream_health(
    stream_id: UUID,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "log_streaming")
    row = await db.get(LogStream, stream_id)
    if not row or row.org_id != current_user.org_id:
        raise not_found()
    runtime = get_log_stream_manager().health(str(stream_id))
    return {
        "id": str(row.id),
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
        "last_error": row.last_error,
        "runtime": runtime,
    }
