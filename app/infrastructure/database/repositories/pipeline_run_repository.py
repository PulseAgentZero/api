from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.base import touch_updated_at
from app.infrastructure.database.models.pipeline_run import PipelineRun

# Runs in these states are considered "in flight" for dedup purposes
ACTIVE_STATUSES = ("queued", "running")


class PipelineRunRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, run_id: UUID) -> PipelineRun | None:
        return await self.db.get(PipelineRun, run_id)

    async def count_active_for_org(self, org_id: UUID) -> int:
        from sqlalchemy import func

        return int(
            await self.db.scalar(
                select(func.count())
                .select_from(PipelineRun)
                .where(PipelineRun.org_id == org_id)
                .where(PipelineRun.status.in_(ACTIVE_STATUSES))
            )
            or 0
        )

    async def get_active_for_org(self, org_id: UUID) -> PipelineRun | None:
        stmt = (
            select(PipelineRun)
            .where(PipelineRun.org_id == org_id)
            .where(PipelineRun.status.in_(ACTIVE_STATUSES))
            .order_by(PipelineRun.created_at.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_org(
        self, org_id: UUID, *, limit: int = 25
    ) -> list[PipelineRun]:
        stmt = (
            select(PipelineRun)
            .where(PipelineRun.org_id == org_id)
            .order_by(PipelineRun.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def create_queued(
        self,
        org_id: UUID,
        *,
        trigger_source: str,
        mapping_id: UUID | None = None,
        triggered_by: UUID | None = None,
    ) -> PipelineRun:
        run = PipelineRun(
            org_id=org_id,
            status="queued",
            trigger_source=trigger_source,
            current_step="queued",
            mapping_id=mapping_id,
            triggered_by=triggered_by,
        )
        self.db.add(run)
        await self.db.flush()
        return run

    async def mark_running(self, run: PipelineRun) -> None:
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        run.current_step = "starting"
        touch_updated_at(run)
        await self.db.flush()

    async def update(self, run: PipelineRun, **fields: Any) -> None:
        for key, value in fields.items():
            if hasattr(run, key):
                setattr(run, key, value)
        touch_updated_at(run)
        await self.db.flush()

    async def finalize(
        self,
        run: PipelineRun,
        *,
        status: str,
        error: str | None,
        current_step: str | None,
        duration_ms: int,
        entities_scored: int,
        critical_count: int,
        high_count: int,
        recommendations_generated: int,
        total_llm_calls: int,
        total_tool_calls: int,
        total_tokens: int,
        provider_fallbacks: int,
        step_metrics: list | None,
        generation_caps: dict | None,
        rag_metrics: dict | None = None,
    ) -> None:
        run.status = status
        run.error = error
        run.current_step = current_step
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = duration_ms
        run.entities_scored = entities_scored
        run.critical_count = critical_count
        run.high_count = high_count
        run.recommendations_generated = recommendations_generated
        run.total_llm_calls = total_llm_calls
        run.total_tool_calls = total_tool_calls
        run.total_tokens = total_tokens
        run.provider_fallbacks = provider_fallbacks
        run.step_metrics = step_metrics
        run.generation_caps = generation_caps
        run.rag_metrics = rag_metrics
        touch_updated_at(run)
        await self.db.flush()
