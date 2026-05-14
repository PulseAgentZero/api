from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CONNECTOR_TYPES = frozenset(
    {
        "postgresql",
        "mysql",
        "mssql",
        "sqlite",
        "redshift",
        "snowflake",
        "bigquery",
        "databricks",
        "clickhouse",
        "google_sheets",
        "airtable",
        "mongodb",
        "s3",
        "gcs",
        "csv",
    }
)


class CreateConnectionRequest(BaseModel):
    name: str | None = Field(None, max_length=255)
    connector_type: str = Field(
        "postgresql",
        description="See CONNECTOR_REGISTRY in app.infrastructure.connectors.registry",
    )
    db_type: Literal["postgresql", "mysql", "mssql", "sqlite", "redshift"] | None = None
    host: str = Field("", max_length=255)
    port: int | None = Field(None, ge=1, le=65535)
    database_name: str = Field("", max_length=2048)
    username: str = Field("", max_length=255)
    password: str = Field("", max_length=4096)
    sslmode: str = Field("prefer", min_length=1, max_length=20)
    connection_url: str | None = Field(
        None,
        description="Full SQLAlchemy URL for Snowflake, BigQuery, Databricks, or native ClickHouse DSN.",
    )
    # Airtable
    airtable_pat: str | None = None
    airtable_base_id: str | None = None
    # Google Sheets (API key flow)
    google_sheets_api_key: str | None = None
    google_spreadsheet_id: str | None = None
    # MongoDB
    mongodb_uri: str | None = None
    # ClickHouse HTTP alternative
    clickhouse_https_url: str | None = None
    clickhouse_user: str = ""
    clickhouse_password: str = ""
    # S3
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_region: str | None = Field("us-east-1", max_length=64)
    # GCS
    gcs_bucket: str | None = None
    gcs_service_account_json: str | None = None

    @field_validator("connector_type")
    @classmethod
    def _connector(cls, v: str) -> str:
        key = (v or "postgresql").strip().lower()
        if key not in _CONNECTOR_TYPES:
            raise ValueError(f"Unsupported connector_type: {v}")
        return key

    @model_validator(mode="after")
    def _require_fields_by_connector(self) -> CreateConnectionRequest:
        ct = self.connector_type
        if ct in ("postgresql", "mysql", "mssql", "redshift"):
            if not self.host or self.port is None:
                raise ValueError("host and port are required for SQL connectors")
            if not self.database_name or not self.username:
                raise ValueError("database_name and username are required for SQL connectors")
            if ct in ("postgresql", "mysql", "mssql") and not self.password:
                raise ValueError("password is required for this SQL connector")
        if ct == "sqlite" and not self.database_name.strip():
            raise ValueError("For SQLite, database_name must be the database file path")
        if ct in ("snowflake", "bigquery", "databricks") and not (self.connection_url or "").strip():
            raise ValueError("connection_url is required for this connector")
        if ct == "clickhouse" and not (
            (self.connection_url or "").strip().lower().startswith("clickhouse")
            or (self.clickhouse_https_url or "").strip()
        ):
            raise ValueError("ClickHouse requires connection_url (native DSN) or clickhouse_https_url")
        if ct == "airtable" and not (self.airtable_pat or "").strip():
            raise ValueError("airtable_pat is required")
        if ct == "google_sheets" and (
            not (self.google_sheets_api_key or "").strip()
            or not (self.google_spreadsheet_id or "").strip()
        ):
            raise ValueError("google_sheets_api_key and google_spreadsheet_id are required")
        if ct == "mongodb" and not (self.mongodb_uri or "").strip():
            raise ValueError("mongodb_uri is required")
        if ct == "s3" and (
            not (self.s3_bucket or "").strip()
            or not (self.s3_access_key_id or "").strip()
            or not (self.s3_secret_access_key or "").strip()
        ):
            raise ValueError("s3_bucket, s3_access_key_id, and s3_secret_access_key are required")
        if ct == "gcs" and (
            not (self.gcs_bucket or "").strip() or not (self.gcs_service_account_json or "").strip()
        ):
            raise ValueError("gcs_bucket and gcs_service_account_json are required")
        return self


class UpdateConnectionRequest(BaseModel):
    name: str | None = None
    db_type: Literal["postgresql", "mysql", "mssql", "sqlite", "redshift"] | None = None
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    username: str | None = None
    password: str | None = None
    sslmode: str | None = None
    connection_url: str | None = None


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
    sslmode: str | None
    status: str
    last_tested_at: datetime | None
    last_test_error: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict, alias="metadata_")
    connection_meta: dict[str, Any] = Field(default_factory=dict)
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
