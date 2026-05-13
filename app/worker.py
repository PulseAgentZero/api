"""Background worker — consumes pipeline jobs from Redis (docker `command: worker`)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from uuid import UUID

from app.agents.orchestrators.pipeline import PipelineOrchestrator
from app.config.settings import settings
from app.infrastructure.database.session import async_session_factory
from app.infrastructure.logging import configure_logging
from app.infrastructure.redis.client import close_redis, get_redis
from app.services.pipeline_queue import PIPELINE_QUEUE_KEY

configure_logging()
logger = logging.getLogger(__name__)


async def _loop() -> None:
    while True:
        r = await get_redis()
        if r is None:
            logger.error("REDIS_URL not set — worker sleeping")
            await asyncio.sleep(30)
            continue
        try:
            item = await r.brpop(PIPELINE_QUEUE_KEY, timeout=5)
        except Exception:
            logger.exception("Redis BRPOP failed")
            await asyncio.sleep(5)
            continue
        if not item:
            continue
        _, raw = item
        try:
            data = json.loads(raw)
            org_id = UUID(data["org_id"])
            run_id = UUID(data["run_id"])
            trigger_source = data.get("trigger_source", "manual")
        except Exception:
            logger.warning("Bad queue payload: %s", raw)
            continue
        try:
            async with async_session_factory() as session:
                orch = PipelineOrchestrator(session)
                await orch.execute(org_id, trigger_source=trigger_source, run_id=run_id)
        except Exception:
            logger.exception("Pipeline run %s failed for org %s", run_id, org_id)


async def _main() -> None:
    try:
        await _loop()
    finally:
        await close_redis()


def main() -> None:
    if not settings.REDIS_URL:
        logger.error("REDIS_URL is required for the worker process")
        sys.exit(1)
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("Worker interrupted")


if __name__ == "__main__":
    main()
