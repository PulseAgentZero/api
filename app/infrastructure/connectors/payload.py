"""Encrypted credential blobs for non-DSN connectors (JSON inside Fernet)."""

from __future__ import annotations

import json
from typing import Any


def pulse_api_blob(kind: str, **secrets: Any) -> str:
    """Return plaintext JSON to encrypt with ``encrypt_dsn``."""
    return json.dumps({"pulse_connector": True, "kind": kind, **secrets})


def parse_pulse_api_payload(decrypted: str) -> dict[str, Any] | None:
    s = decrypted.strip()
    if not s.startswith("{"):
        return None
    try:
        d = json.loads(s)
    except json.JSONDecodeError:
        return None
    if isinstance(d, dict) and d.get("pulse_connector") is True and "kind" in d:
        return d
    return None
