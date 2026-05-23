"""Recover and re-deliver a self-hosted license key for a completed payment.

Use when the Paystack webhook fired but the email never went out (e.g. the
webhook handler crashed mid-flight, the customer closed the verify tab, or
the license server was briefly unreachable).

The script:
  1. Verifies the Paystack transaction by reference
  2. Confirms the metadata says ``purchase_type=self_hosted_license``
  3. Calls the Entivia license server (idempotent — returns the existing
     key if one was already issued for this payment)
  4. Emails the key to the original delivery address

Run from the project root::

    python -m scripts.recover_self_hosted_license <PAYSTACK_REFERENCE>

or pass ``--email <addr>`` to override the delivery address (rare; only use
when the customer email on file is wrong).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx

from app.api.routes.billing import (
    PAYSTACK_BASE,
    _issue_and_deliver_self_hosted_license,
    _paystack_headers,
)
from app.config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def recover(reference: str, override_email: str | None) -> int:
    if not settings.get_paystack_secret_key():
        logger.error("PAYSTACK_SECRET_KEY is not configured")
        return 2

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{PAYSTACK_BASE}/transaction/verify/{reference}",
            headers=_paystack_headers(),
        )

    if resp.status_code != 200:
        logger.error("Paystack verify failed: %s — %s", resp.status_code, resp.text[:300])
        return 3

    tx = resp.json().get("data", {})
    if tx.get("status") != "success":
        logger.error("Transaction is not successful: status=%s", tx.get("status"))
        return 4

    meta = tx.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("purchase_type") and meta.get("purchase_type") != "self_hosted_license":
        logger.error("Payment is not a self-hosted license purchase: %s", meta.get("purchase_type"))
        return 5

    delivery_email = (override_email or "").strip().lower()
    if not delivery_email:
        delivery_email = (
            (meta.get("delivery_email") if isinstance(meta, dict) else "") or ""
        ).strip().lower()
    if not delivery_email:
        delivery_email = (tx.get("customer") or {}).get("email", "").strip().lower()
    if not delivery_email:
        logger.error("No delivery email could be determined for ref=%s", reference)
        return 6

    purchaser_org_id: str | None = None
    if isinstance(meta, dict):
        raw_org = meta.get("org_id")
        if raw_org:
            purchaser_org_id = str(raw_org)

    license_key, expires_at = await _issue_and_deliver_self_hosted_license(
        payment_reference=reference,
        delivery_email=delivery_email,
        purchaser_org_id=purchaser_org_id,
    )

    if not license_key:
        logger.error(
            "License server did not return a key — check LICENSE_SERVER_URL "
            "(%s) and LICENSE_SERVER_API_KEY",
            settings.LICENSE_SERVER_URL,
        )
        return 7

    print()
    print("Recovered license for", delivery_email)
    print("Reference:", reference)
    print("Expires at:", expires_at or "never")
    print()
    print("License key:")
    print(license_key)
    print()
    print(
        "Email has been queued — check the customer's inbox. The license server "
        "is idempotent, so re-running this script with the same reference is safe."
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", help="Paystack transaction reference")
    parser.add_argument(
        "--email",
        dest="email",
        default=None,
        help="Override delivery email (only when the on-file address is wrong)",
    )
    args = parser.parse_args()

    exit_code = asyncio.run(recover(args.reference, args.email))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
