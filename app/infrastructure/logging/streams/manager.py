"""Load org log streams and deliver batched structured logs."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.infrastructure.database.models.log_stream import LogStream
from app.infrastructure.database.session import async_session_factory
from app.infrastructure.logging.context import get_log_org_id, get_log_request_id
from app.infrastructure.logging.streams.config_crypto import decrypt_stream_config
from app.infrastructure.logging.streams.delivery import deliver_batch
from app.infrastructure.redis import keys as redis_keys
from app.infrastructure.redis.client import get_redis

logger = logging.getLogger(__name__)

_JSON_FORMATTER: Any = None


def _formatter() -> Any:
    global _JSON_FORMATTER
    if _JSON_FORMATTER is None:
        from app.infrastructure.logging.setup import JsonFormatter

        _JSON_FORMATTER = JsonFormatter()
    return _JSON_FORMATTER

_LEVEL_NO = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

DEFAULT_BATCH_SIZE = 50
DEFAULT_FLUSH_INTERVAL_S = 5.0
QUEUE_MAX = 5000


@dataclass
class ActiveStream:
    stream_id: str
    org_id: str
    destination_type: str
    min_level: str
    event_categories: list[str]
    config: dict[str, Any]
    batch_size: int = DEFAULT_BATCH_SIZE
    flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S
    queue: deque[dict[str, Any]] = field(default_factory=deque)
    dropped: int = 0
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_flush_at: float = 0.0

    def accepts(self, record: logging.LogRecord) -> bool:
        if record.levelno < _LEVEL_NO.get(self.min_level.upper(), 20):
            return False
        cat = getattr(record, "event_category", None) or "system"
        if self.event_categories and cat not in self.event_categories:
            return False
        rec_org = getattr(record, "org_id", None) or get_log_org_id()
        if rec_org and str(rec_org) != self.org_id:
            return False
        return True


class LogStreamManager:
    def __init__(self) -> None:
        self._streams: dict[str, ActiveStream] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._flush_task: asyncio.Task | None = None
        self._pubsub_task: asyncio.Task | None = None
        self._started = False

    def handle_record(self, record: logging.LogRecord) -> None:
        if not self._streams:
            return
        org_id = getattr(record, "org_id", None) or get_log_org_id()
        if org_id is None:
            org_id = None
        else:
            org_id = str(org_id)
        request_id = getattr(record, "request_id", None) or get_log_request_id()
        if request_id and not hasattr(record, "request_id"):
            record.request_id = request_id  # noqa: SLF001
        if org_id and not hasattr(record, "org_id"):
            record.org_id = org_id  # noqa: SLF001
        if not hasattr(record, "event_category"):
            if record.name == "pulse.client_data_audit":
                record.event_category = "security"  # noqa: SLF001
            elif record.name.startswith("pulse."):
                record.event_category = "system"  # noqa: SLF001
            else:
                record.event_category = "system"  # noqa: SLF001

        try:
            import json

            payload = json.loads(_formatter().format(record))
        except Exception:
            payload = {
                "timestamp": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "event_category": getattr(record, "event_category", "system"),
            }
            if org_id:
                payload["org_id"] = org_id
            if request_id:
                payload["request_id"] = request_id

        for stream in self._streams.values():
            if not stream.accepts(record):
                continue
            if len(stream.queue) >= QUEUE_MAX:
                stream.dropped += 1
                continue
            stream.queue.append(payload)

    async def start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        await self.reload()
        self._flush_task = asyncio.create_task(self._flush_loop(), name="log-stream-flush")
        self._pubsub_task = asyncio.create_task(self._pubsub_loop(), name="log-stream-pubsub")
        self._started = True

    async def stop(self) -> None:
        for task in (self._flush_task, self._pubsub_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._flush_task = None
        self._pubsub_task = None
        self._started = False

    async def reload(self) -> None:
        self._streams.clear()
        try:
            async with async_session_factory() as db:
                rows = (
                    await db.execute(
                        select(LogStream).where(LogStream.is_active.is_(True))
                    )
                ).scalars().all()
                for row in rows:
                    cfg = decrypt_stream_config(dict(row.config or {}))
                    batch_size = int(cfg.pop("batch_size", DEFAULT_BATCH_SIZE) or DEFAULT_BATCH_SIZE)
                    flush_s = float(
                        cfg.pop("flush_interval_s", DEFAULT_FLUSH_INTERVAL_S)
                        or DEFAULT_FLUSH_INTERVAL_S
                    )
                    self._streams[str(row.id)] = ActiveStream(
                        stream_id=str(row.id),
                        org_id=str(row.org_id),
                        destination_type=row.destination_type,
                        min_level=row.min_level,
                        event_categories=list(row.event_categories or []),
                        config=cfg,
                        batch_size=max(1, min(batch_size, 500)),
                        flush_interval_s=max(1.0, min(flush_s, 300.0)),
                    )
        except Exception:
            logger.exception("Failed to reload log streams")

    async def _pubsub_loop(self) -> None:
        while True:
            try:
                r = await get_redis()
                if r is None:
                    await asyncio.sleep(30)
                    continue
                pubsub = r.pubsub()
                await pubsub.subscribe(redis_keys.log_streams_changed())
                async for message in pubsub.listen():
                    if message.get("type") == "message":
                        await self.reload()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Log stream pubsub error")
                await asyncio.sleep(5)

    async def _flush_loop(self) -> None:
        import time

        while True:
            try:
                await asyncio.sleep(1)
                now = datetime.now(timezone.utc)
                mono = time.monotonic()
                for stream in list(self._streams.values()):
                    if not stream.queue:
                        continue
                    elapsed = mono - stream.last_flush_at
                    due = len(stream.queue) >= stream.batch_size or elapsed >= stream.flush_interval_s
                    if not due:
                        continue
                    stream.last_flush_at = mono
                    batch = [stream.queue.popleft() for _ in range(min(len(stream.queue), stream.batch_size))]
                    ok, err = await deliver_batch(
                        stream.destination_type, stream.config, batch
                    )
                    if ok:
                        stream.last_success_at = now
                        stream.last_error = None
                        await self._persist_health(
                            UUID(stream.stream_id), success=True, error=None
                        )
                    else:
                        stream.last_error = err
                        await self._persist_health(
                            UUID(stream.stream_id), success=False, error=err
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Log stream flush loop error")

    async def _persist_health(
        self, stream_id: UUID, *, success: bool, error: str | None
    ) -> None:
        try:
            async with async_session_factory() as db:
                row = await db.get(LogStream, stream_id)
                if row is None:
                    return
                if success:
                    row.last_success_at = datetime.now(timezone.utc)
                    row.last_error = None
                else:
                    row.last_error = (error or "delivery failed")[:2000]
                await db.commit()
        except Exception:
            logger.debug("Could not persist log stream health for %s", stream_id)

    def health(self, stream_id: str) -> dict[str, Any]:
        s = self._streams.get(stream_id)
        if s is None:
            return {"active": False, "queue_depth": 0, "dropped": 0}
        return {
            "active": True,
            "queue_depth": len(s.queue),
            "dropped": s.dropped,
            "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
            "last_error": s.last_error,
        }


_manager_singleton: LogStreamManager | None = None


def get_log_stream_manager() -> LogStreamManager:
    global _manager_singleton
    if _manager_singleton is None:
        _manager_singleton = LogStreamManager()
    return _manager_singleton


async def publish_log_streams_changed() -> None:
    r = await get_redis()
    if r is not None:
        await r.publish(redis_keys.log_streams_changed(), "1")


async def start_log_stream_runtime() -> None:
    from app.config.settings import settings
    from app.infrastructure.logging.streams.handler import set_stream_manager

    if settings.DEPLOYMENT_MODE != "self_hosted":
        return
    mgr = get_log_stream_manager()
    set_stream_manager(mgr)
    await mgr.start()


async def stop_log_stream_runtime() -> None:
    mgr = get_log_stream_manager()
    await mgr.stop()
