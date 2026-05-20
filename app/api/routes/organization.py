import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.org_helpers import is_org_owner
from app.api.auth.role_deps import require_org_owner, require_org_security_manager, require_role
from app.api.dependencies.plan_gate import get_usage_summary
from app.api.errors import bad_request, not_found
from app.api.safe_errors import log_and_bad_request, public_message
from app.api.schemas.organization import (
    AssetUploadResponse,
    CompleteSetupResponse,
    DeleteOrgConfirmRequest,
    MemberSettingsRequest,
    OrgProfileResponse,
    OrgSecurityRequest,
    OrgSecurityResponse,
    UpdateOrgRequest,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.external_services.s3_assets import build_object_key, upload_bytes_to_s3
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.session import get_db
from app.services.org_export_service import build_organization_export
from app.services.org_setup_service import complete_org_setup, try_auto_complete_setup

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/organization", tags=["Organization"])

AssetCategory = Literal["profile", "logo", "data", "csv", "attachment"]

_MEMBER_FIELDS = ("industry", "business_context", "entity_label", "goal_label")


def _merge_tour_guide(existing: dict[str, Any] | None, patch: dict[str, Any] | None) -> dict[str, Any]:
    base = dict(existing or {})
    if patch:
        base.update(patch)
    return base


def _to_out(org, *, current_user: User | None = None) -> OrgProfileResponse:
    owner_flag = is_org_owner(current_user, org) if current_user else False
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
        require_2fa=bool(getattr(org, "require_2fa", False)),
        is_org_owner=owner_flag,
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
    return _to_out(org, current_user=current_user)


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
    return _to_out(org, current_user=current_user)


@router.patch("/member-settings", response_model=OrgProfileResponse)
async def patch_member_settings(
    body: MemberSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgProfileResponse:
    """Update business context or tour state — any org member."""
    payload = body.model_dump(exclude_unset=True)
    if not payload:
        raise bad_request("BAD_REQUEST", "No fields provided")
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise not_found("Organization not found")

    tour_patch = payload.pop("tour_guide", None)
    for key in _MEMBER_FIELDS:
        if key in payload:
            setattr(org, key, payload[key])
    if tour_patch is not None:
        org.tour_guide = _merge_tour_guide(getattr(org, "tour_guide", None), tour_patch)

    await db.commit()
    await db.refresh(org)

    if not org.onboarding_done and (org.business_context or "").strip():
        try:
            await try_auto_complete_setup(
                db, current_user.org_id, completed_by=current_user.id
            )
            await db.refresh(org)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Auto complete-setup failed for org %s", current_user.org_id)

    return _to_out(org, current_user=current_user)


@router.patch("/security", response_model=OrgSecurityResponse)
async def patch_org_security(
    body: OrgSecurityRequest,
    current_user: User = Depends(require_org_security_manager()),
    db: AsyncSession = Depends(get_db),
) -> OrgSecurityResponse:
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise not_found("Organization not found")
    org.require_2fa = body.require_2fa
    await db.commit()
    return OrgSecurityResponse(require_2fa=org.require_2fa)


@router.post("/delete/request-code", status_code=204)
async def request_org_delete_code(
    current_user: User = Depends(require_org_owner()),
    db: AsyncSession = Depends(get_db),
) -> None:
    from app.services.org_deletion_service import send_org_delete_code

    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise not_found("Organization not found")
    await send_org_delete_code(owner=current_user, org_name=org.name, org_id=org.id)


@router.post("/delete/confirm", status_code=204)
async def confirm_org_delete(
    body: DeleteOrgConfirmRequest,
    current_user: User = Depends(require_org_owner()),
    db: AsyncSession = Depends(get_db),
) -> None:
    from app.infrastructure.audit import log_audit
    from app.services.org_deletion_service import confirm_org_deletion

    await confirm_org_deletion(
        db,
        org_id=current_user.org_id,
        owner_id=current_user.id,
        code=body.code,
    )
    await log_audit(
        db,
        org_id=current_user.org_id,
        user_id=current_user.id,
        action="org.deleted",
        resource="organization",
        resource_id=current_user.org_id,
    )
    await db.commit()


@router.post("/complete-setup", response_model=CompleteSetupResponse)
async def post_complete_setup(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompleteSetupResponse:
    """Finalize org setup and trigger the first pipeline run when ready."""
    result = await complete_org_setup(
        db, current_user.org_id, completed_by=current_user.id
    )
    if result.already_complete:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Setup is already complete for this organization",
        )
    return CompleteSetupResponse(
        message=result.message,
        onboarding_done=result.onboarding_done,
        generated_recommendations=result.generated_recommendations,
    )


@router.get("/usage")
async def get_organization_usage(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return current usage counts versus plan limits for the authenticated org.

    All users can call this — it only shows data for their own org.

    Response shape:
    ```json
    {
      "plan": "free",
      "limits": {
        "api_keys":                   { "used": 1, "limit": 1 },
        "connections":                 { "used": 2, "limit": 5 },
        "webhook_channels":            { "used": 0, "limit": 1 },
        "users":                       { "used": 2, "limit": 3 },
        "pipeline_runs_this_month":    { "used": 3, "limit": 20 },
        "agent_queries_this_month":    { "used": 12, "limit": 100 },
        "studio_executions_today":     { "used": 5, "limit": 600, "resets_at": "2025-05-20T00:00:00Z" }
      }
    }
    ```

    `limit: null` means unlimited (Pro plan or self-hosted).
    `resets_at` on `studio_executions_today` is the next **00:00 UTC** (daily counter key rolls on UTC date).
    """
    return await get_usage_summary(db, current_user.org_id)


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
    """Upload a file using the configured storage backend (S3, MinIO, or local filesystem)."""
    max_bytes = 50 * 1024 * 1024
    data = await file.read()
    if len(data) > max_bytes:
        raise bad_request("BAD_REQUEST", "File too large (max 50MB)")
    filename = file.filename or "upload"
    try:
        from app.infrastructure.external_services.s3_assets import upload_bytes
        url, key = await upload_bytes(
            data,
            org_id=current_user.org_id,
            category=category,
            filename=filename,
            content_type=file.content_type,
        )
    except RuntimeError as exc:
        raise log_and_bad_request(
            "STORAGE_NOT_CONFIGURED",
            exc,
            user_message=public_message("STORAGE_NOT_CONFIGURED"),
        ) from exc
    return AssetUploadResponse(url=url, category=category, object_key=key)
