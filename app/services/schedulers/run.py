"""Unified entry point for all background APScheduler cron jobs.

Run via docker compose `scheduler`, self-hosted supervisord, or::

    python -m app.services.schedulers.run
"""

from __future__ import annotations

import asyncio
import logging
import signal

from app.infrastructure.logging import configure_logging
from app.infrastructure.logging.streams import start_log_stream_runtime, stop_log_stream_runtime
from app.services.schedulers.heartbeat import heartbeat_loop
from app.services.schedulers.billing_scheduler import (
    shutdown_billing_scheduler,
    start_billing_scheduler,
)
from app.services.schedulers.ldap_scheduler import (
    shutdown_ldap_sync_scheduler,
    start_ldap_sync_scheduler,
)
from app.services.schedulers.license_scheduler import (
    shutdown_license_scheduler,
    start_license_scheduler,
)
from app.services.schedulers.pending_license_issuance_scheduler import (
    shutdown_pending_license_issuance_scheduler,
    start_pending_license_issuance_scheduler,
)
from app.services.schedulers.memory_prune_scheduler import (
    shutdown_memory_prune_scheduler,
    start_memory_prune_scheduler,
)
from app.services.schedulers.pipeline_scheduler import (
    shutdown_scheduler as shutdown_pipeline_scheduler,
    start_pipeline_scheduler,
)
from app.services.schedulers.studio_refresh_scheduler import (
    shutdown_studio_refresh_scheduler,
    start_studio_refresh_scheduler,
)
from app.services.schedulers.usage_reset_scheduler import (
    shutdown_usage_reset_scheduler,
    start_usage_reset_scheduler,
)

logger = logging.getLogger(__name__)


async def _safe_start(name: str, coro_factory) -> None:
    """Run a scheduler start function, logging any error without raising.

    A single misconfigured scheduler must never prevent the others (and the
    process-level heartbeat) from running.
    """
    try:
        result = coro_factory()
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.exception("Failed to start %s scheduler — continuing", name)


async def start_all_schedulers() -> None:
    await _safe_start("pipeline", start_pipeline_scheduler)
    await _safe_start("studio_refresh", start_studio_refresh_scheduler)
    await _safe_start("memory_prune", start_memory_prune_scheduler)
    await _safe_start("billing", start_billing_scheduler)
    await _safe_start("usage_reset", start_usage_reset_scheduler)
    await _safe_start("license", start_license_scheduler)
    await _safe_start("pending_license_issuance", start_pending_license_issuance_scheduler)
    await _safe_start("ldap_sync", start_ldap_sync_scheduler)


def _safe_shutdown(name: str, fn) -> None:
    try:
        fn()
    except Exception:
        logger.exception("Failed to shut down %s scheduler", name)


def shutdown_all_schedulers() -> None:
    _safe_shutdown("ldap_sync", shutdown_ldap_sync_scheduler)
    _safe_shutdown("license", shutdown_license_scheduler)
    _safe_shutdown("pending_license_issuance", shutdown_pending_license_issuance_scheduler)
    _safe_shutdown("usage_reset", shutdown_usage_reset_scheduler)
    _safe_shutdown("billing", shutdown_billing_scheduler)
    _safe_shutdown("memory_prune", shutdown_memory_prune_scheduler)
    _safe_shutdown("studio_refresh", shutdown_studio_refresh_scheduler)
    _safe_shutdown("pipeline", shutdown_pipeline_scheduler)


async def _run_until_stopped() -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # Start the process-level heartbeat FIRST so the UI sees "healthy" even if
    # individual scheduler starts are slow or fail.
    heartbeat_task = asyncio.create_task(heartbeat_loop(stop), name="scheduler_heartbeat")

    try:
        await start_log_stream_runtime()
    except Exception:
        logger.exception("Failed to start log stream runtime — continuing")

    await start_all_schedulers()
    logger.info(
        "All schedulers running — waiting for stop signal",
        extra={"event_category": "system"},
    )
    await stop.wait()
    shutdown_all_schedulers()
    try:
        await stop_log_stream_runtime()
    except Exception:
        logger.exception("Failed to stop log stream runtime cleanly")

    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except (asyncio.CancelledError, Exception):
        pass

    logger.info("All schedulers stopped", extra={"event_category": "system"})


async def _run_with_logging() -> None:
    configure_logging()
    await _run_until_stopped()


def main() -> None:
    asyncio.run(_run_with_logging())


if __name__ == "__main__":
    main()
