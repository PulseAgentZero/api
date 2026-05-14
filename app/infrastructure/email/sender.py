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
