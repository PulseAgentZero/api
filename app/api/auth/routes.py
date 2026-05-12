import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.jwt_utils import create_access_token, create_refresh_token, decode_access_token
from app.api.auth.passwords import hash_password, verify_password
from app.api.schemas.auth import (
    LoginRequest,
    MeResponse,
    OrgOut,
    RefreshRequest,
    SignupRequest,
    TokenResponse,
    UserOut,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=TokenResponse, status_code=201)
async def signup(body: SignupRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    existing = await UserRepository(db).get_by_email(body.email)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    org = await OrganizationRepository(db).create(body.organization_name)
    user = await UserRepository(db).create(
        org_id=org.id,
        email=body.email,
        password_hash=hash_password(body.password),
        role="admin",
    )
    await db.commit()

    return TokenResponse(
        access_token=create_access_token(user.id, org.id, user.role, user.email),
        refresh_token=create_refresh_token(user.id),
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    user = await UserRepository(db).get_by_email(body.email)
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    return TokenResponse(
        access_token=create_access_token(user.id, user.org_id, user.role, user.email),
        refresh_token=create_refresh_token(user.id),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    payload = decode_access_token(body.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")

    user = await UserRepository(db).get_by_id(UUID(payload["sub"]))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return TokenResponse(
        access_token=create_access_token(user.id, user.org_id, user.role, user.email),
        refresh_token=create_refresh_token(user.id),
    )


@router.get("/me", response_model=MeResponse)
async def me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    return MeResponse(
        user=UserOut(
            id=current_user.id,
            email=current_user.email,
            role=current_user.role,
            created_at=current_user.created_at.isoformat(),
        ),
        organization=OrgOut(
            id=org.id,
            name=org.name,
            industry=org.industry,
            onboarding_done=org.onboarding_done,
        ),
    )
