"""Standard API error envelope for BACKEND_ROUTES.md."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status


def error_payload(
    code: str,
    message: str,
    *,
    fields: dict[str, str] | None = None,
    feature: str | None = None,
    upgrade_url: str | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if fields:
        err["fields"] = fields
    if feature is not None:
        err["feature"] = feature
    if upgrade_url is not None:
        err["upgrade_url"] = upgrade_url
    return {"error": err}


class PulseHTTPException(HTTPException):
    """HTTPException with structured detail for the global handler."""

    def __init__(
        self,
        status_code: int,
        *,
        code: str,
        message: str,
        fields: dict[str, str] | None = None,
        feature: str | None = None,
        upgrade_url: str | None = None,
    ) -> None:
        detail: dict[str, Any] = {"code": code, "message": message}
        if fields:
            detail["fields"] = fields
        if feature is not None:
            detail["feature"] = feature
        if upgrade_url is not None:
            detail["upgrade_url"] = upgrade_url
        super().__init__(status_code=status_code, detail=detail)


def bad_request(code: str, message: str, **kwargs: Any) -> PulseHTTPException:
    return PulseHTTPException(status.HTTP_400_BAD_REQUEST, code=code, message=message, **kwargs)


def unauthorized(code: str, message: str, **kwargs: Any) -> PulseHTTPException:
    return PulseHTTPException(status.HTTP_401_UNAUTHORIZED, code=code, message=message, **kwargs)


def forbidden(code: str, message: str, **kwargs: Any) -> PulseHTTPException:
    return PulseHTTPException(status.HTTP_403_FORBIDDEN, code=code, message=message, **kwargs)


def not_found(message: str = "Resource not found") -> PulseHTTPException:
    return PulseHTTPException(
        status.HTTP_404_NOT_FOUND, code="NOT_FOUND", message=message
    )


def conflict(code: str, message: str, **kwargs: Any) -> PulseHTTPException:
    return PulseHTTPException(status.HTTP_409_CONFLICT, code=code, message=message, **kwargs)


def plan_locked(feature: str, message: str, upgrade_url: str | None = None) -> PulseHTTPException:
    return PulseHTTPException(
        status.HTTP_402_PAYMENT_REQUIRED,
        code="FEATURE_LOCKED",
        message=message,
        feature=feature,
        upgrade_url=upgrade_url or "https://entivia.online/pricing",
    )


def plan_limit(message: str) -> PulseHTTPException:
    return PulseHTTPException(
        status.HTTP_402_PAYMENT_REQUIRED,
        code="PLAN_LIMIT_REACHED",
        message=message,
    )


def rate_limited(message: str) -> PulseHTTPException:
    return PulseHTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        code="RATE_LIMITED",
        message=message,
    )


def payload_too_large(message: str) -> PulseHTTPException:
    return PulseHTTPException(
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        code="PAYLOAD_TOO_LARGE",
        message=message,
    )


def service_unavailable(code: str, message: str, **kwargs: Any) -> PulseHTTPException:
    return PulseHTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE, code=code, message=message, **kwargs
    )


def validation_error(message: str, fields: dict[str, str]) -> PulseHTTPException:
    return PulseHTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        code="VALIDATION_ERROR",
        message=message,
        fields=fields,
    )
