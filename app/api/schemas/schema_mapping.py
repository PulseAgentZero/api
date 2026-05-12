from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CreateSchemaMappingRequest(BaseModel):
    connection_id: UUID
    entity_table: str
    entity_id_col: str
    entity_name_col: str | None = None
    signal_columns: dict | None = None
    timestamp_col: str | None = None
    risk_config: dict | None = None
    raw_schema: dict | None = None


class UpdateSchemaMappingRequest(BaseModel):
    entity_table: str | None = None
    entity_id_col: str | None = None
    entity_name_col: str | None = None
    signal_columns: dict | None = None
    timestamp_col: str | None = None
    risk_config: dict | None = None
    raw_schema: dict | None = None


class SchemaMappingResponse(BaseModel):
    id: UUID
    org_id: UUID
    connection_id: UUID
    entity_table: str | None
    entity_id_col: str | None
    entity_name_col: str | None
    signal_columns: dict | None
    timestamp_col: str | None
    risk_config: dict | None
    raw_schema: dict | None
    created_at: datetime
