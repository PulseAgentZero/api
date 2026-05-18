"""Background worker — consumes pipeline and introspection jobs from Redis (docker `command: worker`)."""

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
from app.services.pipeline_queue import (
    INTROSPECTION_QUEUE_KEY,
    PIPELINE_QUEUE_KEY,
    STUDIO_QUERY_QUEUE_KEY,
)

configure_logging()
logger = logging.getLogger(__name__)

_ALL_QUEUES = [PIPELINE_QUEUE_KEY, INTROSPECTION_QUEUE_KEY, STUDIO_QUERY_QUEUE_KEY]


async def _handle_pipeline(data: dict) -> None:
    org_id = UUID(data["org_id"])
    run_id = UUID(data["run_id"])
    trigger_source = data.get("trigger_source", "manual")
    async with async_session_factory() as session:
        orch = PipelineOrchestrator(session)
        await orch.execute(org_id, trigger_source=trigger_source, run_id=run_id)


async def _handle_introspection(data: dict) -> None:
    from app.infrastructure.crypto import decrypt_dsn
    from app.infrastructure.database.repositories.connection_repository import ConnectionRepository
    from app.infrastructure.database.repositories.organization_repository import OrganizationRepository
    from app.services.schema_introspection import auto_create_schema_mapping

    connection_id = UUID(data["connection_id"])
    org_id = UUID(data["org_id"])

    async with async_session_factory() as session:
        conn = await ConnectionRepository(session).get_by_id(connection_id)
        if not conn or conn.org_id != org_id or not conn.encrypted_dsn:
            logger.warning("Introspection job: connection %s not found or has no DSN", connection_id)
            return
        org = await OrganizationRepository(session).get_by_id(org_id)
        plaintext = decrypt_dsn(conn.encrypted_dsn)
        await auto_create_schema_mapping(
            session,
            org_id=org_id,
            connection_id=connection_id,
            plaintext_dsn=plaintext,
            sslmode=conn.sslmode,
            entity_label=org.entity_label if org else None,
            goal_label=org.goal_label if org else None,
        )
        await session.commit()
    logger.info("Introspection complete connection_id=%s org_id=%s", connection_id, org_id)


async def _handle_studio_query(data: dict) -> None:
    """Execute a saved studio query and store results in Redis."""
    import json as _json

    from app.infrastructure.database.models.studio_query import StudioQuery
    from app.infrastructure.database.models.studio_query_run import StudioQueryRun
    from app.infrastructure.database.repositories.studio_query_run_repository import (
        StudioQueryRunRepository,
    )
    from app.infrastructure.redis.keys import studio_run_result
    from app.services.studio_query_service import execute_studio_query

    run_id = UUID(data["run_id"])
    query_id = UUID(data["query_id"])
    org_id = UUID(data["org_id"])
    param_values: dict = data.get("param_values") or {}

    async with async_session_factory() as session:
        run_repo = StudioQueryRunRepository(session)
        run = await run_repo.get_by_id(run_id)
        if not run:
            logger.warning("Studio query run %s not found — skipping", run_id)
            return

        q = await session.get(StudioQuery, query_id)
        if not q:
            await run_repo.mark_failed(run, "Query not found")
            await session.commit()
            return

        await run_repo.mark_running(run)
        await session.commit()

        redis = await get_redis()
        try:
            result = await execute_studio_query(
                session,
                org_id,
                q.connection_id,
                q.sql_text,
                param_defs=q.params or [],
                param_values=param_values,
                page=1,
                page_size=5000,
                redis=None,  # Skip cache for the raw result — we store it ourselves below
            )
            # Store full result in Redis keyed by run_id (TTL 1 hour)
            if redis is not None:
                try:
                    await redis.set(
                        studio_run_result(str(run_id)),
                        _json.dumps(result, default=str),
                        ex=3600,
                    )
                except Exception:
                    logger.warning("Could not cache studio run result %s", run_id, exc_info=True)

            await run_repo.mark_completed(run, result["total"])
            await session.commit()
            logger.info(
                "Studio query run %s completed — %d rows", run_id, result["total"]
            )
        except Exception as exc:
            await run_repo.mark_failed(run, str(exc))
            await session.commit()
            logger.exception("Studio query run %s failed", run_id)


async def _loop() -> None:
    while True:
        r = await get_redis()
        if r is None:
            logger.error("REDIS_URL not set — worker sleeping")
            await asyncio.sleep(30)
            continue
        try:
            item = await r.brpop(_ALL_QUEUES, timeout=5)
        except Exception:
            logger.exception("Redis BRPOP failed")
            await asyncio.sleep(5)
            continue
        if not item:
            continue
        queue_key, raw = item
        queue_key = queue_key.decode() if isinstance(queue_key, bytes) else queue_key
        try:
            data = json.loads(raw)
        except Exception:
            logger.warning("Bad queue payload on %s: %s", queue_key, raw)
            continue

        job_type = data.get("job_type", "pipeline" if queue_key == PIPELINE_QUEUE_KEY else None)
        try:
            if job_type == "studio_query" or queue_key == STUDIO_QUERY_QUEUE_KEY:
                await _handle_studio_query(data)
            elif job_type == "introspection" or queue_key == INTROSPECTION_QUEUE_KEY:
                await _handle_introspection(data)
            else:
                await _handle_pipeline(data)
        except Exception:
            logger.exception("Job failed on queue=%s job_type=%s", queue_key, job_type)


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
