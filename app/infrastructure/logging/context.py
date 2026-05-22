"""Request-scoped logging context (org_id, request_id) via contextvars."""

from __future__ import annotations

import contextvars
from uuid import UUID

_log_org_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("log_org_id", default=None)
_log_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "log_request_id", default=None
)


def set_log_org_id(org_id: UUID | str | None) -> contextvars.Token:
    return _log_org_id.set(str(org_id) if org_id is not None else None)


def get_log_org_id() -> str | None:
    return _log_org_id.get()


def set_log_request_id(request_id: str | None) -> contextvars.Token:
    return _log_request_id.set(request_id)


def get_log_request_id() -> str | None:
    return _log_request_id.get()


def reset_org_token(token: contextvars.Token) -> None:
    _log_org_id.reset(token)


def reset_request_token(token: contextvars.Token) -> None:
    _log_request_id.reset(token)
