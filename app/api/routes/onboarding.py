import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.connection import CreateConnectionRequest, IntrospectResponse
from app.api.schemas.onboarding import (
    CompleteOnboardingResponse,
    OnboardingConnectionResponse,
    OnboardingContextRequest,
    OnboardingSchemaMappingResponse,
)
from app.api.schemas.schema_mapping import CreateSchemaMappingRequest
from app.infrastructure.crypto import decrypt_dsn, encrypt_dsn
from app.infrastructure.database.connection_tester import introspect_schema, test_connection
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
from app.api.routes.connections import _connection_to_response, _make_dsn
from app.api.routes.schema_mappings import _mapping_to_response, _validate_mapping_payload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.put("/context")
async def save_context(
    body: OnboardingContextRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    org.industry = body.industry
    org.business_context = body.business_context
    org.entity_label = body.entity_label
    org.goal_label = body.goal_label
    await db.flush()
    await db.commit()
    return {"message": "Business context saved"}


@router.post("/connection", response_model=OnboardingConnectionResponse)
async def save_and_test_connection(
    body: CreateConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardingConnectionResponse:
    dsn = _make_dsn(
        body.db_type,
        body.host,
        body.port,
        body.database_name,
        body.username,
        body.password,
    )
    conn = await ConnectionRepository(db).create(
        org_id=current_user.org_id,
        db_type=body.db_type,
        host=body.host,
        port=body.port,
        database_name=body.database_name,
        username=body.username,
        encrypted_dsn=encrypt_dsn(dsn),
        sslmode=body.sslmode,
    )
    success, message, db_version = await test_connection(dsn, body.sslmode)
    conn.status = "active" if success else "failed"
    from datetime import datetime, timezone

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
    conn = conns[-1]
    tables = await introspect_schema(decrypt_dsn(conn.encrypted_dsn), conn.sslmode)
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
    try:
        generated = await generate_recommendations_for_org(db, current_user.org_id)
    except ClientDBError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    org.onboarding_done = True
    await db.commit()

    # Schedule recurring pipeline runs and trigger the first autonomous run
    from app.services.schedulers.pipeline_scheduler import schedule_org, trigger_pipeline_now

    schedule_org(current_user.org_id, org.name)
    await trigger_pipeline_now(current_user.org_id, trigger_source="onboarding")

    return CompleteOnboardingResponse(
        message="Onboarding complete",
        onboarding_done=True,
        generated_recommendations=generated,
    )

