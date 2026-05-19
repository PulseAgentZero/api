"""Organization setup completion — pipeline schedule, recommendations, first run."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from croniter import croniter
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.pipeline_schedule import PipelineSchedule
from app.infrastructure.database.repositories.connection_repository import ConnectionRepository
from app.infrastructure.database.repositories.organization_repository import OrganizationRepository
from app.infrastructure.database.repositories.schema_mapping_repository import SchemaMappingRepository
from app.services.recommendation_service import ClientDBError, generate_recommendations_for_org

logger = logging.getLogger(__name__)


@dataclass
class CompleteSetupResult:
    message: str
    onboarding_done: bool
    generated_recommendations: int = 0
    already_complete: bool = False


async def complete_org_setup(
    db: AsyncSession,
    org_id: UUID,
    *,
    completed_by: UUID | None = None,
) -> CompleteSetupResult:
    """Mark org setup complete; create schedule and trigger pipeline when ready."""
    org = await OrganizationRepository(db).get_by_id(org_id)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    if org.onboarding_done:
        return CompleteSetupResult(
            message="Setup is already complete for this organization",
            onboarding_done=True,
            already_complete=True,
        )

    conns = await ConnectionRepository(db).list_by_org(org_id)
    active_conn = next((c for c in conns if c.deleted_at is None and c.status == "active"), None)
    active_map = None
    if active_conn is not None:
        mappings = await SchemaMappingRepository(db).list_by_org(org_id)
        active_map = next(
            (m for m in mappings if m.is_active and m.connection_id == active_conn.id),
            None,
        )

    generated = 0
    msg = "Setup complete"
    if active_conn is not None and active_map is not None:
        sch_r = await db.execute(
            select(PipelineSchedule).where(PipelineSchedule.org_id == org_id).limit(1)
        )
        if sch_r.scalar_one_or_none() is None:
            now = datetime.now(timezone.utc)
            tz = org.timezone or "UTC"
            nxt = croniter("0 */6 * * *", now).get_next(datetime)
            db.add(
                PipelineSchedule(
                    org_id=org_id,
                    mapping_id=active_map.id,
                    cron_expression="0 */6 * * *",
                    timezone=tz,
                    is_active=True,
                    next_run_at=nxt,
                )
            )
            await db.flush()

        try:
            generated = await generate_recommendations_for_org(db, org_id)
        except ClientDBError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    else:
        msg = (
            "Setup complete. Add an active connection and schema mapping under Connections "
            "when you are ready to run the pipeline and generate recommendations."
        )

    org.onboarding_done = True
    from app.infrastructure.audit import log_audit

    await log_audit(
        db,
        org_id=org_id,
        user_id=completed_by,
        action="org.onboarding_completed",
        resource="organization",
        resource_id=org_id,
        metadata={"generated_recommendations": generated},
    )
    await db.commit()

    if active_conn is not None and active_map is not None:
        from app.services.schedulers.pipeline_scheduler import trigger_pipeline_now

        # Interval cron is registered by the dedicated scheduler process (periodic org discovery).
        await trigger_pipeline_now(org_id, trigger_source="setup")

    return CompleteSetupResult(
        message=msg,
        onboarding_done=True,
        generated_recommendations=generated,
    )


def _has_business_context(org: Organization) -> bool:
    return bool((org.business_context or "").strip())


async def try_auto_complete_setup(db: AsyncSession, org_id: UUID) -> CompleteSetupResult | None:
    """Complete setup when business context and an active connection exist."""
    org = await OrganizationRepository(db).get_by_id(org_id)
    if not org or org.onboarding_done or not _has_business_context(org):
        return None

    conns = await ConnectionRepository(db).list_by_org(org_id)
    has_active = any(c.deleted_at is None and c.status == "active" for c in conns)
    if not has_active:
        return None

    return await complete_org_setup(db, org_id)
