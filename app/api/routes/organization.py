import logging
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.errors import bad_request, not_found
from app.api.schemas.organization import AssetUploadResponse, OrgProfileResponse, UpdateOrgRequest
from app.infrastructure.database.models.user import User
from app.infrastructure.external_services.s3_assets import build_object_key, upload_bytes_to_s3
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.session import get_db
from app.services.org_export_service import build_organization_export

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/organization", tags=["Organization"])

AssetCategory = Literal["profile", "logo", "data", "csv", "attachment"]


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
        tour_guide=getattr(org, "tour_guide", None) or {},
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


@router.get("/export")
async def export_organization_data(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return a JSON snapshot of this organization. Scoped strictly to ``current_user.org_id``."""
    bundle = await build_organization_export(db, current_user.org_id)
    if not bundle:
        raise not_found("Organization not found")
    return JSONResponse(content=bundle)


@router.post("/assets/upload", response_model=AssetUploadResponse)
async def upload_organization_asset(
    category: AssetCategory = Form(..., description="profile | logo | data | csv | attachment"),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> AssetUploadResponse:
    """Upload a file to S3 under this organization's prefix. Requires ``ASSETS_S3_BUCKET``."""
    max_bytes = 50 * 1024 * 1024
    data = await file.read()
    if len(data) > max_bytes:
        raise bad_request("BAD_REQUEST", "File too large (max 50MB)")
    filename = file.filename or "upload"
    try:
        key = build_object_key(
            org_id=current_user.org_id,
            category=category,
            filename=filename,
        )
        url = upload_bytes_to_s3(
            data,
            org_id=current_user.org_id,
            category=category,
            filename=filename,
            content_type=file.content_type,
            object_key=key,
        )
    except RuntimeError as exc:
        logger.warning("asset upload failed: %s", exc)
        raise bad_request("S3_NOT_CONFIGURED", str(exc)) from exc
    return AssetUploadResponse(url=url, category=category, object_key=key)
