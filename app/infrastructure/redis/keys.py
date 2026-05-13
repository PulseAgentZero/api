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
