import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.schema_mapping import (
    CreateSchemaMappingRequest,
    SchemaMappingResponse,
    UpdateSchemaMappingRequest,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.connection_repository import (
    ConnectionRepository,
)
from app.infrastructure.database.repositories.schema_mapping_repository import (
    SchemaMappingRepository,
)
from app.infrastructure.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/schema-mappings", tags=["schema-mappings"])


def _mapping_to_response(m) -> SchemaMappingResponse:
    return SchemaMappingResponse(
        id=m.id,
        org_id=m.org_id,
        connection_id=m.connection_id,
        entity_table=m.entity_table,
        entity_id_col=m.entity_id_col,
        entity_name_col=m.entity_name_col,
        signal_columns=m.signal_columns,
        timestamp_col=m.timestamp_col,
        risk_config=m.risk_config,
        raw_schema=m.raw_schema,
        created_at=m.created_at,
    )


@router.get("", response_model=list[SchemaMappingResponse])
async def list_schema_mappings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SchemaMappingResponse]:
    mappings = await SchemaMappingRepository(db).list_by_org(current_user.org_id)
    return [_mapping_to_response(m) for m in mappings]


@router.get("/{mapping_id}", response_model=SchemaMappingResponse)
async def get_schema_mapping(
    mapping_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SchemaMappingResponse:
    from uuid import UUID
    mapping = await SchemaMappingRepository(db).get_by_id(UUID(mapping_id))
    if not mapping or mapping.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema mapping not found")
    return _mapping_to_response(mapping)


@router.post("", response_model=SchemaMappingResponse, status_code=201)
async def create_schema_mapping(
    body: CreateSchemaMappingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SchemaMappingResponse:
    conn = await ConnectionRepository(db).get_by_id(body.connection_id)
    if not conn or conn.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")

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
    )
    await db.commit()
    return _mapping_to_response(mapping)


@router.patch("/{mapping_id}", response_model=SchemaMappingResponse)
async def update_schema_mapping(
    mapping_id: str,
    body: UpdateSchemaMappingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SchemaMappingResponse:
    from uuid import UUID
    mapping = await SchemaMappingRepository(db).get_by_id(UUID(mapping_id))
    if not mapping or mapping.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema mapping not found")

    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field must be provided",
        )
    for key, value in payload.items():
        setattr(mapping, key, value)
    await db.flush()
    await db.commit()
    return _mapping_to_response(mapping)


@router.delete("/{mapping_id}", status_code=204)
async def delete_schema_mapping(
    mapping_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from uuid import UUID
    mapping = await SchemaMappingRepository(db).get_by_id(UUID(mapping_id))
    if not mapping or mapping.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema mapping not found")
    await SchemaMappingRepository(db).delete(UUID(mapping_id))
    await db.commit()
