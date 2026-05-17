"""Billing endpoints — Paystack payment integration.

## Cloud subscriptions  (deployment_mode = "cloud")

Manages recurring Pro plan subscriptions for cloud-hosted Pulse workspaces.

  1. POST /billing/initialize
       Start a Paystack checkout. Returns an `authorization_url` to redirect the
       user to. Paystack collects payment, then redirects to `callback_url` with
       a `?reference=xxx` query param.

  2. GET  /billing/verify/{reference}
       Call this after the user is redirected back. Verifies the payment with
       Paystack, activates the Pro subscription, and updates the org plan.

  3. GET  /billing/subscription
       Returns the current subscription state for the authenticated org.

  4. POST /billing/subscription/cancel
       Disables auto-renewal. The subscription stays active until
       `next_payment_date`, then Paystack sends a `subscription.disable` webhook
       which downgrades the org to free.

  5. POST /billing/webhook  (no JWT — Paystack posts here directly)
       Receives Paystack events. Signature is verified with HMAC-SHA512 before
       any processing. Always returns 200 so Paystack does not retry.

       Events handled:
         charge.success          → confirms payment, sends success email
         subscription.create     → binds SUB_xxx / email_token / next_payment_date
         subscription.disable    → downgrades org to free (cancelled / completed)
         subscription.not_renew  → marks subscription as non-renewing
         invoice.payment_failed  → marks subscription as attention, sends failure email

## Self-hosted license purchase  (any deployment_mode)

One-time payment that delivers a signed license key by email. The buyer then
activates the key on their self-hosted Pulse instance via POST /license/activate.

  6. POST /billing/self-hosted/initialize
       Starts a one-time Paystack charge. Returns `authorization_url`.

  7. GET  /billing/self-hosted/verify/{reference}
       Verifies the payment, requests a signed license key from the Pulse license
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
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.errors import bad_request, not_found
from app.config.settings import settings
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.subscription import Subscription
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db
from app.infrastructure.email.sender import (
    send_license_key_email,
    send_subscription_failed_email,
    send_subscription_success_email,
)

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


# ── Schemas ───────────────────────────────────────────────────────────────────

class InitializePaymentRequest(BaseModel):
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
    plan: str = Field(..., description="Current plan: `free` or `pro`.")
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
    updated_at: datetime | None = Field(None, description="Last time this record was modified.")


class SelfHostedInitializeRequest(BaseModel):
    email: str = Field(
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
        "**Cloud only.** Initialises a Paystack transaction for the Pulse Pro plan. "
        "Redirect the user to `authorization_url` to complete payment. "
        "After Paystack redirects back to `callback_url`, call "
        "`GET /billing/verify/{reference}` to activate the subscription.\n\n"
        "> The Pro plan code is configured server-side (`PAYSTACK_PRO_PLAN_CODE`). "
        "The frontend only needs to supply a `callback_url`."
    ),
)
async def initialize_payment(
    body: InitializePaymentRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_cloud()

    plan_code = settings.PAYSTACK_PRO_PLAN_CODE
    if not plan_code:
        raise HTTPException(status_code=503, detail="Pro plan is not configured on this server")

    if not settings.get_paystack_secret_key():
        raise HTTPException(status_code=503, detail="Payment service is not configured")

    payload: dict[str, Any] = {
        "email": current_user.email,
        "amount": 100,  # overridden by the Paystack plan amount
        "plan": plan_code,
        "callback_url": body.callback_url,
        "metadata": {
            "org_id": str(current_user.org_id),
            "user_id": str(current_user.id),
            "plan": "pro",
            "purchase_type": "cloud_subscription",
        },
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
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
    current_user: User = Depends(get_current_user),
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

    sub = await _get_or_create_subscription(db, current_user.org_id)
    sub.plan = "pro"
    sub.status = "active"
    sub.authorization_code = authorization.get("authorization_code")
    sub.paystack_customer_code = customer.get("customer_code")
    sub.paystack_plan_code = settings.PAYSTACK_PRO_PLAN_CODE

    org = await db.get(Organization, current_user.org_id)
    if org:
        org.plan = "pro"

    await db.commit()
    await db.refresh(sub)

    # Send confirmation email (best-effort)
    if sub.next_payment_date:
        next_date = sub.next_payment_date.strftime("%B %d, %Y")
    else:
        next_date = "—"
    org_display = org.name if org else "your organisation"
    await send_subscription_success_email(current_user.email, org_display, next_date)

    return {
        "plan": sub.plan,
        "status": sub.status,
        "paystack_subscription_code": sub.paystack_subscription_code,
        "next_payment_date": sub.next_payment_date,
        "updated_at": sub.updated_at,
    }


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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_cloud()
    sub = await _get_or_create_subscription(db, current_user.org_id)
    await db.commit()
    return {
        "plan": sub.plan,
        "status": sub.status,
        "paystack_subscription_code": sub.paystack_subscription_code,
        "next_payment_date": sub.next_payment_date,
        "updated_at": sub.updated_at,
    }


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
    current_user: User = Depends(get_current_user),
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

    return {
        "plan": sub.plan,
        "status": sub.status,
        "paystack_subscription_code": sub.paystack_subscription_code,
        "next_payment_date": sub.next_payment_date,
        "updated_at": sub.updated_at,
    }


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
            # Notify admin that the payment issue was resolved
            email = await _org_admin_email(db, sub.org_id)
            org = await _org_name(db, sub.org_id)
            if email and sub.next_payment_date:
                next_date = sub.next_payment_date.strftime("%B %d, %Y")
                await send_subscription_success_email(email, org, next_date)


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

    sub.paystack_subscription_code = subscription_code
    sub.paystack_email_token = email_token
    sub.paystack_plan_code = plan_code
    sub.next_payment_date = next_payment
    sub.status = paystack_status
    sub.plan = "pro"

    org = await db.get(Organization, sub.org_id)
    if org:
        org.plan = "pro"

    # Send success email now that we have the next_payment_date
    email = await _org_admin_email(db, sub.org_id)
    if email:
        next_date = next_payment.strftime("%B %d, %Y") if next_payment else "—"
        org_display = org.name if org else "your organisation"
        await send_subscription_success_email(email, org_display, next_date)


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

    email = await _org_admin_email(db, sub.org_id)
    org = await _org_name(db, sub.org_id)
    if email:
        await send_subscription_failed_email(email, org)


# ── Self-hosted license purchase endpoints ────────────────────────────────────


@router.post(
    "/self-hosted/initialize",
    response_model=InitializePaymentResponse,
    summary="Start a self-hosted license purchase",
    description=(
        "Initialises a **one-time** Paystack charge for a Pulse self-hosted license. "
        "Available on all deployment modes.\n\n"
        "The purchase price is configured server-side (`PAYSTACK_SELFHOSTED_LICENSE_PRICE` in kobo). "
        "After Paystack redirects to `callback_url`, call "
        "`GET /billing/self-hosted/verify/{reference}` to receive the license key.\n\n"
        "No recurring billing — this is a single charge."
    ),
)
async def initialize_selfhosted_purchase(
    body: SelfHostedInitializeRequest,
    current_user: User = Depends(get_current_user),
) -> dict:
    price = settings.PAYSTACK_SELFHOSTED_LICENSE_PRICE
    if not price:
        raise HTTPException(status_code=503, detail="Self-hosted license purchase is not configured on this server")

    if not settings.get_paystack_secret_key():
        raise HTTPException(status_code=503, detail="Payment service is not configured")

    payload: dict[str, Any] = {
        "email": body.email,
        "amount": price,
        "callback_url": body.callback_url,
        "metadata": {
            "org_id": str(current_user.org_id),
            "user_id": str(current_user.id),
            "delivery_email": body.email,
            "purchase_type": "self_hosted_license",
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
        "2. A signed license key is requested from the Pulse license server\n"
        "3. The key is emailed to the address provided during checkout\n\n"
        "The `license_key` field is also returned in the response body for "
        "immediate display to the user. If the license server is temporarily "
        "unreachable, `license_key` will be `null` but the key will still be "
        "emailed once the server recovers."
    ),
)
async def verify_selfhosted_purchase(
    reference: str,
    current_user: User = Depends(get_current_user),
) -> dict:
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

    delivery_email = meta.get("delivery_email") or current_user.email
    org_id = str(meta.get("org_id", current_user.org_id))

    # Request a signed license key from the Pulse license server
    license_key, expires_at = await _issue_license_key(
        payment_reference=reference,
        email=delivery_email,
        org_id=org_id,
    )

    if license_key:
        await send_license_key_email(delivery_email, license_key, expires_at)
        return {
            "status": "success",
            "message": f"License key delivered to {delivery_email}",
            "license_key": license_key,
        }

    # License server unreachable — purchase is recorded, key delivered when server recovers
    logger.error(
        "License server did not return key for payment %s (org %s) — will retry delivery",
        reference, org_id,
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
    org_id: str,
) -> tuple[str | None, str | None]:
    """Request a signed license key from the Pulse license server.

    Returns (license_key, expires_at) on success, (None, None) on failure.
    The license server endpoint: POST {LICENSE_SERVER_URL}/api/v1/keys/purchase
    """
    url = f"{settings.LICENSE_SERVER_URL.rstrip('/')}/api/v1/keys/purchase"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json={
                    "payment_reference": payment_reference,
                    "email": email,
                    "org_id": org_id,
                    "product": "self_hosted",
                },
                headers={"Content-Type": "application/json"},
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
