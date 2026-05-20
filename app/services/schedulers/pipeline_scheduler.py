"""Background pipeline scheduler using APScheduler.

Runs the autonomous agent pipeline on a schedule for every org
that has completed onboarding. Uses pipeline_runs.status to
deduplicate overlapping scheduled/manual triggers.
"""

import asyncio
import logging
import os
import random
from uuid import UUID

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config.settings import settings
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.repositories.pipeline_run_repository import (
    PipelineRunRepository,
)
from app.infrastructure.database.session import async_session_factory

logger = logging.getLogger(__name__)

PIPELINE_INTERVAL_HOURS = int(os.getenv("PIPELINE_INTERVAL_HOURS", "4"))
PIPELINE_ORG_DISCOVERY_INTERVAL_MINUTES = int(
    os.getenv("PIPELINE_ORG_DISCOVERY_INTERVAL_MINUTES", "15")
)

_scheduler: AsyncIOScheduler | None = None


async def _claim_run_slot(
    org_id: UUID,
    trigger_source: str,
    *,
    mapping_id: UUID | None = None,
    triggered_by: UUID | None = None,
) -> UUID | None:
    """Create a queued PipelineRun if no run is already active for the org.

    Returns the new run_id, or None if an active run already exists.
    """
    async with async_session_factory() as session:
        repo = PipelineRunRepository(session)
        active = await repo.get_active_for_org(org_id)
        if active is not None:
            logger.info(
                "Pipeline run skipped for org %s — active run %s in state '%s'",
                org_id, active.id, active.status,
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
    """Enqueue or start the pipeline for one organisation (dedup via trigger_pipeline_now)."""
    org_id = UUID(org_id_str)
    await trigger_pipeline_now(org_id, trigger_source=trigger_source)


async def _discover_and_schedule_orgs(scheduler: AsyncIOScheduler) -> None:
    """Find all onboarded orgs and schedule pipeline runs for each."""
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Organization.id, Organization.name).where(
                    Organization.onboarding_done.is_(True)
                )
            )
            orgs = result.all()

        if not orgs:
            logger.info("No onboarded organisations found — scheduler idle")
            return

        for i, (org_id, org_name) in enumerate(orgs):
            job_id = f"pipeline_{org_id}"
            stagger_seconds = i * 30 + random.randint(0, 30)

            scheduler.add_job(
                _run_pipeline_for_org,
                trigger=IntervalTrigger(hours=PIPELINE_INTERVAL_HOURS),
                id=job_id,
                args=[str(org_id), "scheduled"],
                replace_existing=True,
                next_run_time=None,
            )
            logger.info(
                "Scheduled pipeline for org '%s' (%s) every %dh (stagger: %ds)",
                org_name, org_id, PIPELINE_INTERVAL_HOURS, stagger_seconds,
            )

        logger.info(
            "Pipeline scheduler configured: %d orgs, %dh interval",
            len(orgs), PIPELINE_INTERVAL_HOURS,
        )
    except Exception as e:
        logger.error("Failed to discover orgs for scheduling: %s", e)


def schedule_org(org_id: UUID, org_name: str = "") -> None:
    """Add or update a pipeline interval job for one org (scheduler process only).

    Prefer letting periodic org discovery register new orgs; kept for tests/tools.
    """
    global _scheduler
    if _scheduler is None:
        logger.warning("Scheduler not started — cannot schedule org %s", org_id)
        return

    job_id = f"pipeline_{org_id}"
    _scheduler.add_job(
        _run_pipeline_for_org,
        trigger=IntervalTrigger(hours=PIPELINE_INTERVAL_HOURS),
        id=job_id,
        args=[str(org_id), "scheduled"],
        replace_existing=True,
        next_run_time=None,
    )
    logger.info("Scheduled pipeline for org '%s' (%s)", org_name, org_id)


async def trigger_pipeline_now(
    org_id: UUID,
    *,
    trigger_source: str = "manual",
    mapping_id: UUID | None = None,
    triggered_by: UUID | None = None,
) -> UUID | None:
    """Trigger an immediate pipeline run for an org (non-blocking).

    Returns the new run_id, or None if a run is already active for this org.
    """
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
    """Start the APScheduler instance and discover existing orgs.

    Background agents primarily use Groq. The scheduler will still start
    if only Anthropic is configured (risk/rec agents can fall back to
    Claude), but warns if neither provider is available.
    """
    global _scheduler

    groq_ok = settings.is_groq_configured()
    anthropic_ok = settings.is_anthropic_configured()

    if not groq_ok and not anthropic_ok:
        logger.warning(
            "Neither GROQ_API_KEY nor ANTHROPIC_API_KEY configured — "
            "pipeline scheduler disabled"
        )
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

    # Re-scan for newly onboarded orgs (API no longer runs an in-process scheduler).
    _scheduler.add_job(
        _discover_and_schedule_orgs,
        trigger=IntervalTrigger(minutes=PIPELINE_ORG_DISCOVERY_INTERVAL_MINUTES),
        id="pipeline_discover_orgs",
        args=[_scheduler],
        replace_existing=True,
    )

    logger.info(
        "Pipeline scheduler started (groq=%s, anthropic=%s, discovery every %dm)",
        groq_ok,
        anthropic_ok,
        PIPELINE_ORG_DISCOVERY_INTERVAL_MINUTES,
    )
    return _scheduler


def shutdown_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        logger.info("Pipeline scheduler shut down")
        _scheduler = None


if __name__ == "__main__":
    from app.services.schedulers.run import main

    main()

