from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateConnectionRequest(BaseModel):
    name: str | None = Field(None, max_length=255)
    connector_type: Literal["postgresql", "mysql", "csv", "snowflake", "bigquery"] | None = None
    db_type: Literal["postgresql", "mysql"]
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    database_name: str = Field(..., min_length=1, max_length=255)
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1)


class UpdateConnectionRequest(BaseModel):
    name: str | None = None
    db_type: Literal["postgresql", "mysql"] | None = None
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    username: str | None = None
    password: str | None = None


class ConnectionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    org_id: UUID
    name: str
    connector_type: str
    db_type: str | None
    host: str | None
    port: int | None
    database_name: str | None
    username: str | None
    status: str
    last_tested_at: datetime | None
    last_test_error: str | None = None
    config: dict[str, Any] = {}
    metadata: dict[str, Any] = Field(default_factory=dict, alias="metadata_")
    created_at: datetime


class TestConnectionResponse(BaseModel):
    success: bool
    message: str
    db_version: str | None = None


class ColumnInfo(BaseModel):
    name: str
    data_type: str
    nullable: bool


class TableInfo(BaseModel):
    name: str
    columns: list[ColumnInfo]


class IntrospectResponse(BaseModel):
    tables: list[TableInfo]


class TablePreviewResponse(BaseModel):
    table: str
    rows: list[dict[str, object]]
    limit: int
