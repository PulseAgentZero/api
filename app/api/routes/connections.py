import logging
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, unquote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.connection import (
    ConnectionResponse,
    CreateConnectionRequest,
    IntrospectResponse,
    TestConnectionResponse,
    UpdateConnectionRequest,
)
from app.infrastructure.crypto import decrypt_dsn, encrypt_dsn
from app.infrastructure.database.connection_tester import introspect_schema, test_connection
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.connection_repository import (
    ConnectionRepository,
)
from app.infrastructure.database.repositories.recommendation_repository import (
    RecommendationRepository,
)
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
        db_type=conn.db_type,
        host=conn.host,
        port=conn.port,
        database_name=conn.database_name,
        username=conn.username,
        status=conn.status,
        last_tested_at=conn.last_tested_at,
        created_at=conn.created_at,
    )


async def _get_current_connection(db: AsyncSession, org_id) -> object:
    conns = await ConnectionRepository(db).list_by_org(org_id)
    if not conns:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    return conns[-1]


async def _test_and_mark_connection(conn) -> tuple[bool, str, str | None]:
    dsn = decrypt_dsn(conn.encrypted_dsn)
    success, message, db_version = await test_connection(dsn)
    conn.status = "active" if success else "failed"
    conn.last_tested_at = datetime.now(timezone.utc)
    return success, message, db_version


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ConnectionResponse]:
    conns = await ConnectionRepository(db).list_by_org(current_user.org_id)
    return [_connection_to_response(c) for c in conns]


@router.post("/test", response_model=TestConnectionResponse)
async def test_current_connection(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TestConnectionResponse:
    conn = await _get_current_connection(db, current_user.org_id)
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
    return await _update_connection_record(conn, body, db)


@router.delete("", status_code=204)
async def delete_current_connection(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conn = await _get_current_connection(db, current_user.org_id)
    await ConnectionRepository(db).delete(conn.id)
    await RecommendationRepository(db).delete_by_org(current_user.org_id)
    await db.commit()


@router.get("/{connection_id}", response_model=ConnectionResponse)
async def get_connection(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    conn = await ConnectionRepository(db).get_by_id(_parse_uuid(connection_id, "connection_id"))
    if not conn or conn.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    return _connection_to_response(conn)


@router.post("", response_model=ConnectionResponse, status_code=201)
async def create_connection(
    body: CreateConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    dsn = _make_dsn(
        body.db_type,
        body.host,
        body.port,
        body.database_name,
        body.username,
        body.password,
    )
    encrypted = encrypt_dsn(dsn)
    conn = await ConnectionRepository(db).create(
        org_id=current_user.org_id,
        db_type=body.db_type,
        host=body.host,
        port=body.port,
        database_name=body.database_name,
        username=body.username,
        encrypted_dsn=encrypted,
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
    conn.encrypted_dsn = encrypt_dsn(_make_dsn(password=password, **effective))

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
    await ConnectionRepository(db).delete(connection_uuid)
    await RecommendationRepository(db).delete_by_org(current_user.org_id)
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

    dsn = decrypt_dsn(conn.encrypted_dsn)
    tables = await introspect_schema(dsn)
    return IntrospectResponse(tables=tables)
