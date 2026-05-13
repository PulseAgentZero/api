from __future__ import annotations

import hashlib


def email_verify(token: str) -> str:
    return f"email_verify:{token}"


def pw_reset(token: str) -> str:
    return f"pw_reset:{token}"


def refresh(raw_token: str) -> str:
    h = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    return f"refresh:{h}"


def pipeline_lock(org_id: str) -> str:
    return f"pipeline:lock:{org_id}"


def pipeline_progress(run_id: str) -> str:
    return f"pipeline:progress:{run_id}"


def pipeline_cancel(run_id: str) -> str:
    return f"pipeline:cancel:{run_id}"


def rate_limit(ip: str, endpoint: str) -> str:
    return f"rate_limit:{ip}:{endpoint}"


def email_verify_rate(user_id) -> str:
    """Rate limit key for resend-verification — TTL 60s."""
    return f"email_verify_rate:{user_id}"


def user_sessions_pattern(user_id) -> str:
    """Glob pattern to match all refresh tokens for a user.
    NOTE: refresh tokens are stored as refresh:{sha256(raw)} — we can't
    reverse-lookup by user_id without a secondary index. This key tracks
    them explicitly. See tokens.py set_refresh_token for how it's written.
    """
    return f"user_sessions:{user_id}:*"
