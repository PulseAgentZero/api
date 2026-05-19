import logging
import os
import uuid as uuid_mod
from datetime import datetime, timezone
from urllib.parse import urlsplit, unquote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.dependencies.plan_gate import max_cloud_free_connections
from app.api.errors import (
    PulseHTTPException,
    bad_request,
    not_found,
    payload_too_large,
    validation_error,
)
from app.api.schemas.connection import (
    ConnectionResponse,
    CreateConnectionRequest,
    IntrospectResponse,
    TablePreviewResponse,
    TestConnectionResponse,
    UpdateConnectionRequest,
)
from app.infrastructure.audit import log_audit
from app.infrastructure.crypto import decrypt_dsn, encrypt_dsn
from app.infrastructure.database.base import touch_updated_at
from app.infrastructure.connectors.factory import _make_sql_dsn, build_encrypted_secret_and_row_fields
from app.infrastructure.connectors.payload import parse_pulse_api_payload
from app.infrastructure.connectors.connection_test import test_connection_record
from app.infrastructure.database.connection_tester import (
    introspect_schema,
    preview_table_rows,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.connection_repository import ConnectionRepository
from app.infrastructure.database.repositories.schema_mapping_repository import SchemaMappingRepository
from app.infrastructure.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/connections", tags=["Connections"])

_CONNECTOR_CATALOG = [
    {
        "connector_type": "postgresql",
        "display_name": "PostgreSQL",
        "category": "SQL Database",
        "icon_slug": "postgresql",
        "description": "Connect to a PostgreSQL database.",
        "fields": [
            {"key": "host", "label": "Host", "type": "string", "required": True},
            {"key": "port", "label": "Port", "type": "integer", "required": True, "default": 5432},
            {"key": "database_name", "label": "Database Name", "type": "string", "required": True},
            {"key": "username", "label": "Username", "type": "string", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True},
            {"key": "sslmode", "label": "SSL Mode", "type": "select", "required": False, "default": "prefer",
             "options": ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]},
        ],
    },
    {
        "connector_type": "mysql",
        "display_name": "MySQL",
        "category": "SQL Database",
        "icon_slug": "mysql",
        "description": "Connect to a MySQL or MariaDB database.",
        "fields": [
            {"key": "host", "label": "Host", "type": "string", "required": True},
            {"key": "port", "label": "Port", "type": "integer", "required": True, "default": 3306},
            {"key": "database_name", "label": "Database Name", "type": "string", "required": True},
            {"key": "username", "label": "Username", "type": "string", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True},
        ],
    },
    {
        "connector_type": "mssql",
        "display_name": "Microsoft SQL Server",
        "category": "SQL Database",
        "icon_slug": "mssql",
        "description": "Connect to a Microsoft SQL Server database.",
        "fields": [
            {"key": "host", "label": "Host", "type": "string", "required": True},
            {"key": "port", "label": "Port", "type": "integer", "required": True, "default": 1433},
            {"key": "database_name", "label": "Database Name", "type": "string", "required": True},
            {"key": "username", "label": "Username", "type": "string", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True},
        ],
    },
    {
        "connector_type": "redshift",
        "display_name": "Amazon Redshift",
        "category": "SQL Database",
        "icon_slug": "redshift",
        "description": "Connect to an Amazon Redshift cluster.",
        "fields": [
            {"key": "host", "label": "Host", "type": "string", "required": True},
            {"key": "port", "label": "Port", "type": "integer", "required": True, "default": 5439},
            {"key": "database_name", "label": "Database Name", "type": "string", "required": True},
            {"key": "username", "label": "Username", "type": "string", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True},
        ],
    },
    {
        "connector_type": "sqlite",
        "display_name": "SQLite",
        "category": "SQL Database",
        "icon_slug": "sqlite",
        "description": "Connect to a local SQLite database file.",
        "fields": [
            {"key": "database_name", "label": "Database File Path", "type": "string", "required": True,
             "placeholder": "/path/to/database.db"},
        ],
    },
    {
        "connector_type": "snowflake",
        "display_name": "Snowflake",
        "category": "Cloud Warehouse",
        "icon_slug": "snowflake",
        "description": "Connect to a Snowflake data warehouse.",
        "fields": [
            {"key": "connection_url", "label": "Connection URL", "type": "string", "required": True,
             "placeholder": "snowflake://user:pass@account/db?warehouse=WH&role=ROLE"},
        ],
    },
    {
        "connector_type": "bigquery",
        "display_name": "Google BigQuery",
        "category": "Cloud Warehouse",
        "icon_slug": "bigquery",
        "description": "Connect to Google BigQuery with a project URL and optional service account JSON.",
        "fields": [
            {"key": "connection_url", "label": "Connection URL", "type": "string", "required": True,
             "placeholder": "bigquery://my-project/my_dataset",
             "help": "Format: bigquery://project_id/dataset_id"},
            {"key": "bigquery_service_account_json", "label": "Service Account JSON",
             "type": "textarea", "required": False,
             "placeholder": '{"type": "service_account", "project_id": "...", ...}',
             "help": "Paste the full JSON key from GCP (IAM → Service Accounts → Keys). "
                      "Required unless Application Default Credentials are configured on the Pulse host."},
        ],
    },
    {
        "connector_type": "databricks",
        "display_name": "Databricks",
        "category": "Cloud Warehouse",
        "icon_slug": "databricks",
        "description": "Connect to a Databricks SQL warehouse.",
        "fields": [
            {"key": "connection_url", "label": "Connection URL", "type": "string", "required": True,
             "placeholder": "databricks+connector://token@host/database"},
        ],
    },
    {
        "connector_type": "clickhouse",
        "display_name": "ClickHouse",
        "category": "Analytical Database",
        "icon_slug": "clickhouse",
        "description": "Connect to a ClickHouse analytical database via native DSN or HTTP.",
        "fields": [
            {"key": "connection_url", "label": "Native DSN", "type": "string", "required": False,
             "placeholder": "clickhouse+native://user:pass@host:9000/db"},
            {"key": "clickhouse_https_url", "label": "HTTPS URL", "type": "string", "required": False,
             "placeholder": "https://host:8443"},
            {"key": "clickhouse_user", "label": "Username", "type": "string", "required": False},
            {"key": "clickhouse_password", "label": "Password", "type": "password", "required": False},
        ],
        "notes": "Provide either a native DSN or the HTTPS URL with credentials.",
    },
    {
        "connector_type": "mongodb",
        "display_name": "MongoDB",
        "category": "NoSQL Database",
        "icon_slug": "mongodb",
        "description": "Connect to a MongoDB cluster.",
        "fields": [
            {"key": "mongodb_uri", "label": "Connection URI", "type": "string", "required": True,
             "placeholder": "mongodb+srv://user:pass@cluster.mongodb.net/db"},
        ],
    },
    {
        "connector_type": "airtable",
        "display_name": "Airtable",
        "category": "SaaS / Spreadsheet",
        "icon_slug": "airtable",
        "description": "Connect to an Airtable base using a Personal Access Token.",
        "fields": [
            {"key": "airtable_pat", "label": "Personal Access Token", "type": "password", "required": True},
            {"key": "airtable_base_id", "label": "Base ID", "type": "string", "required": False,
             "placeholder": "appXXXXXXXXXXXXXX"},
        ],
    },
    {
        "connector_type": "google_sheets",
        "display_name": "Google Sheets",
        "category": "SaaS / Spreadsheet",
        "icon_slug": "google_sheets",
        "description": "Connect to a Google Spreadsheet with an API key or a service account.",
        "fields": [
            {"key": "google_auth_method", "label": "Authentication", "type": "select",
             "required": True, "default": "api_key",
             "options": ["api_key", "service_account"]},
            {"key": "google_spreadsheet_id", "label": "Spreadsheet ID", "type": "string", "required": True,
             "placeholder": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
             "help": "From the sheet URL: docs.google.com/spreadsheets/d/{id}/edit"},
            {"key": "google_sheets_api_key", "label": "Google API Key", "type": "password",
             "required": True, "when": {"google_auth_method": "api_key"},
             "help": "GCP Console → APIs & Services → Credentials → API key (Sheets API enabled)"},
            {"key": "google_service_account_json", "label": "Service Account JSON",
             "type": "textarea", "required": True,
             "when": {"google_auth_method": "service_account"},
             "placeholder": '{"type": "service_account", "client_email": "...", ...}',
             "help": "Share the spreadsheet with the service account email (Editor or Viewer)."},
        ],
    },
    {
        "connector_type": "s3",
        "display_name": "Amazon S3",
        "category": "Object Storage",
        "icon_slug": "s3",
        "description": "Connect to an Amazon S3 bucket containing CSV or Parquet files.",
        "fields": [
            {"key": "s3_bucket", "label": "Bucket Name", "type": "string", "required": True},
            {"key": "s3_access_key_id", "label": "Access Key ID", "type": "string", "required": True},
            {"key": "s3_secret_access_key", "label": "Secret Access Key", "type": "password", "required": True},
            {"key": "s3_region", "label": "Region", "type": "string", "required": False, "default": "us-east-1"},
            {"key": "s3_prefix", "label": "Object prefix (optional)", "type": "string", "required": False,
             "placeholder": "data/exports/", "help": "Only list CSV files under this prefix."},
        ],
    },
    {
        "connector_type": "gcs",
        "display_name": "Google Cloud Storage",
        "category": "Object Storage",
        "icon_slug": "gcs",
        "description": "Connect to a GCS bucket with a service account JSON key.",
        "fields": [
            {"key": "gcs_bucket", "label": "Bucket Name", "type": "string", "required": True,
             "placeholder": "my-data-bucket"},
            {"key": "gcs_service_account_json", "label": "Service Account JSON",
             "type": "textarea", "required": True,
             "placeholder": '{"type": "service_account", "project_id": "...", ...}',
             "help": "GCP → IAM → Service Accounts → Keys → Add key → JSON. "
                      "Grant Storage Object Viewer on the bucket."},
        ],
    },
    {
        "connector_type": "csv",
        "display_name": "CSV / File Upload",
        "category": "File",
        "icon_slug": "csv",
        "description": "Upload a CSV file directly. Max 50 MB. Use POST /connections/upload.",
        "fields": [],
        "upload_endpoint": "/api/v1/connections/upload",
    },
]


@router.get("/catalog")
async def get_connector_catalog() -> list[dict]:
    """Return the full list of supported connector types with display metadata and required fields."""
    return _CONNECTOR_CATALOG


def _parse_uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise bad_request("BAD_REQUEST", f"Invalid {field_name}") from exc


def _password_from_dsn(encrypted_dsn: str) -> str:
    raw = decrypt_dsn(encrypted_dsn)
    if parse_pulse_api_payload(raw) is not None:
        return ""
    parsed = urlsplit(raw)
    return unquote(parsed.password or "")


def _connection_to_response(conn) -> ConnectionResponse:
    return ConnectionResponse(
        id=conn.id,
        org_id=conn.org_id,
        name=conn.name,
        connector_type=conn.connector_type,
        db_type=conn.db_type,
        host=conn.host,
        port=conn.port,
        database_name=conn.database_name,
        username=conn.username,
        sslmode=conn.sslmode,
        status=conn.status,
        last_tested_at=conn.last_tested_at,
        last_test_error=conn.last_test_error,
        config=getattr(conn, "config", None) or {},
        metadata_=getattr(conn, "metadata_", None) or {},
        connection_meta=getattr(conn, "connection_meta", None) or {},
        created_at=conn.created_at,
    )


def _connection_dsn(conn) -> str:
    if not conn.encrypted_dsn:
        raise bad_request(
            "BAD_REQUEST",
            "This connection has no database URL (e.g. file/CSV connectors).",
        )
    return decrypt_dsn(conn.encrypted_dsn)


def _assert_live(conn) -> None:
    if conn.deleted_at is not None:
        raise not_found("Connection not found")


async def _get_current_connection(db: AsyncSession, org_id) -> object:
    conns = await ConnectionRepository(db).list_by_org(org_id)
    if not conns:
        raise not_found("Connection not found")
    return conns[-1]


async def _test_and_mark_connection(conn) -> tuple[bool, str, str | None]:
    success, message, db_version = await test_connection_record(
        conn,
        decrypt_dsn=decrypt_dsn,
    )
    conn.status = "active" if success else "failed"
    conn.last_tested_at = datetime.now(timezone.utc)
    conn.last_test_error = None if success else message
    touch_updated_at(conn)
    return success, message, db_version


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ConnectionResponse]:
    """List all data source connections for the org. Soft-deleted connections are excluded."""
    conns = await ConnectionRepository(db).list_by_org(current_user.org_id)
    return [_connection_to_response(c) for c in conns]


@router.post("/upload", response_model=ConnectionResponse, status_code=201)
async def upload_connection_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    """Upload a CSV or spreadsheet file as a data source. Max file size: 50 MB.

    Send as `multipart/form-data` with the file in the `file` field. The connection
    is created with `status: "pending"` — further pipeline configuration is required
    before the AI pipeline can use it.
    """
    await max_cloud_free_connections(db, current_user.org_id, await ConnectionRepository(db).count_active(current_user.org_id))
    max_bytes = 50 * 1024 * 1024
    upload_dir = f"/tmp/pulse_uploads/{current_user.org_id}"
    os.makedirs(upload_dir, exist_ok=True)
    dest_name = f"{uuid_mod.uuid4()}_{file.filename or 'upload'}"
    dest_path = os.path.join(upload_dir, dest_name)
    size = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise payload_too_large("File too large")
                out.write(chunk)
    except PulseHTTPException:
        if os.path.isfile(dest_path):
            os.remove(dest_path)
        raise
    conn = await ConnectionRepository(db).create(
        org_id=current_user.org_id,
        name=file.filename or "Uploaded file",
        connector_type="csv",
        connection_meta={"kind": "csv"},
    )
    conn.config = {"upload_path": dest_path, "original_filename": file.filename}
    touch_updated_at(conn)
    success, message, _db_version = await _test_and_mark_connection(conn)
    await db.commit()
    await db.refresh(conn)
    if not success:
        raise PulseHTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="TEST_CONNECTION_FAILED",
            message=message,
            fields={"success": "false", "connection_id": str(conn.id)},
        )
    return _connection_to_response(conn)


@router.post("/test", response_model=TestConnectionResponse)
async def test_current_connection(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TestConnectionResponse:
    """Test the org's most recent connection. Returns 422 if the test fails.

    Prefer `POST /connections/{id}/test` when you have a specific connection ID.
    """
    conn = await _get_current_connection(db, current_user.org_id)
    _assert_live(conn)
    success, message, db_version = await _test_and_mark_connection(conn)
    await db.flush()
    await db.commit()
    if not success:
        raise PulseHTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="TEST_CONNECTION_FAILED",
            message=message,
            fields={"success": "false"},
        )
    return TestConnectionResponse(success=True, message=message, db_version=db_version)


@router.put("", response_model=ConnectionResponse)
async def update_current_connection(
    body: UpdateConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    conn = await _get_current_connection(db, current_user.org_id)
    _assert_live(conn)
    return await _update_connection_record(conn, body, db)


@router.delete("", status_code=204)
async def delete_current_connection(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conn = await _get_current_connection(db, current_user.org_id)
    _assert_live(conn)
    await ConnectionRepository(db).soft_delete(conn.id)
    await SchemaMappingRepository(db).deactivate_for_connection(conn.id)
    await db.commit()


@router.get("/{connection_id}/tables", response_model=IntrospectResponse)
async def list_connection_tables(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IntrospectResponse:
    """Introspect all tables and columns in a connection. Makes a live query against the remote DB.

    Use this to populate the schema-mapping step in onboarding or settings. For large databases
    this can take several seconds. Prefer the cached schema from `GET /onboarding/connection/schema`
    during the onboarding flow.
    """
    conn = await ConnectionRepository(db).get_by_id(_parse_uuid(connection_id, "connection_id"))
    if not conn or conn.org_id != current_user.org_id:
        raise not_found("Connection not found")
    _assert_live(conn)
    dsn = _connection_dsn(conn)
    tables = await introspect_schema(dsn, sslmode=conn.sslmode)
    return IntrospectResponse(tables=tables)


@router.get("/{connection_id}/tables/{table_name}/preview", response_model=TablePreviewResponse)
async def preview_connection_table(
    connection_id: str,
    table_name: str,
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TablePreviewResponse:
    """Preview up to 500 rows from a specific table. Useful for verifying column mappings.

    Results are returned as `rows: [{ column: value, ... }]`. No sensitive values are
    masked — use this only in trusted admin contexts.
    """
    conn = await ConnectionRepository(db).get_by_id(_parse_uuid(connection_id, "connection_id"))
    if not conn or conn.org_id != current_user.org_id:
        raise not_found("Connection not found")
    _assert_live(conn)
    dsn = _connection_dsn(conn)
    try:
        rows = await preview_table_rows(dsn, table_name, limit=limit, sslmode=conn.sslmode)
    except ValueError as exc:
        raise bad_request("BAD_REQUEST", str(exc)) from exc
    return TablePreviewResponse(table=table_name, rows=rows, limit=limit)


@router.get("/{connection_id}", response_model=ConnectionResponse)
async def get_connection(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    """Return a single connection by ID."""
    conn = await ConnectionRepository(db).get_by_id(_parse_uuid(connection_id, "connection_id"))
    if not conn or conn.org_id != current_user.org_id:
        raise not_found("Connection not found")
    _assert_live(conn)
    return _connection_to_response(conn)


@router.post("", response_model=ConnectionResponse, status_code=201)
async def create_connection(
    body: CreateConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    """Create and immediately test a new data source connection.

    Set `connector_type` to one of the values returned by `GET /connections/catalog`.
    Each connector type requires different fields — see the catalog for the exact field list.
    Returns 422 if the connection test fails; the `fields.connection_id` in the error
    contains the ID of the failed record so the frontend can offer a retry without creating
    a duplicate.
    """
    repo = ConnectionRepository(db)
    await max_cloud_free_connections(db, current_user.org_id, await repo.count_active(current_user.org_id))
    built = build_encrypted_secret_and_row_fields(body)
    plaintext = built.pop("plaintext_secret")
    conn = await repo.create(
        org_id=current_user.org_id,
        encrypted_dsn=encrypt_dsn(plaintext),
        name=body.name or "My Connection",
        connector_type=built.get("connector_type"),
        connection_meta=built.get("connection_meta") or {},
    )
    success, message, _db_version = await _test_and_mark_connection(conn)
    if success:
        await log_audit(
            db,
            org_id=current_user.org_id,
            user_id=current_user.id,
            action="connection.created",
            resource="connection",
            resource_id=conn.id,
            metadata={"name": conn.name, "connector_type": conn.connector_type},
        )
    await db.commit()
    if not success:
        raise PulseHTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="TEST_CONNECTION_FAILED",
            message=message,
            fields={"success": "false", "connection_id": str(conn.id)},
        )
    return _connection_to_response(conn)


@router.patch("/{connection_id}", response_model=ConnectionResponse)
async def update_connection(
    connection_id: str,
    body: UpdateConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    conn = await ConnectionRepository(db).get_by_id(_parse_uuid(connection_id, "connection_id"))
    if not conn or conn.org_id != current_user.org_id:
        raise not_found("Connection not found")
    _assert_live(conn)

    return await _update_connection_record(conn, body, db)


async def _update_connection_record(
    conn,
    body: UpdateConnectionRequest,
    db: AsyncSession,
) -> ConnectionResponse:
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise validation_error(
            "At least one field must be provided",
            fields={"body": "Provide at least one field to update"},
        )
    if not conn.encrypted_dsn:
        raise bad_request(
            "BAD_REQUEST",
            "Cannot update this connection type via this endpoint",
        )

    decrypted = decrypt_dsn(conn.encrypted_dsn)
    api_blob = parse_pulse_api_payload(decrypted)

    if api_blob is not None:
        disallowed = {
            k for k in ("host", "port", "database_name", "username", "db_type", "password", "connection_url")
            if k in payload
        }
        if disallowed:
            raise bad_request(
                "BAD_REQUEST",
                "API and object-store connectors cannot change credentials via PATCH; "
                "delete the connection and create a new one.",
            )
        if "name" in payload:
            conn.name = payload["name"]
        if "sslmode" in payload:
            conn.sslmode = payload["sslmode"]
        success, message, _db_version = await _test_and_mark_connection(conn)
        await db.flush()
        await db.commit()
        if not success:
            raise PulseHTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="TEST_CONNECTION_FAILED",
                message=message,
                fields={"success": "false"},
            )
        return _connection_to_response(conn)

    password = payload.pop("password", None) or _password_from_dsn(conn.encrypted_dsn)

    # Update simple top-level fields and meta fields via the model's property setters.
    for key, value in payload.items():
        if key == "connection_url":
            continue
        if hasattr(conn, key):
            setattr(conn, key, value)

    ct = (conn.connector_type or "postgresql").lower()
    dt = (conn.db_type or "postgresql").lower()
    if dt == "postgres":
        dt = "postgresql"

    if ct in ("snowflake", "bigquery", "databricks") or dt in ("snowflake", "bigquery", "databricks"):
        url = (body.connection_url or decrypted).strip()
        if not url:
            raise bad_request("BAD_REQUEST", "connection_url is required to update this connector")
        conn.encrypted_dsn = encrypt_dsn(url)
    elif ct == "sqlite" or dt == "sqlite":
        path = (conn.database_name or "").strip()
        if not path:
            raise bad_request("BAD_REQUEST", "SQLite connection is missing database file path")
        conn.encrypted_dsn = encrypt_dsn(f"sqlite:///{path}")
    else:
        if conn.host is None or conn.port is None:
            raise bad_request("BAD_REQUEST", "host and port are required for this SQL connection")
        if not conn.database_name or not conn.username:
            raise bad_request("BAD_REQUEST", "database_name and username are required")
        db_for_dsn = dt
        if db_for_dsn == "mssql" or ct == "mssql":
            db_for_dsn = "mssql"
        elif db_for_dsn == "mysql" or ct == "mysql":
            db_for_dsn = "mysql"
        elif db_for_dsn == "redshift" or ct == "redshift":
            db_for_dsn = "redshift"
        else:
            db_for_dsn = "postgresql"
        conn.encrypted_dsn = encrypt_dsn(
            _make_sql_dsn(
                db_for_dsn, conn.host, int(conn.port),
                conn.database_name, conn.username, password,
                sslmode=conn.sslmode,
            )
        )

    success, message, _db_version = await _test_and_mark_connection(conn)
    await db.flush()
    await db.commit()
    if not success:
        raise PulseHTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="TEST_CONNECTION_FAILED",
            message=message,
            fields={"success": "false"},
        )
    return _connection_to_response(conn)


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    connection_uuid = _parse_uuid(connection_id, "connection_id")
    conn = await ConnectionRepository(db).get_by_id(connection_uuid)
    if not conn or conn.org_id != current_user.org_id:
        raise not_found("Connection not found")
    _assert_live(conn)
    await ConnectionRepository(db).soft_delete(connection_uuid)
    await SchemaMappingRepository(db).deactivate_for_connection(connection_uuid)
    await log_audit(
        db,
        org_id=current_user.org_id,
        user_id=current_user.id,
        action="connection.deleted",
        resource="connection",
        resource_id=connection_uuid,
        metadata={"name": conn.name},
    )
    await db.commit()


@router.post("/{connection_id}/test", response_model=TestConnectionResponse)
async def test_db_connection(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TestConnectionResponse:
    conn = await ConnectionRepository(db).get_by_id(_parse_uuid(connection_id, "connection_id"))
    if not conn or conn.org_id != current_user.org_id:
        raise not_found("Connection not found")
    _assert_live(conn)

    success, message, db_version = await _test_and_mark_connection(conn)
    await db.flush()
    await db.commit()
    if not success:
        raise PulseHTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="TEST_CONNECTION_FAILED",
            message=message,
            fields={"success": "false"},
        )

    return TestConnectionResponse(success=True, message=message, db_version=db_version)


@router.post("/{connection_id}/introspect", response_model=IntrospectResponse)
async def introspect_db_schema(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IntrospectResponse:
    conn = await ConnectionRepository(db).get_by_id(_parse_uuid(connection_id, "connection_id"))
    if not conn or conn.org_id != current_user.org_id:
        raise not_found("Connection not found")
    _assert_live(conn)

    dsn = _connection_dsn(conn)
    tables = await introspect_schema(dsn, sslmode=conn.sslmode)
    return IntrospectResponse(tables=tables)
