import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.organization import OrgProfileResponse, UpdateOrgRequest
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/org", tags=["org"])


@router.get("/profile", response_model=OrgProfileResponse)
async def get_org_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgProfileResponse:
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return OrgProfileResponse(
        id=org.id,
        name=org.name,
        industry=org.industry,
        business_context=org.business_context,
        entity_label=org.entity_label,
        goal_label=org.goal_label,
        onboarding_done=org.onboarding_done,
    )


@router.patch("/profile", response_model=OrgProfileResponse)
async def update_org_profile(
    body: UpdateOrgRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgProfileResponse:
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field must be provided",
        )
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    for key, value in payload.items():
        setattr(org, key, value)
    await db.flush()
    return OrgProfileResponse(
        id=org.id,
        name=org.name,
        industry=org.industry,
        business_context=org.business_context,
        entity_label=org.entity_label,
        goal_label=org.goal_label,
        onboarding_done=org.onboarding_done,
    )
