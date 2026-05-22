"""Background pipeline scheduler using APScheduler.

Runs the autonomous agent pipeline on a schedule for every org
that has completed onboarding. Per-org cadence comes from
``pipeline_schedules`` when configured; otherwise falls back to
``PIPELINE_INTERVAL_HOURS``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from croniter import croniter
from sqlalchemy import select

from app.config.settings import settings
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.pipeline_schedule import PipelineSchedule
from app.infrastructure.database.repositories.pipeline_run_repository import (
    PipelineRunRepository,
)
from app.infrastructure.database.session import async_session_factory
from app.infrastructure.redis.client import get_redis
from app.services.schedulers.heartbeat import (
    HEARTBEAT_KIND,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_STALE_AFTER_SECONDS,
)
from app.services.self_hosted_license import get_concurrent_pipeline_limit

logger = logging.getLogger(__name__)

PIPELINE_INTERVAL_HOURS = int(os.getenv("PIPELINE_INTERVAL_HOURS", "4"))

# Discovery: default 60s; legacy minutes env still supported when seconds unset.
_discovery_seconds_env = os.getenv("PIPELINE_ORG_DISCOVERY_INTERVAL_SECONDS", "").strip()
_discovery_minutes_env = os.getenv("PIPELINE_ORG_DISCOVERY_INTERVAL_MINUTES", "").strip()
if _discovery_seconds_env:
    PIPELINE_ORG_DISCOVERY_INTERVAL_SECONDS = max(10, int(_discovery_seconds_env))
elif _discovery_minutes_env:
    PIPELINE_ORG_DISCOVERY_INTERVAL_SECONDS = max(10, int(_discovery_minutes_env) * 60)
else:
    PIPELINE_ORG_DISCOVERY_INTERVAL_SECONDS = 60

SCHEDULE_RELOAD_CHANNEL = "pulse:schedule:reload"
# Heartbeat constants are re-exported from ``heartbeat.py`` for backward
# compatibility with callers that import them from this module.
__all_heartbeat__ = (
    "HEARTBEAT_KIND",
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_STALE_AFTER_SECONDS",
)

_scheduler: AsyncIOScheduler | None = None
_reload_listener_task: asyncio.Task | None = None
_job_signatures: dict[str, str] = {}

# In-process counter of scheduled invocations of `_run_pipeline_for_org`.
# This is intentionally lightweight (no Prometheus dep). The persisted
# `scheduler_heartbeats.scheduled_runs_total` mirrors it across restarts.
_scheduled_invocations_total = 0
_last_scheduled_invocation_at: datetime | None = None
_last_scheduled_invocation_org_id: str | None = None


def _trigger_signature(trigger: Any) -> str:
    """Stable string for comparing whether a job trigger changed."""
    if isinstance(trigger, CronTrigger):
        fields = getattr(trigger, "fields", None)
        if fields:
            parts = []
            for f in fields:
                if f.name in ("year", "month", "day", "week", "day_of_week", "hour", "minute", "second"):
                    parts.append(f"{f.name}={f}")
            tz = getattr(trigger, "timezone", None)
            return f"cron:{':'.join(parts)}:tz={tz}"
        return f"cron:{trigger!r}"
    if isinstance(trigger, IntervalTrigger):
        interval = getattr(trigger, "interval", None)
        return f"interval:{interval}"
    return repr(trigger)


def _cron_trigger_from_crontab(
    expression: str,
    *,
    timezone: ZoneInfo,
    jitter_seconds: int = 0,
) -> CronTrigger:
    """Build a CronTrigger from a five-field crontab expression with jitter."""
    minute, hour, day, month, day_of_week = expression.strip().split()
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone=timezone,
        jitter=jitter_seconds,
    )


def _build_trigger(
    schedule: PipelineSchedule | None,
    *,
    jitter_seconds: int = 0,
) -> tuple[Any, str]:
    """Return (APScheduler trigger, signature) for an org."""
    if schedule is not None and schedule.is_active:
        tz_name = schedule.timezone or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
            tz_name = "UTC"
        trigger = _cron_trigger_from_crontab(
            schedule.cron_expression.strip(),
            timezone=tz,
            jitter_seconds=jitter_seconds,
        )
        sig = f"cron:{schedule.cron_expression}:{tz_name}"
        return trigger, sig

    trigger = IntervalTrigger(hours=PIPELINE_INTERVAL_HOURS, jitter=jitter_seconds)
    return trigger, f"interval:{PIPELINE_INTERVAL_HOURS}h"


async def touch_pipeline_schedule_after_run(org_id: UUID) -> None:
    """Update last_run_at / next_run_at on the org's pipeline schedule row."""
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(PipelineSchedule).where(PipelineSchedule.org_id == org_id).limit(1)
            )
            row = result.scalar_one_or_none()
            if row is None or not row.is_active:
                return
            now_utc = datetime.now(timezone.utc)
            row.last_run_at = now_utc
            try:
                tz = ZoneInfo(row.timezone or "UTC")
                base = now_utc.astimezone(tz)
                row.next_run_at = croniter(row.cron_expression, base).get_next(datetime)
            except Exception:
                row.next_run_at = croniter(row.cron_expression, now_utc).get_next(datetime)
            await session.commit()
    except Exception as e:
        logger.warning("Failed to update pipeline schedule timestamps for org %s: %s", org_id, e)


async def publish_schedule_reload() -> None:
    """Notify the scheduler process to re-read pipeline_schedules immediately."""
    r = await get_redis()
    if r is None:
        return
    try:
        await r.publish(SCHEDULE_RELOAD_CHANNEL, "reload")
    except Exception as e:
        logger.warning("Failed to publish pipeline schedule reload: %s", e)


async def _claim_run_slot(
    org_id: UUID,
    trigger_source: str,
    *,
    mapping_id: UUID | None = None,
    triggered_by: UUID | None = None,
) -> UUID | None:
    """Create a queued PipelineRun if the org has an available concurrency slot."""
    async with async_session_factory() as session:
        repo = PipelineRunRepository(session)
        max_concurrent = 1
        if settings.DEPLOYMENT_MODE == "self_hosted":
            max_concurrent = await get_concurrent_pipeline_limit(session, org_id)
        active_count = await repo.count_active_for_org(org_id)
        if active_count >= max_concurrent:
            active = await repo.get_active_for_org(org_id)
            logger.info(
                "Pipeline run skipped for org %s — concurrency limit reached (%s/%s), latest active=%s",
                org_id, active_count, max_concurrent, active.id if active else None,
            )
            return None
        run = await repo.create_queued(
            org_id,
            trigger_source=trigger_source,
            mapping_id=mapping_id,
            triggered_by=triggered_by,
        )
        await session.commit()
        return run.id


async def _run_pipeline_for_org(
    org_id_str: str, trigger_source: str = "scheduled"
) -> None:
    """Enqueue or start the pipeline for one organisation."""
    global _scheduled_invocations_total, _last_scheduled_invocation_at
    global _last_scheduled_invocation_org_id

    if trigger_source == "scheduled":
        _scheduled_invocations_total += 1
        _last_scheduled_invocation_at = datetime.now(timezone.utc)
        _last_scheduled_invocation_org_id = org_id_str
        logger.info(
            "[Scheduler] tick: org_id=%s total_scheduled_invocations=%d",
            org_id_str,
            _scheduled_invocations_total,
        )

    org_id = UUID(org_id_str)
    await trigger_pipeline_now(org_id, trigger_source=trigger_source)


def get_scheduled_invocations_total() -> int:
    """Test/debug accessor for the in-process counter."""
    return _scheduled_invocations_total


async def _discover_and_schedule_orgs(scheduler: AsyncIOScheduler) -> None:
    """Find onboarded orgs and register APScheduler jobs from pipeline_schedules."""
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Organization, PipelineSchedule)
                .outerjoin(
                    PipelineSchedule,
                    PipelineSchedule.org_id == Organization.id,
                )
                .where(Organization.onboarding_done.is_(True))
                .order_by(Organization.id, PipelineSchedule.updated_at.desc().nullslast())
            )
            rows_by_org: dict[UUID, tuple[Organization, PipelineSchedule | None]] = {}
            for org, schedule_row in result.all():
                rows_by_org.setdefault(org.id, (org, schedule_row))
            rows = list(rows_by_org.values())

        if not rows:
            logger.info("No onboarded organisations found — scheduler idle")
            return

        scheduled_count = 0
        desired_job_ids: set[str] = set()
        for i, (org, schedule_row) in enumerate(rows):
            job_id = f"pipeline_{org.id}"
            stagger_seconds = i * 30 + random.randint(0, 30)

            if schedule_row is not None and not schedule_row.is_active:
                existing = scheduler.get_job(job_id)
                if existing is not None:
                    scheduler.remove_job(job_id)
                    _job_signatures.pop(job_id, None)
                    logger.info(
                        "Removed paused pipeline job for org '%s' (%s)",
                        org.name, org.id,
                    )
                continue

            desired_job_ids.add(job_id)
            trigger, sig = _build_trigger(schedule_row, jitter_seconds=stagger_seconds)
            existing = scheduler.get_job(job_id)
            if existing is not None and _job_signatures.get(job_id) == sig:
                continue

            scheduler.add_job(
                _run_pipeline_for_org,
                trigger=trigger,
                id=job_id,
                args=[str(org.id), "scheduled"],
                replace_existing=True,
                misfire_grace_time=300,
                coalesce=True,
            )
            _job_signatures[job_id] = sig

            if schedule_row is not None:
                logger.info(
                    "Scheduled pipeline for org '%s' (%s) cron='%s' tz=%s (stagger jitter=%ds)",
                    org.name,
                    org.id,
                    schedule_row.cron_expression,
                    schedule_row.timezone,
                    stagger_seconds,
                )
            else:
                logger.info(
                    "Scheduled pipeline for org '%s' (%s) every %dh (stagger jitter=%ds)",
                    org.name,
                    org.id,
                    PIPELINE_INTERVAL_HOURS,
                    stagger_seconds,
                )
            scheduled_count += 1

        for job in scheduler.get_jobs():
            if job.id.startswith("pipeline_") and job.id not in desired_job_ids:
                scheduler.remove_job(job.id)
                _job_signatures.pop(job.id, None)

        logger.info(
            "Pipeline scheduler configured: %d active jobs (of %d onboarded orgs)",
            scheduled_count,
            len(rows),
        )
    except Exception as e:
        logger.error("Failed to discover orgs for scheduling: %s", e)


async def _listen_schedule_reload(scheduler: AsyncIOScheduler) -> None:
    """Subscribe to Redis reload events and re-discover org schedules."""
    r = await get_redis()
    if r is None:
        return
    pubsub = r.pubsub()
    await pubsub.subscribe(SCHEDULE_RELOAD_CHANNEL)
    logger.info("Pipeline scheduler listening on %s", SCHEDULE_RELOAD_CHANNEL)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            logger.info("Pipeline schedule reload received — re-discovering orgs")
            await _discover_and_schedule_orgs(scheduler)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("Schedule reload listener stopped: %s", e)
    finally:
        await pubsub.unsubscribe(SCHEDULE_RELOAD_CHANNEL)
        await pubsub.aclose()


def schedule_org(org_id: UUID, org_name: str = "") -> None:
    """Add or update a pipeline job for one org (scheduler process only)."""
    global _scheduler
    if _scheduler is None:
        logger.warning("Scheduler not started — cannot schedule org %s", org_id)
        return
    asyncio.get_event_loop().create_task(_discover_and_schedule_orgs(_scheduler))


async def trigger_pipeline_now(
    org_id: UUID,
    *,
    trigger_source: str = "manual",
    mapping_id: UUID | None = None,
    triggered_by: UUID | None = None,
) -> UUID | None:
    """Trigger an immediate pipeline run for an org (non-blocking)."""
    run_id = await _claim_run_slot(
        org_id,
        trigger_source,
        mapping_id=mapping_id,
        triggered_by=triggered_by,
    )
    if run_id is None:
        return None

    from app.services.pipeline_queue import enqueue_pipeline_job

    if await enqueue_pipeline_job(run_id=run_id, org_id=org_id, trigger_source=trigger_source):
        logger.info(
            "Queued pipeline run %s for org %s (trigger=%s) on Redis",
            run_id, org_id, trigger_source,
        )
        return run_id

    async def _execute() -> None:
        from app.agents.orchestrators.pipeline import PipelineOrchestrator

        try:
            async with async_session_factory() as session:
                orchestrator = PipelineOrchestrator(session)
                await orchestrator.execute(
                    org_id, trigger_source=trigger_source, run_id=run_id,
                )
        except Exception as e:
            logger.exception(
                "Background pipeline run %s for org %s failed: %s",
                run_id, org_id, e,
            )

    asyncio.create_task(_execute())
    logger.info(
        "Triggered immediate pipeline run %s for org %s (trigger=%s)",
        run_id, org_id, trigger_source,
    )
    return run_id


async def start_pipeline_scheduler() -> AsyncIOScheduler:
    """Start the APScheduler instance and discover existing orgs."""
    global _scheduler, _reload_listener_task

    groq_ok = settings.is_groq_configured()
    anthropic_ok = settings.is_anthropic_configured()

    if not groq_ok and not anthropic_ok:
        logger.warning(
            "Neither GROQ_API_KEY nor ANTHROPIC_API_KEY configured — "
            "pipeline scheduler will not register org jobs (heartbeat still runs)"
        )
        # Start an empty APScheduler so callers that expect a running scheduler
        # (e.g. schedule_org) don't crash. Heartbeat lives at the process level
        # in app.services.schedulers.run — so the UI still shows "healthy".
        _scheduler = AsyncIOScheduler()
        _scheduler.start()
        return _scheduler

    if not groq_ok:
        logger.warning(
            "GROQ_API_KEY not configured — background agents will "
            "attempt Anthropic fallback (higher cost)"
        )

    _scheduler = AsyncIOScheduler()
    _scheduler.start()

    await _discover_and_schedule_orgs(_scheduler)

    _scheduler.add_job(
        _discover_and_schedule_orgs,
        trigger=IntervalTrigger(seconds=PIPELINE_ORG_DISCOVERY_INTERVAL_SECONDS),
        id="pipeline_discover_orgs",
        args=[_scheduler],
        replace_existing=True,
    )

    _reload_listener_task = asyncio.create_task(_listen_schedule_reload(_scheduler))

    logger.info(
        "Pipeline scheduler started (groq=%s, anthropic=%s, discovery every %ds)",
        groq_ok,
        anthropic_ok,
        PIPELINE_ORG_DISCOVERY_INTERVAL_SECONDS,
    )
    return _scheduler


def shutdown_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    global _scheduler, _reload_listener_task
    if _reload_listener_task is not None:
        _reload_listener_task.cancel()
        _reload_listener_task = None
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        logger.info("Pipeline scheduler shut down")
        _scheduler = None


if __name__ == "__main__":
    from app.services.schedulers.run import main

    main()
