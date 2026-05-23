"""Attach org_id from JWT to logging context for structured log streaming."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.api.auth.jwt_utils import decode_access_token
from app.infrastructure.logging.context import (
    reset_org_token,
    reset_request_token,
    set_log_org_id,
    set_log_request_id,
)


class OrgContextMiddleware(BaseHTTPMiddleware):
    SKIP_PATHS = {"/health", "/healthz", "/ready", "/metrics", "/favicon.ico"}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)

        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:12]
        req_token = set_log_request_id(request_id)
        org_token = None

        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            try:
                payload = decode_access_token(token)
                org_id = payload.get("org_id")
                if org_id:
                    org_token = set_log_org_id(str(org_id))
            except Exception:
                pass

        try:
            return await call_next(request)
        finally:
            if org_token is not None:
                reset_org_token(org_token)
            reset_request_token(req_token)
