from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.errors import bad_request, not_found
from app.api.schemas.organization import OrgProfileResponse, UpdateOrgRequest
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/organization", tags=["Organization"])


def _to_out(org) -> OrgProfileResponse:
    return OrgProfileResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        industry=org.industry,
        business_context=org.business_context,
        entity_label=org.entity_label,
        goal_label=org.goal_label,
        plan=org.plan,
        timezone=org.timezone,
        logo_url=org.logo_url,
        onboarding_done=org.onboarding_done,
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


@router.get("", response_model=OrgProfileResponse)
async def get_organization(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgProfileResponse:
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise not_found("Organization not found")
    return _to_out(org)


@router.put("", response_model=OrgProfileResponse)
async def update_organization(
    body: UpdateOrgRequest,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> OrgProfileResponse:
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise bad_request("BAD_REQUEST", "No fields provided")
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise not_found("Organization not found")
    for key, value in payload.items():
        setattr(org, key, value)
    await db.commit()
    await db.refresh(org)
    return _to_out(org)
