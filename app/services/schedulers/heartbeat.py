"""Process-level liveness heartbeat for the scheduler runner.

Runs as a top-level `asyncio` task inside `app.services.schedulers.run` so
heartbeats are written even when individual schedulers fail to start or no
pipeline jobs are registered (e.g. before any LLM key is configured).

The UI reads ``scheduler_heartbeats`` via ``GET /api/v1/pipeline/scheduler/status``
to render the "Scheduler healthy / not seen recently" pill.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config.settings import settings
from app.infrastructure.database.models.scheduler_heartbeat import SchedulerHeartbeat
from app.infrastructure.database.session import async_session_factory

logger = logging.getLogger(__name__)

HEARTBEAT_KIND = "pipeline"
HEARTBEAT_INTERVAL_SECONDS = int(
    os.getenv("PIPELINE_SCHEDULER_HEARTBEAT_SECONDS", "60")
)
# How long without a heartbeat before the UI shows "unhealthy".
HEARTBEAT_STALE_AFTER_SECONDS = int(
    os.getenv("PIPELINE_SCHEDULER_HEARTBEAT_STALE_SECONDS", "180")
)


async def write_heartbeat_once(scheduled_runs_total: int = 0) -> bool:
    """Upsert a single heartbeat row. Returns True on success. Best-effort."""
    if not settings.is_database_configured():
        return False
    try:
        async with async_session_factory() as session:
            now_utc = datetime.now(timezone.utc)
            stmt = pg_insert(SchedulerHeartbeat).values(
                kind=HEARTBEAT_KIND,
                last_seen_at=now_utc,
                process_id=str(os.getpid()),
                host=socket.gethostname(),
                scheduled_runs_total=scheduled_runs_total,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[SchedulerHeartbeat.kind],
                set_={
                    "last_seen_at": now_utc,
                    "process_id": str(os.getpid()),
                    "host": socket.gethostname(),
                    "scheduled_runs_total": scheduled_runs_total,
                    "updated_at": now_utc,
                },
            )
            await session.execute(stmt)
            await session.commit()
        return True
    except Exception as exc:
        logger.warning("[Scheduler] heartbeat write failed: %s", exc)
        return False


def _get_scheduled_runs_total() -> int:
    """Read the live in-process scheduled-runs counter (best-effort)."""
    try:
        from app.services.schedulers.pipeline_scheduler import (
            get_scheduled_invocations_total,
        )

        return get_scheduled_invocations_total()
    except Exception:
        return 0


async def heartbeat_loop(stop: asyncio.Event) -> None:
    """Run forever — write a heartbeat every HEARTBEAT_INTERVAL_SECONDS.

    Independent of any individual scheduler. The loop catches every error so a
    transient DB outage can never crash the scheduler process.
    """
    logger.info(
        "[Scheduler] heartbeat loop started (every %ss, stale after %ss)",
        HEARTBEAT_INTERVAL_SECONDS,
        HEARTBEAT_STALE_AFTER_SECONDS,
    )
    await write_heartbeat_once(_get_scheduled_runs_total())

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass

        if stop.is_set():
            break

        try:
            await write_heartbeat_once(_get_scheduled_runs_total())
        except Exception:
            logger.exception("[Scheduler] heartbeat tick raised — continuing")

    logger.info("[Scheduler] heartbeat loop stopped")
