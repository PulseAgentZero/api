"""Redis queue for pipeline worker (optional — falls back to in-process)."""

from __future__ import annotations

import json
import logging
from uuid import UUID

from app.infrastructure.redis.client import get_redis

logger = logging.getLogger(__name__)

PIPELINE_QUEUE_KEY = "pulse:pipeline:queue"
INTROSPECTION_QUEUE_KEY = "pulse:introspection:queue"
STUDIO_QUERY_QUEUE_KEY = "pulse:studio:query:queue"


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


async def enqueue_studio_query_job(
    *,
    run_id: UUID,
    query_id: UUID,
    org_id: UUID,
    param_values: dict,
) -> bool:
    """Enqueue a studio query execution job for the worker.

    Returns False if Redis is unavailable (caller should fall back to sync execution).
    No plaintext SQL or credentials are placed in the queue — worker fetches from DB.
    """
    r = await get_redis()
    if r is None:
        return False
    payload = json.dumps(
        {
            "job_type": "studio_query",
            "run_id": str(run_id),
            "query_id": str(query_id),
            "org_id": str(org_id),
            "param_values": param_values,
        }
    )
    await r.rpush(STUDIO_QUERY_QUEUE_KEY, payload)
    logger.info("Enqueued studio query job run_id=%s query_id=%s", run_id, query_id)
    return True


async def enqueue_introspection_job(
    *,
    connection_id: UUID,
    org_id: UUID,
) -> bool:
    """Enqueue a schema introspection job for the worker to process.

    The worker fetches and decrypts credentials itself — no plaintext DSN in Redis.
    Returns False if Redis is unavailable (caller should fall back to in-process).
    """
    r = await get_redis()
    if r is None:
        return False
    payload = json.dumps(
        {
            "job_type": "introspection",
            "connection_id": str(connection_id),
            "org_id": str(org_id),
        }
    )
    await r.rpush(INTROSPECTION_QUEUE_KEY, payload)
    logger.info("Enqueued introspection job connection_id=%s org_id=%s", connection_id, org_id)
    return True
