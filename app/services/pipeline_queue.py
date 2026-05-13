"""Redis queue for pipeline worker (optional — falls back to in-process)."""

from __future__ import annotations

import json
import logging
from uuid import UUID

from app.infrastructure.redis.client import get_redis

logger = logging.getLogger(__name__)

PIPELINE_QUEUE_KEY = "pulse:pipeline:queue"


async def enqueue_pipeline_job(
    *,
    run_id: UUID,
    org_id: UUID,
    trigger_source: str,
) -> bool:
    r = await get_redis()
    if r is None:
        return False
    payload = json.dumps(
        {
            "run_id": str(run_id),
            "org_id": str(org_id),
            "trigger_source": trigger_source,
        }
    )
    await r.rpush(PIPELINE_QUEUE_KEY, payload)
    logger.info("Enqueued pipeline job run_id=%s org_id=%s", run_id, org_id)
    return True
