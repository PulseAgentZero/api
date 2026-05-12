from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CreateConnectionRequest(BaseModel):
    db_type: str
    host: str
    port: int
    database_name: str
    username: str
    password: str


class UpdateConnectionRequest(BaseModel):
    db_type: str | None = None
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    username: str | None = None
    password: str | None = None


class ConnectionResponse(BaseModel):
    id: UUID
    org_id: UUID
    db_type: str | None
    host: str | None
    port: int | None
    database_name: str | None
    username: str | None
    status: str
    last_tested_at: datetime | None
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
