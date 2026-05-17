"""Email sending via Resend. Gracefully no-ops when RESEND_API_KEY is not set."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config.settings import settings

logger = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"
_TEMPLATES_DIR = Path(__file__).parent / "templates"

_jinja = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _render(template_name: str, **context) -> str:
    context.setdefault("year", datetime.utcnow().year)
    return _jinja.get_template(template_name).render(**context)


async def _send(*, to: str, subject: str, html: str) -> bool:
    api_key = settings.get_resend_api_key()
    if not api_key:
        logger.warning("RESEND_API_KEY not set — email to %s skipped: %s", to, subject)
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _RESEND_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "from": settings.DEFAULT_FROM_EMAIL,
                    "to": [to],
                    "subject": subject,
                    "html": html,
                },
            )
            resp.raise_for_status()
            logger.info("Email sent to %s — %s", to, subject)
            return True
    except Exception:
        logger.exception("Failed to send email to %s — %s", to, subject)
        return False


async def send_verification_email(to: str, token: str) -> None:
    link = f"{settings.FRONTEND_URL.rstrip('/')}/auth/verify-email?token={token}"
    html = _render("verify_email.html", subject="Verify your email address", link=link)
    await _send(to=to, subject="Verify your Pulse email address", html=html)


async def send_password_reset_email(to: str, token: str) -> None:
    link = f"{settings.FRONTEND_URL.rstrip('/')}/auth/reset-password?token={token}"
    html = _render("reset_password.html", subject="Reset your password", link=link, email=to)
    await _send(to=to, subject="Reset your Pulse password", html=html)


async def send_invitation_email(
    to: str,
    token: str,
    invited_by: str,
    org_name: str,
    role: str = "member",
) -> None:
    link = f"{settings.FRONTEND_URL.rstrip('/')}/auth/accept-invite?token={token}"
    html = _render(
        "invitation.html",
        subject=f"You've been invited to join {org_name}",
        link=link,
        invited_by=invited_by,
        org_name=org_name,
        role=role,
    )
    await _send(to=to, subject=f"You've been invited to join {org_name} on Pulse", html=html)


async def send_subscription_success_email(
    to: str,
    org_name: str,
    next_payment_date: str,
) -> None:
    dashboard_url = f"{settings.FRONTEND_URL.rstrip('/')}/dashboard"
    html = _render(
        "subscription_success.html",
        subject="You're now on Pulse Pro",
        org_name=org_name,
        next_payment_date=next_payment_date,
        dashboard_url=dashboard_url,
    )
    await _send(to=to, subject="You're now on Pulse Pro 🎉", html=html)


async def send_subscription_failed_email(to: str, org_name: str) -> None:
    manage_url = f"{settings.FRONTEND_URL.rstrip('/')}/settings/billing"
    html = _render(
        "subscription_failed.html",
        subject="Payment failed for your Pro subscription",
        org_name=org_name,
        manage_url=manage_url,
    )
    await _send(to=to, subject="Action required: Pulse Pro payment failed", html=html)


async def send_subscription_renewal_reminder_email(
    to: str,
    org_name: str,
    renewal_date: str,
) -> None:
    manage_url = f"{settings.FRONTEND_URL.rstrip('/')}/settings/billing"
    html = _render(
        "subscription_renewal_reminder.html",
        subject="Your Pulse Pro subscription renews tomorrow",
        org_name=org_name,
        renewal_date=renewal_date,
        manage_url=manage_url,
    )
    await _send(to=to, subject="Reminder: Your Pulse Pro subscription renews tomorrow", html=html)


async def send_license_key_email(
    to: str,
    license_key: str,
    expires_at: str | None = None,
) -> None:
    activate_docs_url = f"{settings.FRONTEND_URL.rstrip('/')}/docs/license-activation"
    html = _render(
        "license_purchase_success.html",
        subject="Your Pulse license key is ready",
        license_key=license_key,
        expires_at=expires_at,
        activate_docs_url=activate_docs_url,
    )
    await _send(to=to, subject="Your Pulse self-hosted license key", html=html)
