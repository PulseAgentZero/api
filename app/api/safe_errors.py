"""Sanitize internal errors before returning them to API clients."""

from __future__ import annotations

import logging
import re

from app.api.errors import PulseHTTPException, bad_request

logger = logging.getLogger(__name__)

PUBLIC_MESSAGES: dict[str, str] = {
    "BAD_REQUEST": "The request could not be processed. Check your input and try again.",
    "CLIENT_DB_ERROR": "Could not access the data source. Check your connection settings.",
    "STORAGE_NOT_CONFIGURED": "File storage is not configured on this server.",
    "CONNECTION_ERROR": "Could not connect to the data source.",
    "TEST_CONNECTION_FAILED": "Connection test failed. Check your credentials and network settings.",
    "OAUTH_FAILED": "Sign-in with Google failed. Please try again.",
}


def public_message(code: str, fallback: str | None = None) -> str:
    return PUBLIC_MESSAGES.get(code) or fallback or PUBLIC_MESSAGES["BAD_REQUEST"]


def log_and_bad_request(
    code: str,
    exc: BaseException,
    *,
    user_message: str | None = None,
) -> PulseHTTPException:
    logger.exception("API error %s: %s", code, exc)
    return bad_request(code, user_message or public_message(code))


def sanitize_connection_test_message(message: str) -> str:
    """Return a generic message for API responses; keep raw message in DB only."""
    if not message:
        return public_message("TEST_CONNECTION_FAILED")
    lowered = message.lower()
    if any(
        kw in lowered
        for kw in (
            "password",
            "authentication",
            "timeout",
            "refused",
            "could not connect",
            "connection refused",
            "network",
            "ssl",
            "certificate",
        )
    ):
        return "Could not connect. Verify host, port, credentials, and firewall rules."
    if "not found" in lowered or "does not exist" in lowered:
        return "Database or resource not found. Check the database name."
    return public_message("TEST_CONNECTION_FAILED")


_DRIVER_HOST_RE = re.compile(
    r"(?:host|server|address)\s*[=:]\s*['\"]?[\w.\-]+['\"]?",
    re.IGNORECASE,
)


def scrub_internal_text(text: str) -> str:
    """Remove obvious host/path fragments from a string before logging to clients."""
    return _DRIVER_HOST_RE.sub("[redacted]", text)
