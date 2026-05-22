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


async def start_all_schedulers() -> None:
    await start_pipeline_scheduler()
    await start_studio_refresh_scheduler()
    await start_memory_prune_scheduler()
    await start_billing_scheduler()
    await start_usage_reset_scheduler()
    await start_license_scheduler()
    start_ldap_sync_scheduler()


def shutdown_all_schedulers() -> None:
    shutdown_ldap_sync_scheduler()
    shutdown_license_scheduler()
    shutdown_usage_reset_scheduler()
    shutdown_billing_scheduler()
    shutdown_memory_prune_scheduler()
    shutdown_studio_refresh_scheduler()
    shutdown_pipeline_scheduler()


async def _run_until_stopped() -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await start_log_stream_runtime()
    await start_all_schedulers()
    logger.info(
        "All schedulers running — waiting for stop signal",
        extra={"event_category": "system"},
    )
    await stop.wait()
    shutdown_all_schedulers()
    await stop_log_stream_runtime()
    logger.info("All schedulers stopped", extra={"event_category": "system"})


async def _run_with_logging() -> None:
    configure_logging()
    await _run_until_stopped()


def main() -> None:
    asyncio.run(_run_with_logging())


if __name__ == "__main__":
    main()
