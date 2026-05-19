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


def auth_rl_ip(ip: str, action: str) -> str:
    return f"auth_rl:ip:{ip}:{action}"


def auth_rl_email(email: str, action: str) -> str:
    return f"auth_rl:email:{email.strip().lower()}:{action}"


def invite_rl_org(org_id) -> str:
    return f"invite_rl:org:{org_id}"


def invite_rl_invitation(invitation_id) -> str:
    return f"invite_rl:inv:{invitation_id}"


def email_verify_rate(user_id) -> str:
    """Rate limit key for resend-verification — TTL 60s."""
    return f"email_verify_rate:{user_id}"


def oauth_google_state(state: str) -> str:
    return f"oauth_google_state:{state}"


def oauth_google_link_pending(token: str) -> str:
    return f"oauth_google_link_pending:{token}"


def oauth_google_signup_pending(token: str) -> str:
    return f"oauth_google_signup_pending:{token}"


def studio_embed(token: str) -> str:
    return f"studio:embed:{token}"


def studio_budget(org_id: str, date_str: str) -> str:
    return f"studio:budget:{org_id}:{date_str}"


def studio_run_result(run_id: str) -> str:
    """Redis key for storing async studio query run results (TTL 1 hour)."""
    return f"studio:run_result:{run_id}"


def studio_public_rl(ip: str) -> str:
    return f"studio:public_rl:{ip}"


def studio_query_cache(org_id: str, query_hash: str) -> str:
    return f"studio:qcache:{org_id}:{query_hash}"


def user_sessions_pattern(user_id) -> str:
    """Glob pattern to match all refresh tokens for a user.
    NOTE: refresh tokens are stored as refresh:{sha256(raw)} — we can't
    reverse-lookup by user_id without a secondary index. This key tracks
    them explicitly. See tokens.py set_refresh_token for how it's written.
    """
    return f"user_sessions:{user_id}:*"
