import asyncio
import logging
from datetime import datetime, timezone

from croniter import croniter
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
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
from app.infrastructure.database.session import async_session_factory, get_db
from app.services.pipeline_queue import enqueue_introspection_job
from app.services.recommendation_service import (
    ClientDBError,
    generate_recommendations_for_org,
)
from app.api.routes.connections import _assert_live, _connection_to_response
from app.api.routes.schema_mappings import _mapping_to_response, _validate_mapping_payload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/onboarding", tags=["Onboarding"])


def _infer_col(columns: list, *patterns: str) -> str | None:
    """Find the first column whose name matches any of the given patterns."""
    col_map = {c.name.lower(): c.name for c in columns}
    for pat in patterns:
        for lower, original in col_map.items():
            if lower == pat or lower.endswith(f"_{pat}") or lower.startswith(f"{pat}_"):
                return original
    return None


async def _auto_create_schema_mapping(
    db: AsyncSession,
    org_id,
    connection_id,
    plaintext_dsn: str,
    sslmode: str | None,
    entity_label: str | None,
    goal_label: str | None,
) -> None:
    """Introspect the client DB and create (or update) a best-guess schema mapping."""
    try:
        tables = await introspect_schema(plaintext_dsn, sslmode=sslmode)
    except Exception:
        logger.warning("Schema introspection failed for connection %s — skipping auto-mapping", connection_id)
        return

    if not tables:
        return

    # Score tables: prefer the one whose name matches the entity label
    entity_kw = (entity_label or "").lower().strip()

    def _table_score(t) -> tuple:
        name = t.name.lower()
        if entity_kw and (entity_kw in name or name in entity_kw):
            return (3, len(t.columns))
        if any(kw in name for kw in ("customer", "user", "subscriber", "patient", "client", "member", "account", "employee", "product", "item", "sku")):
            return (2, len(t.columns))
        if any(kw in name for kw in ("log", "audit", "config", "setting", "migration", "session", "token", "permission")):
            return (0, len(t.columns))
        return (1, len(t.columns))

    best = max(tables, key=_table_score)
    cols = best.columns

    entity_id_col = _infer_col(cols, "id", "uuid", "key", "pk") or cols[0].name
    entity_name_col = _infer_col(cols, "name", "full_name", "fullname", "display_name", "title", "email", "username")
    timestamp_col = _infer_col(cols, "created_at", "timestamp", "date", "updated_at", "event_date", "recorded_at")

    goal_kw = (goal_label or "").lower()
    target_col = None
    if "churn" in goal_kw:
        target_col = _infer_col(cols, "churned", "churn", "is_active", "active", "status", "cancelled")
    elif any(kw in goal_kw for kw in ("stock", "inventor")):
        target_col = _infer_col(cols, "stock", "quantity", "inventory", "available", "qty")
    elif "risk" in goal_kw:
        target_col = _infer_col(cols, "risk", "risk_score", "risk_tier", "score")

    raw_schema = {
        "tables": [
            {"name": t.name, "columns": [{"name": c.name, "data_type": c.data_type, "nullable": c.nullable} for c in t.columns]}
            for t in tables
        ]
    }

    mapping_repo = SchemaMappingRepository(db)
    existing = await mapping_repo.list_by_org(org_id)
    if existing:
        await mapping_repo.update(
            existing[0].id,
            connection_id=connection_id,
            entity_table=best.name,
            entity_id_col=entity_id_col,
            entity_name_col=entity_name_col,
            timestamp_col=timestamp_col,
            target_column=target_col,
            raw_schema=raw_schema,
        )
    else:
        await mapping_repo.create(
            org_id=org_id,
            connection_id=connection_id,
            entity_table=best.name,
            entity_id_col=entity_id_col,
            entity_name_col=entity_name_col,
            timestamp_col=timestamp_col,
            target_column=target_col,
            raw_schema=raw_schema,
        )


async def _introspect_in_background(
    org_id,
    connection_id,
    plaintext_dsn: str,
    sslmode: str | None,
    entity_label: str | None,
    goal_label: str | None,
) -> None:
    """Run schema introspection in a background task with its own DB session."""
    try:
        async with async_session_factory() as db:
            await _auto_create_schema_mapping(
                db, org_id, connection_id, plaintext_dsn, sslmode, entity_label, goal_label
            )
            await db.commit()
    except Exception:
        logger.exception("Background schema introspection failed for connection %s", connection_id)


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
    """Save the org's business context — industry, entity label, and goal.

    Step 1 of the onboarding wizard. All fields are optional on each call so the
    frontend can save partial progress. The values are used later by the AI pipeline to
    personalise risk narratives and recommendations.
    """
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
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardingConnectionResponse:
    """Save and test a data source connection.

    Tests the connection immediately (credentials + reachability). Schema introspection
    runs in the background after this call returns — poll `GET /onboarding/connection/schema`
    to check when it is ready. This keeps response time under ~10 s regardless of how
    many tables the remote database contains.

    Returns 422 if the connection test fails. On success the connection `status` will be
    `"active"`.
    """
    repo = ConnectionRepository(db)
    built = build_encrypted_secret_and_row_fields(body)
    plaintext = built.pop("plaintext_secret")
    meta = built.get("connection_meta") or {}

    # Reuse the org's existing onboarding connection if one exists so we don't
    # create duplicates when the user goes back and resubmits this step.
    existing = await repo.list_by_org(current_user.org_id)
    live = [c for c in existing if c.deleted_at is None]
    if live:
        conn = max(live, key=lambda c: c.updated_at or c.created_at)
        await repo.update(
            conn.id,
            encrypted_dsn=encrypt_dsn(plaintext),
            name=body.name or conn.name,
            connector_type=built.get("connector_type"),
            connection_meta=meta,
        )
    else:
        await max_cloud_free_connections(db, current_user.org_id, 0)
        conn = await repo.create(
            org_id=current_user.org_id,
            encrypted_dsn=encrypt_dsn(plaintext),
            name=body.name or "My Connection",
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

    # Schema introspection (N+1 queries over the remote DB) runs in the background
    # so this endpoint returns immediately after the connection test passes.
    # Prefer the Redis-backed worker queue for durability; fall back to an in-process
    # FastAPI BackgroundTask when Redis is not configured (dev / no-Redis deployments).
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    queued = await enqueue_introspection_job(
        connection_id=conn.id,
        org_id=current_user.org_id,
    )
    if not queued:
        background_tasks.add_task(
            _introspect_in_background,
            org_id=current_user.org_id,
            connection_id=conn.id,
            plaintext_dsn=plaintext,
            sslmode=body.sslmode,
            entity_label=org.entity_label if org else None,
            goal_label=org.goal_label if org else None,
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
    """Return the schema (tables + columns) for the org's active connection.

    Serves from the cached schema stored during the background introspection triggered by
    `POST /onboarding/connection`. If the cache is not yet ready (background task still
    running), falls back to a live introspection query. Either way the response shape is
    the same — call this endpoint after `POST /onboarding/connection` succeeds.
    """
    conns = await ConnectionRepository(db).list_by_org(current_user.org_id)
    if not conns:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    conn = _pick_primary_connection_for_introspect(conns)
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": "This connector type does not support schema introspection"},
        )

    # Serve from the raw_schema cached by the background introspection task when available.
    mappings = await SchemaMappingRepository(db).list_by_org(current_user.org_id)
    if mappings:
        raw = mappings[0].raw_schema or {}
        tables_data = raw.get("tables") or []
        if tables_data:
            return IntrospectResponse.model_validate({"tables": tables_data})

    # Background task hasn't finished yet — fall back to live introspection.
    tables = await introspect_schema(decrypt_dsn(conn.encrypted_dsn), sslmode=conn.sslmode)
    return IntrospectResponse(tables=tables)


@router.post("/schema-mapping", response_model=OnboardingSchemaMappingResponse)
async def save_schema_mapping(
    body: CreateSchemaMappingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardingSchemaMappingResponse:
    """Confirm the schema mapping for the active connection.

    Step 3 of the onboarding wizard. The frontend should pre-fill this form using the
    schema returned by `GET /onboarding/connection/schema` and the auto-inferred mapping
    values. The user can correct any field before submitting.

    `entity_table` is the primary table to profile. `entity_id_col` must uniquely identify
    each entity row. `signal_columns` is an optional dict mapping column names to semantic
    labels (e.g. `{"last_payment_date": "recency", "total_spend": "value"}`).
    """
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
    """Finalise onboarding and trigger the first pipeline run.

    Step 4 (final step) of the onboarding wizard. Marks the org as onboarded, creates the
    default pipeline schedule (every 6 hours), generates an initial batch of recommendations
    synchronously, then kicks off a full pipeline run in the background.

    Returns 409 if onboarding is already complete. Returns the number of recommendations
    generated in `generated_recommendations`.
    """
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

