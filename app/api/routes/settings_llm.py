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


def _key_state(*, db_value: object, env_present: bool) -> dict[str, object]:
    """Return ``{configured, source}`` describing where a provider key comes from.

    ``source`` is ``"db"`` when the admin stored a key via PUT, ``"env"`` when
    only the process-level env var is set, and ``None`` when neither is
    configured. ``configured`` is true whenever the agent stack can call the
    provider at runtime.
    """
    has_db = bool(db_value)
    if has_db:
        return {"configured": True, "source": "db"}
    if env_present:
        return {"configured": True, "source": "env"}
    return {"configured": False, "source": None}


@router.get("/llm-keys")
async def get_llm_keys(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, dict[str, object]]:
    """**Self-hosted only.** Returns 404 on cloud deployments.

    Report each provider's runtime status. For every provider the response
    has ``{"configured": bool, "source": "env" | "db" | null}`` so the UI can:

    - Show "Configured via environment variable" and disable the input when
      ``source == "env"`` — the operator already provisioned the key in
      ``.env`` and it should not be overwritten from the dashboard.
    - Show "Saved" when ``source == "db"`` — overrides the env var for this org.
    - Allow the admin to paste a new key when ``configured == false``.

    Keys themselves are never returned — only presence and source.
    """
    _require_self_hosted()
    r = await db.execute(select(LlmKeyStore).where(LlmKeyStore.org_id == current_user.org_id))
    row = r.scalar_one_or_none()
    db_anthropic: object = None
    db_groq: object = None
    if row is not None:
        try:
            data = json.loads(decrypt_dsn(row.keys))
            db_anthropic = data.get("anthropic")
            db_groq = data.get("groq")
        except Exception:
            db_anthropic = None
            db_groq = None

    anthropic_env = bool(settings.get_anthropic_api_key())
    groq_env = bool(settings.is_groq_configured())
    return {
        "anthropic": _key_state(db_value=db_anthropic, env_present=anthropic_env),
        "groq": _key_state(db_value=db_groq, env_present=groq_env),
    }


@router.put("/llm-keys")
async def put_llm_keys(
    body: LlmKeysUpdate,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, dict[str, object]]:
    """**Self-hosted only.** Returns 404 on cloud deployments.

    Store or update LLM API keys for this org. Keys are encrypted at rest with Fernet.
    Pass `null` or an empty string for a provider to remove its stored key (the instance
    will fall back to the ``ANTHROPIC_API_KEY`` / ``GROQ_API_KEY`` environment variables).
    Returns the updated presence + source descriptors — never the raw keys.
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
        "anthropic": _key_state(
            db_value=payload.get("anthropic"),
            env_present=bool(settings.get_anthropic_api_key()),
        ),
        "groq": _key_state(
            db_value=payload.get("groq"),
            env_present=bool(settings.is_groq_configured()),
        ),
    }
