"""Self-hosted LLM key storage (BACKEND_ROUTES §17)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.errors import not_found
from app.config.settings import settings
from app.infrastructure.crypto import decrypt_dsn, encrypt_dsn
from app.infrastructure.database.models.llm_key_store import LlmKeyStore
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/settings", tags=["Settings"])


def _require_self_hosted() -> None:
    if settings.DEPLOYMENT_MODE != "self_hosted":
        raise not_found("Not found")


class LlmKeysUpdate(BaseModel):
    anthropic: str | None = None
    groq: str | None = None


@router.get("/llm-keys")
async def get_llm_keys(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    """**Self-hosted only.** Returns 404 on cloud deployments.

    Return whether each LLM provider key is configured. Returns `{ "anthropic": bool, "groq": bool }`.
    Keys themselves are never returned — only presence is indicated. Falls back to env-var detection
    if no org-level key has been stored via `PUT /settings/llm-keys`.
    """
    _require_self_hosted()
    r = await db.execute(select(LlmKeyStore).where(LlmKeyStore.org_id == current_user.org_id))
    row = r.scalar_one_or_none()
    if row is None:
        return {"anthropic": bool(settings.get_anthropic_api_key()), "groq": bool(settings.is_groq_configured())}
    try:
        data = json.loads(decrypt_dsn(row.keys))
    except Exception:
        return {"anthropic": False, "groq": False}
    return {
        "anthropic": bool(data.get("anthropic")),
        "groq": bool(data.get("groq")),
    }


@router.put("/llm-keys")
async def put_llm_keys(
    body: LlmKeysUpdate,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    """**Self-hosted only.** Returns 404 on cloud deployments.

    Store or update LLM API keys for this org. Keys are encrypted at rest with Fernet.
    Pass `null` or an empty string for a provider to remove its stored key (the instance
    will fall back to the `ANTHROPIC_API_KEY` / `GROQ_API_KEY` environment variables).
    Returns the updated presence flags — never the raw keys.
    """
    _require_self_hosted()
    r = await db.execute(select(LlmKeyStore).where(LlmKeyStore.org_id == current_user.org_id))
    row = r.scalar_one_or_none()
    payload: dict[str, Any] = {}
    if row:
        try:
            payload = json.loads(decrypt_dsn(row.keys))
        except Exception:
            payload = {}
    if body.anthropic is not None:
        if body.anthropic == "":
            payload.pop("anthropic", None)
        else:
            payload["anthropic"] = body.anthropic
    if body.groq is not None:
        if body.groq == "":
            payload.pop("groq", None)
        else:
            payload["groq"] = body.groq
    enc = encrypt_dsn(json.dumps(payload))
    if row:
        row.keys = enc
    else:
        db.add(LlmKeyStore(org_id=current_user.org_id, keys=enc))
    await db.commit()
    return {
        "anthropic": bool(payload.get("anthropic")),
        "groq": bool(payload.get("groq")),
    }
