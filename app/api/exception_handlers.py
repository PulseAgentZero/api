"""Shared FastAPI exception handlers (internal API, public API, agent service)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.errors import error_payload

logger = logging.getLogger(__name__)


def attach_exception_handlers(app: FastAPI) -> None:
    """Attach the standard error envelope handlers to any FastAPI instance."""

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields: dict[str, str] = {}
        for err in exc.errors():
            loc_parts = [str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path")]
            key = ".".join(loc_parts) if loc_parts else "request"
            fields[key] = err.get("msg", "Invalid value")
        logger.warning(
            "422 validation error %s %s fields=%s",
            request.method,
            request.url.path,
            fields,
        )
        return JSONResponse(
            status_code=422,
            content=error_payload("VALIDATION_ERROR", "Request validation failed", fields=fields),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error(
                "%s %s -> %s %s",
                request.method,
                request.url.path,
                exc.status_code,
                exc.detail,
            )
        elif exc.status_code not in (401, 404):
            logger.warning(
                "%s %s -> %s %s",
                request.method,
                request.url.path,
                exc.status_code,
                exc.detail,
            )

        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail and "message" in detail:
            err = {str(k): detail[k] for k in detail}
            return JSONResponse(status_code=exc.status_code, content={"error": err})
        msg = detail if isinstance(detail, str) else str(detail)
        code = "TOKEN_EXPIRED" if exc.status_code == 401 else "BAD_REQUEST"
        return JSONResponse(status_code=exc.status_code, content=error_payload(code, msg))

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "500 unhandled %s %s — %s: %s",
            request.method,
            request.url.path,
            type(exc).__name__,
            exc,
        )
        return JSONResponse(
            status_code=500,
            content=error_payload("INTERNAL_ERROR", "Internal server error"),
        )
