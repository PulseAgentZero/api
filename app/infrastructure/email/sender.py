"""Multi-backend email sending.

Supported backends (set EMAIL_BACKEND env var):
  resend  — Resend API (https://resend.com) — default when RESEND_API_KEY is set
  smtp    — Any SMTP server (Gmail, SendGrid, Mailgun, Postmark, self-hosted, etc.)

Auto-detection: uses Resend if RESEND_API_KEY is set, otherwise SMTP if
SMTP_HOST is set. If neither is configured, emails are logged and silently skipped.
"""

from __future__ import annotations

import logging
import os
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


# ── Backend selection ─────────────────────────────────────────────────────────

def _get_backend() -> str:
    backend = os.getenv("EMAIL_BACKEND", "").strip().lower()
    if backend in ("resend", "smtp"):
        return backend
    # Auto-detect
    if settings.get_resend_api_key():
        return "resend"
    if os.getenv("SMTP_HOST"):
        return "smtp"
    return "none"


# ── Resend backend ────────────────────────────────────────────────────────────

async def _send_via_resend(*, to: str, subject: str, html: str) -> bool:
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
            logger.info("Email sent via Resend to %s — %s", to, subject)
            return True
    except Exception:
        logger.exception("Resend: failed to send email to %s — %s", to, subject)
        return False


# ── SMTP backend ──────────────────────────────────────────────────────────────

async def _send_via_smtp(*, to: str, subject: str, html: str) -> bool:
    """Send via SMTP. Works with any SMTP server.

    Environment variables:
      SMTP_HOST         — SMTP server hostname (required)
      SMTP_PORT         — port (default 587 for STARTTLS, 465 for SSL, 25 for plain)
      SMTP_USERNAME     — login username (required unless server allows anonymous)
      SMTP_PASSWORD     — login password
      SMTP_USE_TLS      — "true" for SSL/TLS on port 465 (default false)
      SMTP_USE_STARTTLS — "true" for STARTTLS on port 587 (default true when port ≠ 465)
      DEFAULT_FROM_EMAIL — sender address
    """
    import asyncio
    import smtplib
    import ssl
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    host = os.getenv("SMTP_HOST", "")
    if not host:
        logger.warning("SMTP_HOST not set — email to %s skipped: %s", to, subject)
        return False

    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    use_tls = os.getenv("SMTP_USE_TLS", "false").lower() == "true"
    use_starttls = os.getenv("SMTP_USE_STARTTLS", "true" if port != 465 else "false").lower() == "true"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.DEFAULT_FROM_EMAIL
    msg["To"] = to
    msg.attach(MIMEText(html, "html"))

    def _send_sync() -> None:
        if use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context) as server:
                if username:
                    server.login(username, password)
                server.sendmail(settings.DEFAULT_FROM_EMAIL, [to], msg.as_string())
        else:
            with smtplib.SMTP(host, port) as server:
                if use_starttls:
                    server.starttls(context=ssl.create_default_context())
                if username:
                    server.login(username, password)
                server.sendmail(settings.DEFAULT_FROM_EMAIL, [to], msg.as_string())

    try:
        await asyncio.to_thread(_send_sync)
        logger.info("Email sent via SMTP to %s — %s", to, subject)
        return True
    except Exception:
        logger.exception("SMTP: failed to send email to %s — %s", to, subject)
        return False


# ── Dispatcher ────────────────────────────────────────────────────────────────

async def _send(*, to: str, subject: str, html: str) -> bool:
    backend = _get_backend()
    if backend == "resend":
        return await _send_via_resend(to=to, subject=subject, html=html)
    if backend == "smtp":
        return await _send_via_smtp(to=to, subject=subject, html=html)
    logger.warning(
        "No email backend configured — email to %s skipped: %s. "
        "Set RESEND_API_KEY or SMTP_HOST to enable email.",
        to, subject,
    )
    return False


# ── Public send helpers ───────────────────────────────────────────────────────

async def send_verification_email(to: str, token: str) -> None:
    link = f"{settings.FRONTEND_URL.rstrip('/')}/auth/verify-email?token={token}"
    html = _render("verify_email.html", subject="Verify your email address", link=link)
    await _send(to=to, subject="Verify your Pulse email address", html=html)


async def send_password_reset_email(to: str, token: str) -> None:
    link = f"{settings.FRONTEND_URL.rstrip('/')}/auth/reset-password?token={token}"
    html = _render("reset_password.html", subject="Reset your password", link=link, email=to)
    await _send(to=to, subject="Reset your Pulse password", html=html)


async def send_welcome_email(
    to: str,
    *,
    full_name: str,
    org_name: str,
) -> None:
    dashboard_url = f"{settings.FRONTEND_URL.rstrip('/')}/dashboard"
    display_name = (full_name or "").strip() or "there"
    html = _render(
        "welcome.html",
        subject=f"Welcome to Pulse — {org_name}",
        full_name=display_name,
        org_name=org_name,
        dashboard_url=dashboard_url,
    )
    await _send(to=to, subject=f"Welcome to Pulse — {org_name}", html=html)


async def send_invitation_email(to: str, token: str, invited_by: str, org_name: str, role: str = "member") -> None:
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


async def send_subscription_success_email(to: str, org_name: str, next_payment_date: str) -> None:
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


async def send_subscription_renewal_reminder_email(to: str, org_name: str, renewal_date: str) -> None:
    manage_url = f"{settings.FRONTEND_URL.rstrip('/')}/settings/billing"
    html = _render(
        "subscription_renewal_reminder.html",
        subject="Your Pulse Pro subscription renews tomorrow",
        org_name=org_name,
        renewal_date=renewal_date,
        manage_url=manage_url,
    )
    await _send(to=to, subject="Reminder: Your Pulse Pro subscription renews tomorrow", html=html)


async def send_license_key_email(to: str, license_key: str, expires_at: str | None = None) -> None:
    activate_docs_url = f"{settings.FRONTEND_URL.rstrip('/')}/docs/license-activation"
    html = _render(
        "license_purchase_success.html",
        subject="Your Pulse license key is ready",
        license_key=license_key,
        expires_at=expires_at,
        activate_docs_url=activate_docs_url,
    )
    await _send(to=to, subject="Your Pulse self-hosted license key", html=html)
