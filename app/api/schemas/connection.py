from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class CreateConnectionRequest(BaseModel):
    db_type: Literal["postgresql", "mysql"]
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    database_name: str = Field(..., min_length=1, max_length=255)
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1)
    sslmode: str = Field("prefer", min_length=1, max_length=20)


class UpdateConnectionRequest(BaseModel):
    db_type: Literal["postgresql", "mysql"] | None = None
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    username: str | None = None
    password: str | None = None
    sslmode: str | None = None


class ConnectionResponse(BaseModel):
    id: UUID
    org_id: UUID
    db_type: str | None
    host: str | None
    port: int | None
    database_name: str | None
    username: str | None
    sslmode: str | None
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
