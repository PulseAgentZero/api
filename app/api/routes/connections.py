import logging
import os
import uuid as uuid_mod
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, unquote
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.dependencies.plan_gate import max_cloud_free_connections
from app.api.schemas.connection import (
    ConnectionResponse,
    CreateConnectionRequest,
    IntrospectResponse,
    TablePreviewResponse,
    TestConnectionResponse,
    UpdateConnectionRequest,
)
from app.infrastructure.crypto import decrypt_dsn, encrypt_dsn
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
router = APIRouter(prefix="/connections", tags=["connections"])


def _parse_uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid {field_name}") from exc


def _make_dsn(
    db_type: str,
    host: str,
    port: int,
    database_name: str,
    username: str,
    password: str,
) -> str:
    scheme = "mysql" if db_type == "mysql" else "postgresql"
    return (
        f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database_name, safe='')}"
    )


def _password_from_dsn(encrypted_dsn: str) -> str:
    parsed = urlsplit(decrypt_dsn(encrypted_dsn))
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
        status=conn.status,
        last_tested_at=conn.last_tested_at,
        last_test_error=conn.last_test_error,
        created_at=conn.created_at,
    )


def _connection_dsn(conn) -> str:
    if not conn.encrypted_dsn:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This connection has no database URL (e.g. file/CSV connectors).",
        )
    return decrypt_dsn(conn.encrypted_dsn)


def _assert_live(conn) -> None:
    if conn.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")


async def _get_current_connection(db: AsyncSession, org_id) -> object:
    conns = await ConnectionRepository(db).list_by_org(org_id)
    if not conns:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    return conns[-1]


async def _test_and_mark_connection(conn) -> tuple[bool, str, str | None]:
    try:
        dsn = _connection_dsn(conn)
    except HTTPException as exc:
        return False, str(exc.detail), None
    success, message, db_version = await test_connection(dsn)
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
                    raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large")
                out.write(chunk)
    except HTTPException:
        if os.path.isfile(dest_path):
            os.remove(dest_path)
        raise
    conn = await ConnectionRepository(db).create(
        org_id=current_user.org_id,
        encrypted_dsn=None,
        db_type=None,
        host=None,
        port=None,
        database_name=None,
        username=None,
        name=file.filename or "Uploaded file",
        connector_type="csv",
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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=TestConnectionResponse(success=False, message=message).model_dump(),
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    _assert_live(conn)
    dsn = _connection_dsn(conn)
    tables = await introspect_schema(dsn)
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    _assert_live(conn)
    dsn = _connection_dsn(conn)
    try:
        rows = await preview_table_rows(dsn, table_name, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return TablePreviewResponse(table=table_name, rows=rows, limit=limit)


@router.get("/{connection_id}", response_model=ConnectionResponse)
async def get_connection(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    conn = await ConnectionRepository(db).get_by_id(_parse_uuid(connection_id, "connection_id"))
    if not conn or conn.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
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
    dsn = _make_dsn(
        body.db_type,
        body.host,
        body.port,
        body.database_name,
        body.username,
        body.password,
    )
    encrypted = encrypt_dsn(dsn)
    # Normalise "postgresql" → "postgres" so _make_dsn and downstream checks are consistent
    _ALIASES = {"postgresql": "postgres"}
    ct = _ALIASES.get(body.connector_type or body.db_type or "", body.connector_type or body.db_type)
    conn = await repo.create(
        org_id=current_user.org_id,
        db_type=body.db_type,
        host=body.host,
        port=body.port,
        database_name=body.database_name,
        username=body.username,
        encrypted_dsn=encrypted,
        name=body.name,
        connector_type=ct,
    )
    success, message, _db_version = await _test_and_mark_connection(conn)
    await db.commit()
    if not success:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=TestConnectionResponse(success=False, message=message).model_dump(),
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    _assert_live(conn)

    return await _update_connection_record(conn, body, db)


async def _update_connection_record(
    conn,
    body: UpdateConnectionRequest,
    db: AsyncSession,
) -> ConnectionResponse:
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field must be provided",
        )
    if not conn.encrypted_dsn:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot update this connection type via this endpoint",
        )

    password = payload.pop("password", None) or _password_from_dsn(conn.encrypted_dsn)
    effective = {
        "db_type": payload.get("db_type", conn.db_type),
        "host": payload.get("host", conn.host),
        "port": payload.get("port", conn.port),
        "database_name": payload.get("database_name", conn.database_name),
        "username": payload.get("username", conn.username),
    }

    for key, value in payload.items():
        setattr(conn, key, value)
    conn.encrypted_dsn = encrypt_dsn(_make_dsn(
        effective["db_type"],
        effective["host"],
        effective["port"],
        effective["database_name"],
        effective["username"],
        password,
    ))

    success, message, _db_version = await _test_and_mark_connection(conn)
    await db.flush()
    await db.commit()
    if not success:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=TestConnectionResponse(success=False, message=message).model_dump(),
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    _assert_live(conn)

    success, message, db_version = await _test_and_mark_connection(conn)
    await db.flush()
    await db.commit()
    if not success:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=TestConnectionResponse(success=False, message=message).model_dump(),
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    _assert_live(conn)

    dsn = _connection_dsn(conn)
    tables = await introspect_schema(dsn)
    return IntrospectResponse(tables=tables)
