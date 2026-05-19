"""Test connectivity for any connection row (SQL DSN, API blob, or file upload)."""

from __future__ import annotations

import csv
import os
from typing import TYPE_CHECKING, Callable

from app.infrastructure.database.connection_tester import test_connection

if TYPE_CHECKING:
    from app.infrastructure.database.models.connection import Connection


def _test_csv_upload(config: dict) -> tuple[bool, str, str | None]:
    path = (config or {}).get("upload_path")
    if not path or not isinstance(path, str):
        return False, "No uploaded file path on this connection", None
    if not os.path.isfile(path):
        return False, "Uploaded file not found on server (it may have been removed)", None
    try:
        size = os.path.getsize(path)
        if size == 0:
            return False, "Uploaded file is empty", None
        max_bytes = 50 * 1024 * 1024
        if size > max_bytes:
            return False, "File exceeds 50 MB limit", None
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            sample = f.read(8192)
        if not sample.strip():
            return False, "Uploaded file has no readable content", None
        try:
            rows = csv.reader(sample.splitlines())
            header = next(rows, None)
            if not header:
                return False, "CSV has no header row", None
        except csv.Error as exc:
            return False, f"CSV parse error: {exc}", None
        name = (config or {}).get("original_filename") or os.path.basename(path)
        return True, f"File '{name}' is readable ({size:,} bytes)", None
    except OSError as exc:
        return False, f"Cannot read uploaded file: {exc}", None


async def test_connection_record(
    conn: Connection,
    *,
    decrypt_dsn: Callable[[str], str],
) -> tuple[bool, str, str | None]:
    """Run an appropriate connectivity check for any connector type."""
    if conn.encrypted_dsn:
        try:
            dsn = decrypt_dsn(conn.encrypted_dsn)
        except Exception as exc:
            return False, f"Could not decrypt connection credentials: {exc}", None
        return await test_connection(dsn, sslmode=conn.sslmode)

    connector = (conn.connector_type or "").lower()
    if connector == "csv":
        return _test_csv_upload(conn.config or {})

    return (
        False,
        "Connection has no stored credentials yet. Save or re-create the connection, then test again.",
        None,
    )
