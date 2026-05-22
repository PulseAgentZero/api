"""Billing endpoints — Paystack payment integration.

## Cloud subscriptions  (deployment_mode = "cloud")

Manages recurring Pro plan subscriptions for cloud-hosted Entivia workspaces.

  1. POST /billing/initialize
       Start a Paystack checkout. Returns an `authorization_url` to redirect the
       user to. Paystack collects payment, then redirects to `callback_url` with
       a `?reference=xxx` query param.

  2. GET  /billing/verify/{reference}
       Call this after the user is redirected back. Verifies the payment with
       Paystack, activates the Pro subscription, and updates the org plan.

  3. GET  /billing/subscription
       Returns the current subscription state for the authenticated org.

  4. GET  /billing/subscription/manage-link
       Returns a Paystack URL for the customer to update their card on file.

  5. POST /billing/subscription/cancel
       Disables auto-renewal. The subscription stays active until
       `next_payment_date`, then Paystack sends a `subscription.disable` webhook
       which downgrades the org to free.

  6. POST /billing/webhook  (no JWT — Paystack posts here directly)
       Receives Paystack events. Signature is verified with HMAC-SHA512 before
       any processing. Always returns 200 so Paystack does not retry.

       Events handled:
         charge.success          → confirms payment, sends success email
         subscription.create     → binds SUB_xxx / email_token / next_payment_date
         subscription.disable    → downgrades org to free (cancelled / completed)
         subscription.not_renew  → marks subscription as non-renewing
         invoice.payment_failed  → marks subscription as attention, sets grace timer, sends failure email

  Cloud tiers: `growth` and `pro` (see `POST /billing/initialize` body `plan` field).

## Self-hosted license purchase  (any deployment_mode)

One-time payment that delivers a signed license key by email. The buyer then
activates the key on their self-hosted Entivia instance via POST /license/activate.

  7. POST /billing/self-hosted/initialize
       Starts a one-time Paystack charge. Returns `authorization_url`.

  8. GET  /billing/self-hosted/verify/{reference}
       Verifies the payment, requests a signed license key from the Entivia license
       server, and emails it to the purchaser. Falls back gracefully if the
       license server is temporarily unreachable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role

MANAGER_PLUS = require_role("admin", "manager")
from app.api.errors import bad_request, not_found, unauthorized
from app.config.settings import settings
from app.infrastructure.database.models.license_issuance import LicenseIssuance
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.subscription import Subscription
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db
from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis.rate_limit import (
    client_ip,
    enforce_auth_email_limit,
    enforce_auth_ip_limit,
)
from app.services.email_queue import queue_email
from app.services.billing_entitlements import (
    get_effective_cloud_plan,
    paystack_plan_code_for_tier,
    subscription_response,
    tier_from_paystack_plan_code,
)
from app.services.license_portal import (
    LicensePortalError,
    LicensePortalUnauthorized,
    create_portal_session_token,
    decode_portal_session_token,
    consume_magic_link_token,
    issue_magic_link_token,
    resend_license_key_email,
)

SELFHOST_INIT_IP_PER_HOUR = 30
SELFHOST_VERIFY_IP_PER_HOUR = 60
PORTAL_LINK_IP_PER_HOUR = 30
PORTAL_LINK_EMAIL_PER_HOUR = 5
PORTAL_EXCHANGE_IP_PER_HOUR = 60
PORTAL_RESEND_IP_PER_HOUR = 20

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["Billing"])

PAYSTACK_BASE = "https://api.paystack.co"


# ── Guards ────────────────────────────────────────────────────────────────────

def _require_cloud() -> None:
    """Raise 404 on self-hosted deployments — subscriptions are cloud-only."""
    if settings.DEPLOYMENT_MODE == "self_hosted":
        raise not_found()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _paystack_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.get_paystack_secret_key()}",
        "Content-Type": "application/json",
    }


def _verify_webhook_signature(body: bytes, signature: str | None) -> bool:
    secret = settings.get_paystack_secret_key()
    if not secret or not signature:
        return False
    computed = hmac.new(secret.encode("utf-8"), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(computed, signature)


async def _get_or_create_subscription(db: AsyncSession, org_id: uuid.UUID) -> Subscription:
    result = await db.execute(select(Subscription).where(Subscription.org_id == org_id))
    sub = result.scalar_one_or_none()
    if sub is None:
        sub = Subscription(org_id=org_id, plan="free", status="inactive")
        db.add(sub)
        await db.flush()
    return sub


async def _find_sub_by_code(db: AsyncSession, subscription_code: str) -> Subscription | None:
    result = await db.execute(
        select(Subscription).where(Subscription.paystack_subscription_code == subscription_code)
    )
    return result.scalar_one_or_none()


async def _find_sub_by_customer(db: AsyncSession, customer_code: str) -> Subscription | None:
    result = await db.execute(
        select(Subscription).where(Subscription.paystack_customer_code == customer_code)
    )
    return result.scalar_one_or_none()


async def _org_admin_email(db: AsyncSession, org_id: uuid.UUID) -> str | None:
    """Return the email of the first active admin in the org, or None."""
    result = await db.execute(
        select(User.email)
        .where(User.org_id == org_id, User.role == "admin", User.is_active.is_(True))
        .limit(1)
    )
    row = result.first()
    return row[0] if row else None


async def _org_name(db: AsyncSession, org_id: uuid.UUID) -> str:
    org = await db.get(Organization, org_id)
    return org.name if org else "your organisation"


async def _subscription_payload(db: AsyncSession, sub: Subscription) -> dict:
    effective = await get_effective_cloud_plan(db, sub.org_id, sub=sub)
    return subscription_response(sub, effective_plan=effective)


def _paystack_key_mode() -> str:
    key = settings.get_paystack_secret_key() or ""
    if key.startswith("sk_live_"):
        return "live"
    if key.startswith("sk_test_"):
        return "test"
    return "unknown"


async def _fetch_paystack_plan(
    client: httpx.AsyncClient,
    *,
    plan_code: str,
    tier: str,
) -> dict[str, Any]:
    """Fetch a configured Paystack plan before initializing checkout.

    Paystack returns the same "Plan not found" error from transaction
    initialization when the plan code belongs to a different integration mode
    (for example, a live dashboard plan used with a test secret key). Fetching
    the plan first lets us log and return a targeted configuration error.
    """
    resp = await client.get(
        f"{PAYSTACK_BASE}/plan/{plan_code}",
        headers=_paystack_headers(),
    )
    if resp.status_code == 200:
        return resp.json().get("data", {}) or {}

    logger.error(
        "Paystack %s plan lookup failed for %s using %s key: %s — %s",
        tier,
        plan_code,
        _paystack_key_mode(),
        resp.status_code,
        resp.text[:300],
    )
    if resp.status_code == 404:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Paystack {tier} plan was not found for the configured "
                f"{_paystack_key_mode()} secret key. Use a live secret key for "
                "live dashboard plans, or a test secret key for test dashboard plans."
            ),
        )
    raise HTTPException(status_code=502, detail="Could not validate Paystack plan")


# ── Schemas ───────────────────────────────────────────────────────────────────

class InitializePaymentRequest(BaseModel):
    plan: Literal["pro", "growth"] = Field(
        default="pro",
        description="Cloud tier to subscribe to. Maps to Paystack plan codes via server env.",
    )
    callback_url: str = Field(
        ...,
        description=(
            "URL Paystack redirects to after checkout. "
            "Append the returned `reference` to this URL and call "
            "`GET /billing/verify/{reference}` to activate the subscription."
        ),
        examples=["https://app.yoursite.com/settings/billing?verify=1"],
    )


class InitializePaymentResponse(BaseModel):
    authorization_url: str = Field(..., description="Redirect the user to this URL to complete payment.")
    access_code: str = Field(..., description="Paystack access code for the transaction.")
    reference: str = Field(..., description="Unique transaction reference. Use this to verify the payment.")


class SubscriptionResponse(BaseModel):
    plan: str = Field(..., description="Stored plan: `free`, `growth`, or `pro`.")
    effective_plan: str = Field(
        ...,
        description="Plan used for limits (may be `free` when grace period expired after failed payment).",
    )
    status: str = Field(
        ...,
        description=(
            "Subscription status. One of: `inactive` (no subscription), "
            "`active`, `non-renewing` (cancelled but not yet expired), "
            "`attention` (payment issue), `completed`, `cancelled`."
        ),
    )
    paystack_subscription_code: str | None = Field(
        None, description="Paystack SUB_xxx code. `null` until first successful payment."
    )
    next_payment_date: datetime | None = Field(
        None, description="Next scheduled billing date. `null` for free or cancelled plans."
    )
    payment_failed_at: datetime | None = Field(
        None, description="When the last renewal payment failed. `null` if not in failure state."
    )
    grace_ends_at: datetime | None = Field(
        None,
        description="End of grace period after failed payment. After this, effective_plan becomes `free`.",
    )
    payment_attention: bool = Field(
        False,
        description="True when payment failed but grace period has not expired — show update-card banner.",
    )
    manage_link_available: bool = Field(
        False,
        description="True when Paystack subscription code exists and manage-link can be generated.",
    )
    updated_at: datetime | None = Field(None, description="Last time this record was modified.")


class ManageLinkResponse(BaseModel):
    link: str = Field(..., description="Paystack-hosted URL for the customer to update their card.")


class SelfHostedInitializeRequest(BaseModel):
    email: EmailStr = Field(
        ...,
        description="Email address to deliver the license key to after payment.",
        examples=["admin@yourcompany.com"],
    )
    callback_url: str = Field(
        ...,
        description="URL Paystack redirects to after checkout.",
        examples=["https://yoursite.com/license?verify=1"],
    )


class SelfHostedVerifyResponse(BaseModel):
    status: str = Field(..., description="`success` when license key has been emailed.")
    message: str = Field(..., description="Human-readable status message.")
    license_key: str | None = Field(
        None,
        description=(
            "The signed license key, if the license server responded successfully. "
            "Always delivered by email regardless."
        ),
    )


# ── Cloud subscription endpoints ──────────────────────────────────────────────


@router.post(
    "/initialize",
    response_model=InitializePaymentResponse,
    summary="Start a Pro plan checkout",
    description=(
        "**Cloud only.** Initialises a Paystack transaction for the Entivia Pro plan. "
        "Redirect the user to `authorization_url` to complete payment. "
        "After Paystack redirects back to `callback_url`, call "
        "`GET /billing/verify/{reference}` to activate the subscription.\n\n"
        "> The Pro plan code is configured server-side (`PAYSTACK_PRO_PLAN_CODE`). "
        "The frontend only needs to supply a `callback_url`."
    ),
)
async def initialize_payment(
    body: InitializePaymentRequest,
    current_user: User = Depends(MANAGER_PLUS),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_cloud()

    tier = body.plan.lower()
    plan_code = paystack_plan_code_for_tier(tier)
    if not plan_code:
        raise HTTPException(
            status_code=503,
            detail=f"{tier.title()} plan is not configured on this server (Paystack plan code missing)",
        )

    if not settings.get_paystack_secret_key():
        raise HTTPException(status_code=503, detail="Payment service is not configured")

    async with httpx.AsyncClient(timeout=15.0) as client:
        plan = await _fetch_paystack_plan(client, plan_code=plan_code, tier=tier)
        payload: dict[str, Any] = {
            "email": current_user.email,
            # Paystack docs say the plan overrides amount, but amount remains a
            # required transaction field. Use the fetched plan amount so the
            # checkout request matches the plan exactly.
            "amount": int(plan.get("amount") or 100),
            "plan": plan_code,
            "callback_url": body.callback_url,
            "metadata": {
                "org_id": str(current_user.org_id),
                "user_id": str(current_user.id),
                "plan": tier,
                "paystack_plan_code": plan_code,
                "purchase_type": "cloud_subscription",
            },
        }
        resp = await client.post(
            f"{PAYSTACK_BASE}/transaction/initialize",
            json=payload,
            headers=_paystack_headers(),
        )

    if resp.status_code != 200:
        logger.error("Paystack initialize failed: %s — %s", resp.status_code, resp.text[:300])
        raise HTTPException(status_code=502, detail="Payment initialization failed")

    data = resp.json().get("data", {})
    return {
        "authorization_url": data["authorization_url"],
        "access_code": data["access_code"],
        "reference": data["reference"],
    }


@router.get(
    "/verify/{reference}",
    response_model=SubscriptionResponse,
    summary="Verify payment and activate Pro subscription",
    description=(
        "**Cloud only.** Call this after Paystack redirects the user back to your `callback_url`. "
        "Pass the `reference` query param from the redirect URL as the path parameter here.\n\n"
        "On success:\n"
        "- The org plan is updated to `pro`\n"
        "- A subscription record is created / updated\n"
        "- A confirmation email is sent to the account owner\n\n"
        "Returns the full subscription state."
    ),
)
async def verify_payment(
    reference: str,
    current_user: User = Depends(MANAGER_PLUS),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_cloud()

    if not settings.get_paystack_secret_key():
        raise HTTPException(status_code=503, detail="Payment service is not configured")

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{PAYSTACK_BASE}/transaction/verify/{reference}",
            headers=_paystack_headers(),
        )

    if resp.status_code != 200:
        raise bad_request("VERIFICATION_FAILED", "Could not verify transaction with Paystack")

    tx = resp.json().get("data", {})
    if tx.get("status") != "success":
        raise bad_request("PAYMENT_NOT_SUCCESSFUL", f"Transaction status is '{tx.get('status')}'")

    meta = tx.get("metadata") or {}
    if str(meta.get("org_id", "")) != str(current_user.org_id):
        raise HTTPException(status_code=403, detail="Transaction does not belong to this organization")

    authorization = tx.get("authorization") or {}
    customer = tx.get("customer") or {}

    tier = str(meta.get("plan") or "pro").lower()
    if tier not in ("pro", "growth"):
        tier = "pro"

    sub = await _get_or_create_subscription(db, current_user.org_id)
    sub.plan = tier
    sub.status = "active"
    sub.payment_failed_at = None
    sub.authorization_code = authorization.get("authorization_code")
    sub.paystack_customer_code = customer.get("customer_code")
    sub.paystack_plan_code = paystack_plan_code_for_tier(tier)

    org = await db.get(Organization, current_user.org_id)
    if org:
        org.plan = tier

    await db.commit()
    await db.refresh(sub)

    if sub.next_payment_date:
        next_date = sub.next_payment_date.strftime("%B %d, %Y")
    else:
        next_date = "—"
    org_display = org.name if org else "your organisation"
    await queue_email(
        "subscription_success",
        to=current_user.email,
        org_name=org_display,
        next_payment_date=next_date,
        plan=tier,
    )

    return await _subscription_payload(db, sub)


@router.get(
    "/subscription",
    response_model=SubscriptionResponse,
    summary="Get current subscription",
    description=(
        "**Cloud only.** Returns the subscription state for the authenticated org. "
        "Use this to render billing status in the dashboard Settings → Billing page.\n\n"
        "If the org has never subscribed, returns `plan: free` and `status: inactive`."
    ),
)
async def get_subscription(
    current_user: User = Depends(MANAGER_PLUS),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_cloud()
    sub = await _get_or_create_subscription(db, current_user.org_id)
    await db.commit()
    return await _subscription_payload(db, sub)


@router.get(
    "/subscription/manage-link",
    response_model=ManageLinkResponse,
    summary="Get Paystack link to update payment method",
    description=(
        "**Cloud only.** Returns a Paystack-hosted URL where the customer can update "
        "the card on file for their subscription. Requires an active Paystack subscription code."
    ),
)
async def get_subscription_manage_link(
    current_user: User = Depends(MANAGER_PLUS),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_cloud()

    sub = await _get_or_create_subscription(db, current_user.org_id)
    if not sub.paystack_subscription_code:
        raise bad_request("NO_ACTIVE_SUBSCRIPTION", "No Paystack subscription found for this organization")

    if not settings.get_paystack_secret_key():
        raise HTTPException(status_code=503, detail="Payment service is not configured")

    code = sub.paystack_subscription_code
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{PAYSTACK_BASE}/subscription/{code}/manage/link",
            headers=_paystack_headers(),
        )

    if resp.status_code != 200:
        logger.error("Paystack manage link failed: %s — %s", resp.status_code, resp.text[:300])
        raise HTTPException(status_code=502, detail="Could not generate payment update link")

    link = resp.json().get("data", {}).get("link")
    if not link:
        raise HTTPException(status_code=502, detail="Paystack did not return a manage link")

    return {"link": link}


@router.post(
    "/subscription/cancel",
    response_model=SubscriptionResponse,
    summary="Cancel Pro subscription",
    description=(
        "**Cloud only.** Disables auto-renewal on the active Pro subscription. "
        "The subscription remains active until `next_payment_date` — no refund is issued. "
        "On that date, Paystack sends a `subscription.disable` webhook which "
        "automatically downgrades the org plan to `free`.\n\n"
        "Returns status `non-renewing` immediately."
    ),
)
async def cancel_subscription(
    current_user: User = Depends(MANAGER_PLUS),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_cloud()

    sub = await _get_or_create_subscription(db, current_user.org_id)

    if not sub.paystack_subscription_code or not sub.paystack_email_token:
        raise bad_request("NO_ACTIVE_SUBSCRIPTION", "No active Paystack subscription found")

    if not settings.get_paystack_secret_key():
        raise HTTPException(status_code=503, detail="Payment service is not configured")

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{PAYSTACK_BASE}/subscription/disable",
            json={
                "code": sub.paystack_subscription_code,
                "token": sub.paystack_email_token,
            },
            headers=_paystack_headers(),
        )

    if resp.status_code not in (200, 201):
        logger.error("Paystack disable subscription failed: %s — %s", resp.status_code, resp.text[:300])
        raise HTTPException(status_code=502, detail="Failed to cancel subscription with Paystack")

    sub.status = "non-renewing"
    await db.commit()
    await db.refresh(sub)

    access_until = (
        sub.next_payment_date.strftime("%B %d, %Y")
        if sub.next_payment_date
        else "the end of your current billing period"
    )
    org_display = await _org_name(db, sub.org_id)
    await queue_email(
        "subscription_cancelled",
        to=current_user.email,
        org_name=org_display,
        access_until=access_until,
        plan=sub.plan,
    )

    return await _subscription_payload(db, sub)


# ── Paystack webhook ──────────────────────────────────────────────────────────


@router.post(
    "/webhook",
    status_code=200,
    include_in_schema=False,
    description="Paystack webhook receiver. Do not call this directly — it is for Paystack only.",
)
async def paystack_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_paystack_signature: str | None = Header(default=None),
) -> dict:
    body = await request.body()

    if not _verify_webhook_signature(body, x_paystack_signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event: str = payload.get("event", "")
    data: dict = payload.get("data", {})

    logger.info("Paystack webhook received: %s", event)

    try:
        await _dispatch(db, event, data)
        await db.commit()
    except Exception:
        logger.exception("Error processing Paystack webhook event '%s'", event)
        # Return 200 regardless so Paystack does not retry

    return {"status": "ok"}


async def _dispatch(db: AsyncSession, event: str, data: dict) -> None:
    handlers = {
        "charge.success": _on_charge_success,
        "subscription.create": _on_subscription_create,
        "subscription.disable": _on_subscription_disable,
        "subscription.not_renew": _on_subscription_not_renew,
        "invoice.payment_failed": _on_invoice_payment_failed,
    }
    handler = handlers.get(event)
    if handler:
        await handler(db, data)
    else:
        logger.debug("Unhandled Paystack event: %s", event)


async def _on_charge_success(db: AsyncSession, data: dict) -> None:
    """Payment succeeded. For first-time subscriptions the org_id is in metadata."""
    meta = data.get("metadata") or {}
    customer = data.get("customer") or {}
    authorization = data.get("authorization") or {}
    customer_code = customer.get("customer_code")

    # Only process cloud subscription charges here
    if isinstance(meta, dict) and meta.get("purchase_type") == "self_hosted_license":
        return  # handled by self-hosted verify endpoint, not by webhook

    org_id_str = meta.get("org_id") if isinstance(meta, dict) else None
    if org_id_str:
        try:
            org_id = uuid.UUID(org_id_str)
        except ValueError:
            return
        sub = await _get_or_create_subscription(db, org_id)
        if authorization.get("authorization_code"):
            sub.authorization_code = authorization["authorization_code"]
        if customer_code:
            sub.paystack_customer_code = customer_code
        return

    # Renewal charge (no org_id in metadata) — look up by customer code
    if customer_code:
        sub = await _find_sub_by_customer(db, customer_code)
        if sub and sub.status == "attention":
            sub.status = "active"
            sub.payment_failed_at = None
            email = await _org_admin_email(db, sub.org_id)
            org = await _org_name(db, sub.org_id)
            if email and sub.next_payment_date:
                next_date = sub.next_payment_date.strftime("%B %d, %Y")
                await queue_email(
                    "subscription_success",
                    to=email,
                    org_name=org,
                    next_payment_date=next_date,
                    plan=sub.plan,
                )


async def _on_subscription_create(db: AsyncSession, data: dict) -> None:
    """Subscription created. Bind Paystack codes and next payment date."""
    customer = data.get("customer") or {}
    plan_data = data.get("plan") or {}
    customer_code = customer.get("customer_code")
    subscription_code = data.get("subscription_code")
    email_token = data.get("email_token")
    plan_code = plan_data.get("plan_code")
    next_payment_raw = data.get("next_payment_date")
    paystack_status = data.get("status", "active")

    next_payment: datetime | None = None
    if next_payment_raw:
        try:
            next_payment = datetime.fromisoformat(next_payment_raw.replace("Z", "+00:00"))
        except ValueError:
            pass

    sub = await _find_sub_by_customer(db, customer_code) if customer_code else None
    if sub is None:
        logger.warning("subscription.create: no local subscription for customer %s", customer_code)
        return

    tier = tier_from_paystack_plan_code(plan_code)

    sub.paystack_subscription_code = subscription_code
    sub.paystack_email_token = email_token
    sub.paystack_plan_code = plan_code
    sub.next_payment_date = next_payment
    sub.status = paystack_status
    sub.plan = tier
    sub.payment_failed_at = None

    org = await db.get(Organization, sub.org_id)
    if org:
        org.plan = tier

    # Send success email now that we have the next_payment_date
    email = await _org_admin_email(db, sub.org_id)
    if email:
        next_date = next_payment.strftime("%B %d, %Y") if next_payment else "—"
        org_display = org.name if org else "your organisation"
        await queue_email(
            "subscription_success",
            to=email,
            org_name=org_display,
            next_payment_date=next_date,
            plan=tier,
        )


async def _on_subscription_disable(db: AsyncSession, data: dict) -> None:
    """Subscription cancelled or completed — downgrade org to free."""
    subscription_code = data.get("subscription_code")
    paystack_status = data.get("status", "cancelled")

    sub = await _find_sub_by_code(db, subscription_code) if subscription_code else None
    if sub is None:
        return

    sub.status = paystack_status
    if paystack_status in ("cancelled", "complete", "completed"):
        sub.plan = "free"
        org = await db.get(Organization, sub.org_id)
        if org:
            org.plan = "free"


async def _on_subscription_not_renew(db: AsyncSession, data: dict) -> None:
    """Subscription is active but won't auto-renew (customer cancelled)."""
    subscription_code = data.get("subscription_code")
    sub = await _find_sub_by_code(db, subscription_code) if subscription_code else None
    if sub:
        sub.status = "non-renewing"


async def _on_invoice_payment_failed(db: AsyncSession, data: dict) -> None:
    """Renewal charge failed. Mark subscription and email the admin."""
    subscription_data = data.get("subscription") or {}
    subscription_code = subscription_data.get("subscription_code")

    sub = await _find_sub_by_code(db, subscription_code) if subscription_code else None
    if sub is None:
        return

    sub.status = "attention"
    sub.payment_failed_at = datetime.now(timezone.utc)

    email = await _org_admin_email(db, sub.org_id)
    org = await _org_name(db, sub.org_id)
    if email:
        await queue_email("subscription_failed", to=email, org_name=org, plan=sub.plan)
    try:
        from app.services.notification_service import notify_payment_failed

        await notify_payment_failed(db, sub.org_id)
    except Exception:
        logger.exception("In-app payment-failed notification skipped for org %s", sub.org_id)


# ── Self-hosted license purchase endpoints ────────────────────────────────────


@router.post(
    "/self-hosted/initialize",
    response_model=InitializePaymentResponse,
    summary="Start a self-hosted license purchase",
    description=(
        "Initialises a **one-time** Paystack charge for an Entivia self-hosted license. "
        "Available on all deployment modes.\n\n"
        "The purchase price is configured server-side (`PAYSTACK_SELFHOSTED_LICENSE_PRICE` in kobo). "
        "After Paystack redirects to `callback_url`, call "
        "`GET /billing/self-hosted/verify/{reference}` to receive the license key.\n\n"
        "No recurring billing — this is a single charge."
    ),
)
async def initialize_selfhosted_purchase(
    body: SelfHostedInitializeRequest,
    request: Request,
) -> dict:
    """Anonymous: anyone with a payment method can buy a self-hosted license.

    Buyers do not need a cloud workspace account. The Paystack metadata stores
    only the delivery email (and the buyer IP, for fraud review). The license
    key is bound to the eventual self-hosted ``org_id`` on first activation via
    ``POST /license/activate`` from the customer's own instance.
    """
    if settings.DEPLOYMENT_MODE == "self_hosted":
        purchase_url = f"{settings.MARKETING_URL.rstrip('/')}/pricing/self-hosted"
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PURCHASE_OFF_INSTANCE",
                "message": (
                    "Self-hosted licenses are purchased from the Entivia marketing site, "
                    "not from your self-hosted instance. After payment, paste the plc_… "
                    "key into Settings → License."
                ),
                "purchase_url": purchase_url,
            },
        )

    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "selfhost_init",
        limit=SELFHOST_INIT_IP_PER_HOUR, window_sec=3600,
        message="Too many license-purchase attempts. Please try again later.",
    )

    price = settings.PAYSTACK_SELFHOSTED_LICENSE_PRICE
    if not price:
        raise HTTPException(status_code=503, detail="Self-hosted license purchase is not configured on this server")

    if not settings.get_paystack_secret_key():
        raise HTTPException(status_code=503, detail="Payment service is not configured")

    delivery_email = str(body.email).strip().lower()

    payload: dict[str, Any] = {
        "email": delivery_email,
        "amount": price,
        "callback_url": body.callback_url,
        "metadata": {
            "delivery_email": delivery_email,
            "purchase_type": "self_hosted_license",
            "buyer_ip": client_ip(request),
        },
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{PAYSTACK_BASE}/transaction/initialize",
            json=payload,
            headers=_paystack_headers(),
        )

    if resp.status_code != 200:
        logger.error("Paystack self-hosted init failed: %s — %s", resp.status_code, resp.text[:300])
        raise HTTPException(status_code=502, detail="Payment initialization failed")

    data = resp.json().get("data", {})
    return {
        "authorization_url": data["authorization_url"],
        "access_code": data["access_code"],
        "reference": data["reference"],
    }


@router.get(
    "/self-hosted/verify/{reference}",
    response_model=SelfHostedVerifyResponse,
    summary="Verify self-hosted license payment and receive key",
    description=(
        "Call this after Paystack redirects back from the self-hosted license checkout.\n\n"
        "On success:\n"
        "1. Payment is verified with Paystack\n"
        "2. A signed license key is requested from the Entivia license server\n"
        "3. The key is emailed to the address provided during checkout\n\n"
        "The `license_key` field is also returned in the response body for "
        "immediate display to the user. If the license server is temporarily "
        "unreachable, `license_key` will be `null` but the key will still be "
        "emailed once the server recovers."
    ),
)
async def verify_selfhosted_purchase(
    reference: str,
    request: Request,
) -> dict:
    """Anonymous: callable by anyone holding the Paystack ``reference``.

    Idempotent — the license server returns the same key for repeated calls
    with the same payment reference.
    """
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "selfhost_verify",
        limit=SELFHOST_VERIFY_IP_PER_HOUR, window_sec=3600,
    )

    if not settings.get_paystack_secret_key():
        raise HTTPException(status_code=503, detail="Payment service is not configured")

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{PAYSTACK_BASE}/transaction/verify/{reference}",
            headers=_paystack_headers(),
        )

    if resp.status_code != 200:
        raise bad_request("VERIFICATION_FAILED", "Could not verify transaction with Paystack")

    tx = resp.json().get("data", {})
    if tx.get("status") != "success":
        raise bad_request("PAYMENT_NOT_SUCCESSFUL", f"Transaction status is '{tx.get('status')}'")

    meta = tx.get("metadata") or {}
    if meta.get("purchase_type") != "self_hosted_license":
        raise bad_request("WRONG_TRANSACTION_TYPE", "This reference is not for a self-hosted license purchase")

    delivery_email = (meta.get("delivery_email") or tx.get("customer", {}).get("email") or "").strip().lower()
    if not delivery_email:
        raise bad_request("MISSING_DELIVERY_EMAIL", "No delivery email associated with this transaction")

    # ``org_id`` is intentionally optional for anonymous purchases — the buyer
    # binds the license to their own self-hosted org_id on first activation.
    purchaser_org_id = meta.get("org_id")

    license_key, expires_at = await _issue_license_key(
        payment_reference=reference,
        email=delivery_email,
        org_id=str(purchaser_org_id) if purchaser_org_id else None,
    )

    if license_key:
        await queue_email(
            "license_key",
            to=delivery_email,
            license_key=license_key,
            expires_at=expires_at,
        )
        return {
            "status": "success",
            "message": f"License key delivered to {delivery_email}",
            "license_key": license_key,
        }

    # License server unreachable — purchase is recorded, key delivered when server recovers
    logger.error(
        "License server did not return key for payment %s (org %s) — will retry delivery",
        reference, purchaser_org_id or "pending",
    )
    return {
        "status": "success",
        "message": (
            f"Payment confirmed. Your license key will be emailed to {delivery_email} "
            "within a few minutes. If it doesn't arrive, contact support with your "
            f"payment reference: {reference}"
        ),
        "license_key": None,
    }


async def _issue_license_key(
    payment_reference: str,
    email: str,
    org_id: str | None,
) -> tuple[str | None, str | None]:
    """Request a signed license key from the Entivia license server.

    Returns (license_key, expires_at) on success, (None, None) on failure.
    The license server endpoint: POST {LICENSE_SERVER_URL}/api/v1/keys/purchase
    """
    url = f"{settings.LICENSE_SERVER_URL.rstrip('/')}/api/v1/keys/purchase"
    headers = {"Content-Type": "application/json"}
    api_key = settings.LICENSE_SERVER_API_KEY
    if api_key:
        headers["X-License-Api-Key"] = api_key
    body: dict[str, Any] = {
        "payment_reference": payment_reference,
        "email": email,
        "product": "self_hosted",
    }
    if org_id:
        body["org_id"] = org_id
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json=body,
                headers=headers,
            )
        if resp.status_code in (200, 201):
            data = resp.json().get("data") or resp.json()
            key = data.get("license_key") or data.get("key")
            expires = data.get("expires_at")
            if key:
                return str(key), str(expires) if expires else None
    except Exception as exc:
        logger.warning("License server key issuance failed for ref %s: %s", payment_reference, exc)

    return None, None


# ── Self-hosted license customer portal (magic-link auth) ─────────────────────
#
# Lets buyers retrieve license keys they purchased without creating a cloud
# workspace. Mirrors the pattern used by GitLab customers, n8n, Posthog etc.
#
#   1. POST /billing/self-hosted/portal/request-link
#        Body: { email, callback_url }. Emails a one-time magic link if any
#        licenses exist for this email; otherwise returns success anyway to
#        avoid leaking whether the email is a customer.
#
#   2. POST /billing/self-hosted/portal/exchange
#        Body: { token }. Returns a short-lived portal session JWT (15 min).
#
#   3. GET  /billing/self-hosted/portal/licenses
#        Header: Authorization: Bearer <portal session JWT>. Lists license
#        keys issued to the session email.
#
#   4. POST /billing/self-hosted/portal/licenses/{jti}/resend
#        Re-emails the license key to its original delivery address.


class PortalRequestLinkRequest(BaseModel):
    email: EmailStr = Field(
        ...,
        description="Email used at checkout. We'll email any licenses bound to this address.",
    )
    callback_url: str = Field(
        ...,
        description=(
            "URL the magic link should land on. We append `?token=<token>` to it. "
            "The page should call `POST /billing/self-hosted/portal/exchange` with the token."
        ),
        examples=["https://entivia.online/pricing/self-hosted/portal/callback"],
    )


class PortalRequestLinkResponse(BaseModel):
    status: str = Field(..., description="Always `ok` (we don't reveal whether the email is a customer).")
    message: str = Field(..., description="Human-readable confirmation.")


class PortalExchangeRequest(BaseModel):
    token: str = Field(..., min_length=8)


class PortalExchangeResponse(BaseModel):
    portal_token: str = Field(..., description="Short-lived JWT (15 min). Pass as `Authorization: Bearer ...`.")
    email: EmailStr = Field(..., description="Email this session is bound to.")
    expires_in: int = Field(..., description="Seconds until the portal token expires.")


class PortalLicenseRow(BaseModel):
    jti: str
    plan: str
    features: list[str]
    seat_limit: int | None
    expires_at: datetime | None
    revoked_at: datetime | None
    issued_at: datetime
    payment_reference: str
    license_key_preview: str = Field(
        ..., description="Last 8 chars of the key, e.g. `…f3a2b9c1`. Full key is delivered by email."
    )


class PortalLicensesResponse(BaseModel):
    email: EmailStr
    licenses: list[PortalLicenseRow]


class PortalResendResponse(BaseModel):
    status: str
    message: str


async def _portal_session_email(authorization: str | None) -> str:
    """Decode the portal Bearer token from the Authorization header."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise unauthorized("PORTAL_AUTH_REQUIRED", "Portal session required")
    raw = authorization.split(" ", 1)[1].strip()
    try:
        email = decode_portal_session_token(raw)
    except LicensePortalUnauthorized as exc:
        raise unauthorized("PORTAL_SESSION_EXPIRED", str(exc)) from exc
    return email


@router.post(
    "/self-hosted/portal/request-link",
    response_model=PortalRequestLinkResponse,
    summary="Email a magic link to access purchased license keys",
    description=(
        "**Anonymous.** Sends a one-time magic link (15 min TTL) to the supplied "
        "email if any self-hosted licenses have been issued to it. Always returns "
        "200 with the same message, so the response cannot be used to enumerate "
        "customer emails."
    ),
)
async def portal_request_link(
    body: PortalRequestLinkRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "selfhost_portal_link",
        limit=PORTAL_LINK_IP_PER_HOUR, window_sec=3600,
    )
    await enforce_auth_email_limit(
        r, str(body.email), "selfhost_portal_link",
        limit=PORTAL_LINK_EMAIL_PER_HOUR, window_sec=3600,
        message="Too many magic-link requests for that email. Please try again in an hour.",
    )

    email = str(body.email).strip().lower()

    exists = await db.execute(
        select(LicenseIssuance.id).where(LicenseIssuance.email == email).limit(1)
    )
    if exists.first() is None:
        # Don't leak existence of the customer record.
        return {
            "status": "ok",
            "message": f"If a purchase exists for {email}, a sign-in link has been sent.",
        }

    try:
        token = await issue_magic_link_token(email)
    except LicensePortalError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "PORTAL_UNAVAILABLE", "message": str(exc)},
        ) from exc

    sep = "&" if "?" in body.callback_url else "?"
    link = f"{body.callback_url}{sep}token={token}"

    await queue_email(
        "license_portal_link",
        to=email,
        link=link,
        ip=client_ip(request),
    )

    return {
        "status": "ok",
        "message": f"If a purchase exists for {email}, a sign-in link has been sent.",
    }


@router.post(
    "/self-hosted/portal/exchange",
    response_model=PortalExchangeResponse,
    summary="Exchange a magic-link token for a portal session JWT",
)
async def portal_exchange(
    body: PortalExchangeRequest,
    request: Request,
) -> dict:
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "selfhost_portal_exchange",
        limit=PORTAL_EXCHANGE_IP_PER_HOUR, window_sec=3600,
    )

    email = await consume_magic_link_token(body.token)
    if not email:
        raise unauthorized(
            "MAGIC_LINK_INVALID",
            "This sign-in link has expired or has already been used. Request a new one.",
        )

    portal_token, ttl = create_portal_session_token(email)
    return {"portal_token": portal_token, "email": email, "expires_in": ttl}


@router.get(
    "/self-hosted/portal/licenses",
    response_model=PortalLicensesResponse,
    summary="List license keys for the current portal session",
)
async def portal_list_licenses(
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    email = await _portal_session_email(authorization)

    result = await db.execute(
        select(LicenseIssuance)
        .where(LicenseIssuance.email == email)
        .order_by(LicenseIssuance.created_at.desc())
    )
    rows = result.scalars().all()

    licenses = [
        {
            "jti": row.jti,
            "plan": row.plan,
            "features": list(row.features or []),
            "seat_limit": row.seat_limit,
            "expires_at": row.expires_at,
            "revoked_at": row.revoked_at,
            "issued_at": row.created_at,
            "payment_reference": row.payment_reference,
            "license_key_preview": f"…{row.license_key[-8:]}" if row.license_key else "",
        }
        for row in rows
    ]

    return {"email": email, "licenses": licenses}


@router.post(
    "/self-hosted/portal/licenses/{jti}/resend",
    response_model=PortalResendResponse,
    summary="Re-email a previously issued license key",
)
async def portal_resend_license(
    jti: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "selfhost_portal_resend",
        limit=PORTAL_RESEND_IP_PER_HOUR, window_sec=3600,
    )

    email = await _portal_session_email(authorization)

    result = await db.execute(
        select(LicenseIssuance).where(
            LicenseIssuance.jti == jti,
            LicenseIssuance.email == email,
        )
    )
    issuance = result.scalar_one_or_none()
    if issuance is None:
        raise not_found("License not found for this portal session")
    if issuance.revoked_at is not None:
        raise bad_request("LICENSE_REVOKED", "This license has been revoked and cannot be resent")

    await resend_license_key_email(
        to=email,
        license_key=issuance.license_key,
        expires_at=issuance.expires_at.isoformat() if issuance.expires_at else None,
    )

    return {
        "status": "ok",
        "message": f"Your license key has been re-emailed to {email}.",
    }
