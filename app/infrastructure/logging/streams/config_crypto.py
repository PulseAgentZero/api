"""Encrypt/decrypt sensitive keys inside log stream JSON config.

The stored form keeps secrets only under ``*_encrypted`` keys. The redacted form
that ships back to clients NEVER contains an encrypted blob — it exposes a
``hmac_secret_set`` boolean instead, so a client echoing the redacted payload
back on a PATCH cannot accidentally overwrite the stored secret with a sentinel.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.infrastructure.crypto import decrypt_secret, encrypt_secret


def encrypt_stream_config(
    config: dict[str, Any], *, previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Convert a client-supplied config into the encrypted-at-rest form.

    A plaintext ``hmac_secret`` is encrypted into ``hmac_secret_encrypted``.
    Any ``hmac_secret_encrypted`` value coming from the client is discarded
    (redacted payloads round-trip safely). When ``previous`` is supplied and
    no new ``hmac_secret`` is provided, the previously stored encrypted
    secret is preserved.
    """
    out = deepcopy(config)
    secret = out.pop("hmac_secret", None)
    out.pop("hmac_secret_encrypted", None)
    out.pop("hmac_secret_set", None)
    if secret:
        out["hmac_secret_encrypted"] = encrypt_secret(str(secret))
    elif previous and previous.get("hmac_secret_encrypted"):
        out["hmac_secret_encrypted"] = previous["hmac_secret_encrypted"]
    return out


def decrypt_stream_config(config: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(config)
    enc = out.pop("hmac_secret_encrypted", None)
    if enc:
        try:
            out["hmac_secret"] = decrypt_secret(str(enc))
        except Exception:
            out.pop("hmac_secret", None)
    return out


def redact_stream_config(config: dict[str, Any]) -> dict[str, Any]:
    """Strip every secret form and emit a `_set` boolean instead."""
    out = deepcopy(config)
    had_secret = bool(out.pop("hmac_secret_encrypted", None) or out.pop("hmac_secret", None))
    out["hmac_secret_set"] = had_secret
    return out
