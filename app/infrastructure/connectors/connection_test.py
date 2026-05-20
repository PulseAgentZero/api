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
        from app.config.constants import FILE_UPLOAD_MAX_BYTES

        if size > FILE_UPLOAD_MAX_BYTES:
            return False, "File exceeds 500 MB limit", None
        filename = (config or {}).get("original_filename") or os.path.basename(path)
        is_tsv = filename.lower().endswith(".tsv")
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            sample = f.read(8192)
        if not sample.strip():
            return False, "Uploaded file has no readable content", None
        try:
            rows = csv.reader(sample.splitlines(), delimiter="\t" if is_tsv else ",")
            header = next(rows, None)
            if not header:
                return False, "File has no header row", None
        except csv.Error as exc:
            return False, f"Parse error: {exc}", None
        name = filename
        return True, f"File '{name}' is readable ({size:,} bytes)", None
    except OSError as exc:
        return False, f"Cannot read uploaded file: {exc}", None


def _test_excel_upload(config: dict) -> tuple[bool, str, str | None]:
    path = (config or {}).get("upload_path")
    if not path or not isinstance(path, str):
        return False, "No uploaded file path on this connection", None
    if not os.path.isfile(path):
        return False, "Uploaded file not found on server (it may have been removed)", None

    try:
        import pandas as pd
        from app.services.studio_file_source_service import _excel_engine_for_path

        filename = (config or {}).get("original_filename") or path
        engine = _excel_engine_for_path(path, filename)
        workbook = pd.ExcelFile(path, engine=engine)
        sheet_count = len(workbook.sheet_names)
        if sheet_count == 0:
            return False, "Workbook has no sheets", None
        size = os.path.getsize(path)
        name = filename
        return (
            True,
            f"Workbook '{name}' is readable ({size:,} bytes, {sheet_count} sheet"
            f"{'s' if sheet_count != 1 else ''})",
            None,
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "password" in msg or "encrypted" in msg:
            return (
                False,
                "Workbook is password-protected. Save an unprotected copy and re-upload.",
                None,
            )
        return False, f"Could not read Excel file: {exc}", None


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
    if connector == "excel":
        return _test_excel_upload(conn.config or {})

    return (
        False,
        "Connection has no stored credentials yet. Save or re-create the connection, then test again.",
        None,
    )
