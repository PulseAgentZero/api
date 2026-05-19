"""Resolve stored connection secrets to values usable for tests and SQL engines."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from app.infrastructure.connectors.payload import parse_pulse_api_payload


def _parse_bigquery_url(connection_url: str) -> tuple[str, str | None]:
    """Return (project_id, dataset_id) from bigquery://project/dataset."""
    parsed = urlparse(connection_url.strip())
    path = (parsed.path or "").strip("/")
    parts = [p for p in path.split("/") if p]
    project = parts[0] if parts else (parsed.hostname or "")
    dataset = parts[1] if len(parts) > 1 else None
    if not project:
        raise ValueError("BigQuery connection URL must include a project id (bigquery://project/dataset)")
    return project, dataset


def build_bigquery_stored_secret(connection_url: str, service_account_json: str | None) -> str:
    """Return plaintext stored in encrypted_dsn for BigQuery (URL or API blob)."""
    url = connection_url.strip()
    sa = (service_account_json or "").strip()
    if sa:
        from app.infrastructure.connectors.payload import pulse_api_blob

        project, dataset = _parse_bigquery_url(url)
        return pulse_api_blob(
            "bigquery",
            connection_url=url,
            project_id=project,
            dataset_id=dataset or "",
            service_account_json=sa,
        )
    return url


def google_sheets_auth_mode(payload: dict[str, Any]) -> str:
    if payload.get("service_account_json"):
        return "service_account"
    return "api_key"
