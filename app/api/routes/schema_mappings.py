import logging
import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.schema_mapping import (
    CreateSchemaMappingRequest,
    SchemaMappingResponse,
    UpdateSchemaMappingRequest,
)
from app.infrastructure.database.base import touch_updated_at
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.connection_repository import (
    ConnectionRepository,
)
from app.infrastructure.database.repositories.schema_mapping_repository import (
    SchemaMappingRepository,
)
from app.infrastructure.database.session import get_db
from app.services.pipeline_trigger import maybe_trigger_initial_pipeline

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/schema-mappings", tags=["Schema Mappings"])

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_uuid(value: str, field_name: str):
    from uuid import UUID

    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid {field_name}") from exc


def _validate_mapping_payload(payload: dict) -> None:
    identifiers = [
        ("entity_table", payload.get("entity_table")),
        ("entity_id_col", payload.get("entity_id_col")),
        ("entity_name_col", payload.get("entity_name_col")),
        ("timestamp_col", payload.get("timestamp_col")),
    ]
    signal_columns = payload.get("signal_columns") or {}
    identifiers.extend((f"signal_columns.{key}", value) for key, value in signal_columns.items())
    for label, value in identifiers:
        if value is not None and not _IDENTIFIER.fullmatch(str(value)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid SQL identifier for {label}: {value!r}",
            )

    raw_schema = payload.get("raw_schema") or {}
    tables = raw_schema.get("tables") if isinstance(raw_schema, dict) else None
    if not tables:
        return
    table = payload.get("entity_table")
    table_info = next((item for item in tables if item.get("name") == table), None)
    if not table_info:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Mapped entity_table does not exist in raw_schema",
        )
    columns = {column.get("name") for column in table_info.get("columns", [])}
    for label, value in identifiers[1:]:
        if value is not None and value not in columns:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Mapped column for {label} does not exist in raw_schema",
            )


def _mapping_to_response(
    m,
    *,
    pipeline_triggered: bool = False,
    pipeline_run_id=None,
) -> SchemaMappingResponse:
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
        target_column=m.target_column,
        rag_config=m.rag_config,
        created_at=m.created_at,
        pipeline_triggered=pipeline_triggered,
        pipeline_run_id=pipeline_run_id,
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
    mapping = await SchemaMappingRepository(db).get_by_id(_parse_uuid(mapping_id, "mapping_id"))
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
        rag_config=body.rag_config,
    )
    await db.commit()

    triggered, run_id_str = await maybe_trigger_initial_pipeline(
        db,
        current_user.org_id,
        mapping_id=mapping.id,
        triggered_by=current_user.id,
    )
    pipeline_run_id = UUID(run_id_str) if run_id_str else None

    return _mapping_to_response(
        mapping,
        pipeline_triggered=triggered,
        pipeline_run_id=pipeline_run_id,
    )


@router.patch("/{mapping_id}", response_model=SchemaMappingResponse)
async def update_schema_mapping(
    mapping_id: str,
    body: UpdateSchemaMappingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SchemaMappingResponse:
    mapping = await SchemaMappingRepository(db).get_by_id(_parse_uuid(mapping_id, "mapping_id"))
    if not mapping or mapping.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema mapping not found")

    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field must be provided",
        )
    merged = {
        "entity_table": mapping.entity_table,
        "entity_id_col": mapping.entity_id_col,
        "entity_name_col": mapping.entity_name_col,
        "signal_columns": mapping.signal_columns,
        "timestamp_col": mapping.timestamp_col,
        "risk_config": mapping.risk_config,
        "raw_schema": mapping.raw_schema,
        "target_column": mapping.target_column,
        "rag_config": mapping.rag_config,
    }
    merged.update(payload)
    _validate_mapping_payload(merged)
    for key, value in payload.items():
        setattr(mapping, key, value)
    touch_updated_at(mapping)
    await db.flush()
    await db.commit()
    return _mapping_to_response(mapping)


@router.delete("/{mapping_id}", status_code=204)
async def delete_schema_mapping(
    mapping_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    mapping_uuid = _parse_uuid(mapping_id, "mapping_id")
    mapping = await SchemaMappingRepository(db).get_by_id(mapping_uuid)
    if not mapping or mapping.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema mapping not found")
    await SchemaMappingRepository(db).delete(mapping_uuid)
    await db.commit()
