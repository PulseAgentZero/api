import logging
from datetime import datetime, timezone

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.dependencies.plan_gate import max_cloud_free_connections
from app.api.schemas.connection import CreateConnectionRequest, IntrospectResponse
from app.api.schemas.onboarding import (
    CompleteOnboardingResponse,
    OnboardingConnectionResponse,
    OnboardingContextRequest,
    OnboardingSchemaMappingResponse,
)
from app.api.schemas.schema_mapping import CreateSchemaMappingRequest
from app.infrastructure.connectors.factory import build_encrypted_secret_and_row_fields
from app.infrastructure.crypto import decrypt_dsn, encrypt_dsn
from app.infrastructure.database.connection_tester import introspect_schema, test_connection
from app.infrastructure.database.models.connection import Connection
from app.infrastructure.database.models.pipeline_schedule import PipelineSchedule
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.connection_repository import (
    ConnectionRepository,
)
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.repositories.schema_mapping_repository import (
    SchemaMappingRepository,
)
from app.infrastructure.database.session import get_db
from app.services.recommendation_service import (
    ClientDBError,
    generate_recommendations_for_org,
)
from app.api.routes.connections import _assert_live, _connection_to_response
from app.api.routes.schema_mappings import _mapping_to_response, _validate_mapping_payload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/onboarding", tags=["Onboarding"])


def _pick_primary_connection_for_introspect(conns: list[Connection]) -> Connection | None:
    """Prefer active DB connections with a DSN, then most recently updated."""
    live = [c for c in conns if c.deleted_at is None and c.encrypted_dsn]
    if not live:
        return None
    active = [c for c in live if c.status == "active"]
    pool = active if active else live
    return max(pool, key=lambda c: (c.updated_at or c.created_at))


@router.put("/context")
async def save_context(
    body: OnboardingContextRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return {"message": "No fields to update"}
    if "industry" in updates:
        org.industry = updates["industry"]
    if "business_context" in updates:
        org.business_context = updates["business_context"]
    if "entity_label" in updates:
        org.entity_label = updates["entity_label"]
    if "goal_label" in updates:
        org.goal_label = updates["goal_label"]
    await db.flush()
    await db.commit()
    return {"message": "Business context saved"}


@router.post("/connection", response_model=OnboardingConnectionResponse)
async def save_and_test_connection(
    body: CreateConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardingConnectionResponse:
    repo = ConnectionRepository(db)
    await max_cloud_free_connections(db, current_user.org_id, await repo.count_active(current_user.org_id))
    built = build_encrypted_secret_and_row_fields(body)
    plaintext = built.pop("plaintext_secret")
    meta = built.pop("connection_meta", None) or {}
    conn = await repo.create(
        org_id=current_user.org_id,
        encrypted_dsn=encrypt_dsn(plaintext),
        name=body.name or "My Connection",
        sslmode=body.sslmode,
        db_type=built.get("db_type"),
        host=built.get("host"),
        port=built.get("port"),
        database_name=built.get("database_name"),
        username=built.get("username"),
        connector_type=built.get("connector_type"),
        connection_meta=meta,
    )
    success, message, db_version = await test_connection(plaintext, body.sslmode)
    conn.status = "active" if success else "failed"
    conn.last_tested_at = datetime.now(timezone.utc)
    await db.commit()
    if not success:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"success": False, "message": message, "connection_id": str(conn.id)},
        )
    return OnboardingConnectionResponse(
        connection=_connection_to_response(conn),
        success=True,
        message=message,
        db_version=db_version,
    )


@router.get("/connection/schema", response_model=IntrospectResponse)
async def get_connection_schema(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IntrospectResponse:
    conns = await ConnectionRepository(db).list_by_org(current_user.org_id)
    if not conns:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    conn = _pick_primary_connection_for_introspect(conns)
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": "This connector type does not support schema introspection"},
        )
    tables = await introspect_schema(decrypt_dsn(conn.encrypted_dsn), sslmode=conn.sslmode)
    return IntrospectResponse(tables=tables)


@router.post("/schema-mapping", response_model=OnboardingSchemaMappingResponse)
async def save_schema_mapping(
    body: CreateSchemaMappingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardingSchemaMappingResponse:
    conn = await ConnectionRepository(db).get_by_id(body.connection_id)
    if not conn or conn.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    _assert_live(conn)

    _validate_mapping_payload(body.model_dump())
    mapping = await SchemaMappingRepository(db).create(
        org_id=current_user.org_id,
        connection_id=body.connection_id,
        entity_table=body.entity_table,
        entity_id_col=body.entity_id_col,
        entity_name_col=body.entity_name_col,
        signal_columns=body.signal_columns,
        timestamp_col=body.timestamp_col,
        risk_config=body.risk_config,
        raw_schema=body.raw_schema,
        target_column=body.target_column,
    )
    await db.commit()
    return OnboardingSchemaMappingResponse(schema_mapping=_mapping_to_response(mapping))


@router.post("/complete", response_model=CompleteOnboardingResponse)
async def complete_onboarding(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompleteOnboardingResponse:
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    if org.onboarding_done:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Onboarding is already complete for this organization",
        )

    conns = await ConnectionRepository(db).list_by_org(current_user.org_id)
    active_conn = next((c for c in conns if c.deleted_at is None and c.status == "active"), None)
    active_map = None
    if active_conn is not None:
        mappings = await SchemaMappingRepository(db).list_by_org(current_user.org_id)
        active_map = next(
            (m for m in mappings if m.is_active and m.connection_id == active_conn.id),
            None,
        )

    generated = 0
    msg = "Onboarding complete"
    if active_conn is not None and active_map is not None:
        sch_r = await db.execute(
            select(PipelineSchedule).where(PipelineSchedule.org_id == current_user.org_id).limit(1)
        )
        if sch_r.scalar_one_or_none() is None:
            now = datetime.now(timezone.utc)
            tz = org.timezone or "UTC"
            nxt = croniter("0 */6 * * *", now).get_next(datetime)
            db.add(
                PipelineSchedule(
                    org_id=current_user.org_id,
                    mapping_id=active_map.id,
                    cron_expression="0 */6 * * *",
                    timezone=tz,
                    is_active=True,
                    next_run_at=nxt,
                )
            )
            await db.flush()

        try:
            generated = await generate_recommendations_for_org(db, current_user.org_id)
        except ClientDBError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    else:
        msg = (
            "Onboarding complete. Add an active connection and schema mapping under Connections "
            "when you are ready to run the pipeline and generate recommendations."
        )

    org.onboarding_done = True
    await db.commit()

    if active_conn is not None and active_map is not None:
        from app.services.schedulers.pipeline_scheduler import schedule_org, trigger_pipeline_now

        schedule_org(current_user.org_id, org.name)
        await trigger_pipeline_now(current_user.org_id, trigger_source="onboarding")

    return CompleteOnboardingResponse(
        message=msg,
        onboarding_done=True,
        generated_recommendations=generated,
    )

