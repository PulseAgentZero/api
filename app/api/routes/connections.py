import logging
from datetime import datetime, timezone

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
from app.infrastructure.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/connections", tags=["connections"])


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


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ConnectionResponse]:
    conns = await ConnectionRepository(db).list_by_org(current_user.org_id)
    return [_connection_to_response(c) for c in conns]


@router.get("/{connection_id}", response_model=ConnectionResponse)
async def get_connection(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    from uuid import UUID
    conn = await ConnectionRepository(db).get_by_id(UUID(connection_id))
    if not conn or conn.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    return _connection_to_response(conn)


@router.post("", response_model=ConnectionResponse, status_code=201)
async def create_connection(
    body: CreateConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    dsn = f"postgresql://{body.username}:{body.password}@{body.host}:{body.port}/{body.database_name}"
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
    await db.commit()
    return _connection_to_response(conn)


@router.patch("/{connection_id}", response_model=ConnectionResponse)
async def update_connection(
    connection_id: str,
    body: UpdateConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    from uuid import UUID
    conn = await ConnectionRepository(db).get_by_id(UUID(connection_id))
    if not conn or conn.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")

    payload = body.model_dump(exclude_none=True)
    if "password" in payload:
        password = payload.pop("password")
        dsn = f"postgresql://{conn.username}:{password}@{conn.host}:{conn.port}/{conn.database_name}"
        payload["encrypted_dsn"] = encrypt_dsn(dsn)

    for key, value in payload.items():
        setattr(conn, key, value)
    await db.flush()
    await db.commit()
    return _connection_to_response(conn)


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from uuid import UUID
    conn = await ConnectionRepository(db).get_by_id(UUID(connection_id))
    if not conn or conn.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    await ConnectionRepository(db).delete(UUID(connection_id))
    await db.commit()


@router.post("/{connection_id}/test", response_model=TestConnectionResponse)
async def test_db_connection(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TestConnectionResponse:
    from uuid import UUID
    conn = await ConnectionRepository(db).get_by_id(UUID(connection_id))
    if not conn or conn.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")

    dsn = decrypt_dsn(conn.encrypted_dsn)
    success, message, db_version = await test_connection(dsn)

    if not success:
        conn.status = "failed"
        conn.last_tested_at = datetime.now(timezone.utc)
        await db.flush()
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=TestConnectionResponse(success=False, message=message).model_dump(),
        )

    conn.status = "active"
    conn.last_tested_at = datetime.now(timezone.utc)
    await db.flush()
    await db.commit()

    return TestConnectionResponse(success=True, message=message, db_version=db_version)


@router.post("/{connection_id}/introspect", response_model=IntrospectResponse)
async def introspect_db_schema(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IntrospectResponse:
    from uuid import UUID
    conn = await ConnectionRepository(db).get_by_id(UUID(connection_id))
    if not conn or conn.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")

    dsn = decrypt_dsn(conn.encrypted_dsn)
    tables = await introspect_schema(dsn)
    return IntrospectResponse(tables=tables)
