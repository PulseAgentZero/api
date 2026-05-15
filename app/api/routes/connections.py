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
from app.infrastructure.crypto import decrypt_dsn, encrypt_dsn
from app.infrastructure.connectors.factory import _make_sql_dsn, build_encrypted_secret_and_row_fields
from app.infrastructure.connectors.payload import parse_pulse_api_payload
from app.infrastructure.database.connection_tester import (
    introspect_schema,
    preview_table_rows,
    test_connection,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.connection_repository import ConnectionRepository
from app.infrastructure.database.repositories.schema_mapping_repository import SchemaMappingRepository
from app.infrastructure.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/connections", tags=["Connections"])


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
    try:
        dsn = _connection_dsn(conn)
    except PulseHTTPException as exc:
        detail = exc.detail
        msg = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
        return False, msg, None
    success, message, db_version = await test_connection(dsn, sslmode=conn.sslmode)
    conn.status = "active" if success else "failed"
    conn.last_tested_at = datetime.now(timezone.utc)
    conn.last_test_error = None if success else message
    return success, message, db_version


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ConnectionResponse]:
    conns = await ConnectionRepository(db).list_by_org(current_user.org_id)
    return [_connection_to_response(c) for c in conns]


@router.post("/upload", response_model=ConnectionResponse, status_code=201)
async def upload_connection_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
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
    conn.status = "pending"
    await db.commit()
    await db.refresh(conn)
    return _connection_to_response(conn)


@router.post("/test", response_model=TestConnectionResponse)
async def test_current_connection(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TestConnectionResponse:
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
