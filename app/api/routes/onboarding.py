import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.onboarding import CompleteOnboardingResponse
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.post("/complete", response_model=CompleteOnboardingResponse)
async def complete_onboarding(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompleteOnboardingResponse:
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    if org.onboarding_done:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Onboarding is already complete for this organization",
        )
    org.onboarding_done = True
    await db.commit()
    return CompleteOnboardingResponse(message="Onboarding complete", onboarding_done=True)
