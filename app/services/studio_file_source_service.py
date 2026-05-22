"""Entivia Studio — query CSV uploads, Google Sheets, and S3 CSV objects via DuckDB."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Any
from uuid import UUID

import httpx
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import bad_request, not_found
from app.infrastructure.connectors.payload import parse_pulse_api_payload
from app.infrastructure.crypto import decrypt_dsn
from app.infrastructure.database.models.connection import Connection
from app.services.studio_query_service import (
    _MAX_ROWS,
    _is_select_only,
    _serialize_rows,
    apply_params,
)

logger = logging.getLogger(__name__)

STUDIO_FILE_CONNECTOR_TYPES = frozenset({"csv", "excel", "google_sheets", "s3"})
_MAX_EXCEL_SHEETS = 20
_MAX_S3_CSV_FILES = 15
_MAX_S3_BYTES_PER_FILE = 25 * 1024 * 1024
_TABLE_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def supports_studio_file_queries(conn: Connection) -> bool:
    return (conn.connector_type or "") in STUDIO_FILE_CONNECTOR_TYPES


async def get_connection_for_studio(
    db: AsyncSession, org_id: UUID, connection_id: UUID | None
) -> Connection:
    if connection_id is not None:
        result = await db.execute(
            select(Connection).where(
                Connection.id == connection_id,
                Connection.org_id == org_id,
                Connection.deleted_at.is_(None),
            )
        )
        conn = result.scalar_one_or_none()
        if not conn:
            raise not_found("Connection not found")
        return conn

    result = await db.execute(
        select(Connection)
        .where(Connection.org_id == org_id, Connection.deleted_at.is_(None))
        .order_by(Connection.created_at.desc())
    )
    conns = list(result.scalars().all())
    if not conns:
        raise not_found("No connection found for this organisation")
    active = [c for c in conns if c.status == "active"]
    pool = active or conns
    for c in pool:
        if supports_studio_file_queries(c):
            return c
        if c.encrypted_dsn and parse_pulse_api_payload(decrypt_dsn(c.encrypted_dsn)) is None:
            return c
    return pool[0]


def _sanitize_table_name(name: str, *, fallback: str = "data") -> str:
    base = _TABLE_NAME_RE.sub("_", name.strip()).strip("_").lower()
    if not base:
        base = fallback
    if base[0].isdigit():
        base = f"t_{base}"
    return base[:63]


def _df_to_schema_col(name: str, series: pd.Series) -> dict[str, Any]:
    dtype = str(series.dtype)
    if "int" in dtype or "float" in dtype:
        data_type = "numeric"
    elif "bool" in dtype:
        data_type = "boolean"
    elif "datetime" in dtype:
        data_type = "timestamp"
    else:
        data_type = "text"
    return {"name": name, "data_type": data_type, "nullable": bool(series.isna().any())}


def _schema_from_frames(frames: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    tables = []
    for tname, df in frames.items():
        if df.empty and len(df.columns) == 0:
            continue
        tables.append(
            {
                "name": tname,
                "columns": [_df_to_schema_col(str(c), df[c]) for c in df.columns],
            }
        )
    return tables


def _load_csv_upload(conn: Connection) -> dict[str, pd.DataFrame]:
    path = (conn.config or {}).get("upload_path")
    if not path:
        raise bad_request(
            "CONNECTION_ERROR",
            "File connection has no uploaded file. Re-upload the file in Connections.",
        )
    filename = (conn.config or {}).get("original_filename") or "data.csv"
    tname = _sanitize_table_name(filename.rsplit(".", 1)[0])
    is_tsv = filename.lower().endswith(".tsv")
    try:
        df = pd.read_csv(path, nrows=_MAX_ROWS, sep="\t" if is_tsv else ",")
    except Exception as exc:
        raise bad_request("CLIENT_DB_ERROR", f"Could not read file: {exc}") from exc
    return {tname: df}


def _excel_engine_for_path(path: str, filename: str | None) -> str:
    name = (filename or path).lower()
    if name.endswith(".xls") and not name.endswith(".xlsx"):
        return "xlrd"
    return "openpyxl"


def _load_excel_upload(conn: Connection) -> dict[str, pd.DataFrame]:
    import zipfile

    path = (conn.config or {}).get("upload_path")
    if not path:
        raise bad_request(
            "CONNECTION_ERROR",
            "Excel connection has no uploaded file. Re-upload the file in Connections.",
        )
    filename = (conn.config or {}).get("original_filename") or path
    engine = _excel_engine_for_path(path, filename)

    try:
        workbook = pd.ExcelFile(path, engine=engine)
    except zipfile.BadZipFile as exc:
        raise bad_request(
            "CLIENT_DB_ERROR",
            "We couldn't read this workbook — make sure it's a valid Excel file.",
        ) from exc
    except Exception as exc:
        msg = str(exc).lower()
        if "password" in msg or "encrypted" in msg:
            raise bad_request(
                "CLIENT_DB_ERROR",
                "Workbook is password-protected. Save an unprotected copy and re-upload.",
            ) from exc
        raise bad_request(
            "CLIENT_DB_ERROR",
            f"Could not open Excel file: {exc}",
        ) from exc

    frames: dict[str, pd.DataFrame] = {}
    for sheet_name in workbook.sheet_names[:_MAX_EXCEL_SHEETS]:
        try:
            df = pd.read_excel(
                workbook,
                sheet_name=sheet_name,
                nrows=_MAX_ROWS,
                engine=engine,
            )
        except Exception as exc:
            logger.warning("Skip sheet %s: %s", sheet_name, exc)
            continue

        if df.empty and len(df.columns) == 0:
            continue

        # Drop sheets with no usable header row
        if len(df.columns) == 0 or all(str(c).startswith("Unnamed") for c in df.columns):
            if df.empty:
                continue

        tname = _sanitize_table_name(str(sheet_name), fallback="sheet")
        if tname in frames:
            tname = f"{tname}_{len(frames)}"
        frames[tname] = df

    if not frames:
        raise bad_request("CLIENT_DB_ERROR", "No readable sheets in workbook")

    return frames


async def _fetch_google_sheets_frames(
    *,
    spreadsheet_id: str,
    api_key: str | None = None,
    bearer_token: str | None = None,
) -> dict[str, pd.DataFrame]:
    spreadsheet_id = spreadsheet_id.strip()
    if not spreadsheet_id:
        raise bad_request("CONNECTION_ERROR", "Spreadsheet ID is required")
    if not api_key and not bearer_token:
        raise bad_request("CONNECTION_ERROR", "Google Sheets credentials missing")

    frames: dict[str, pd.DataFrame] = {}
    async with httpx.AsyncClient(timeout=60.0) as client:
        meta_url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
        if bearer_token:
            meta = await client.get(
                meta_url,
                headers={"Authorization": f"Bearer {bearer_token}"},
                params={"fields": "sheets.properties.title"},
            )
        else:
            meta = await client.get(
                meta_url,
                params={"key": api_key, "fields": "sheets.properties.title"},
            )
        if meta.status_code != 200:
            raise bad_request(
                "CLIENT_DB_ERROR",
                f"Google Sheets API error: HTTP {meta.status_code}",
            )
        sheets = meta.json().get("sheets") or []
        for sheet in sheets[:10]:
            title = (sheet.get("properties") or {}).get("title") or "Sheet1"
            tname = _sanitize_table_name(title, fallback="sheet")
            if tname in frames:
                tname = f"{tname}_{len(frames)}"
            range_name = f"'{title}'" if " " in title or "'" in title else title
            values_url = f"{meta_url}/values/{range_name}"
            if bearer_token:
                vr = await client.get(
                    values_url,
                    headers={"Authorization": f"Bearer {bearer_token}"},
                )
            else:
                vr = await client.get(values_url, params={"key": api_key})
            if vr.status_code != 200:
                logger.warning("Skip sheet %s: HTTP %s", title, vr.status_code)
                continue
            rows = vr.json().get("values") or []
            if not rows:
                frames[tname] = pd.DataFrame()
                continue
            header = [str(h) for h in rows[0]]
            data_rows = rows[1 : _MAX_ROWS + 1]
            if not data_rows:
                frames[tname] = pd.DataFrame(columns=header)
            else:
                width = len(header)
                normalized = [
                    [(r[i] if i < len(r) else None) for i in range(width)]
                    for r in data_rows
                ]
                frames[tname] = pd.DataFrame(normalized, columns=header)
    if not frames:
        raise bad_request("CLIENT_DB_ERROR", "No readable sheets found in spreadsheet")
    return frames


def _load_s3_csv_frames(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, pd.DataFrame]:
    import boto3

    bucket = str(payload.get("bucket", ""))
    prefix = str((config or {}).get("prefix") or "")
    s3 = boto3.client(
        "s3",
        aws_access_key_id=str(payload.get("access_key_id", "")),
        aws_secret_access_key=str(payload.get("secret_access_key", "")),
        region_name=payload.get("region") or "us-east-1",
    )
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            key = obj.get("Key") or ""
            if key.lower().endswith(".csv"):
                keys.append(key)
            if len(keys) >= _MAX_S3_CSV_FILES:
                break
        if len(keys) >= _MAX_S3_CSV_FILES:
            break
    if not keys:
        raise bad_request(
            "CLIENT_DB_ERROR",
            f"No CSV files found in s3://{bucket}/{prefix}".rstrip("/"),
        )
    frames: dict[str, pd.DataFrame] = {}
    for key in keys:
        base = key.rsplit("/", 1)[-1]
        tname = _sanitize_table_name(base.rsplit(".", 1)[0])
        if tname in frames:
            tname = f"{tname}_{len(frames)}"
        obj = s3.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read(_MAX_S3_BYTES_PER_FILE + 1)
        if len(body) > _MAX_S3_BYTES_PER_FILE:
            raise bad_request("CLIENT_DB_ERROR", f"S3 object too large: {key}")
        df = pd.read_csv(io.BytesIO(body), nrows=_MAX_ROWS)
        frames[tname] = df
    return frames


async def load_file_source_frames(conn: Connection) -> dict[str, pd.DataFrame]:
    ct = conn.connector_type or ""
    if ct == "csv":
        return await asyncio.to_thread(_load_csv_upload, conn)
    if ct == "excel":
        return await asyncio.to_thread(_load_excel_upload, conn)
    if ct == "google_sheets":
        if not conn.encrypted_dsn:
            raise bad_request("CONNECTION_ERROR", "Google Sheets credentials missing")
        payload = parse_pulse_api_payload(decrypt_dsn(conn.encrypted_dsn))
        if not payload:
            raise bad_request("CONNECTION_ERROR", "Invalid Google Sheets connection")
        spreadsheet_id = str(payload.get("spreadsheet_id", ""))
        if payload.get("service_account_json"):
            from app.infrastructure.connectors.connector_health import _sheets_bearer_token

            token = await asyncio.to_thread(
                _sheets_bearer_token, str(payload["service_account_json"])
            )
            return await _fetch_google_sheets_frames(
                spreadsheet_id=spreadsheet_id,
                bearer_token=token,
            )
        return await _fetch_google_sheets_frames(
            spreadsheet_id=spreadsheet_id,
            api_key=str(payload.get("api_key", "")),
        )
    if ct == "s3":
        if not conn.encrypted_dsn:
            raise bad_request("CONNECTION_ERROR", "S3 credentials missing")
        payload = parse_pulse_api_payload(decrypt_dsn(conn.encrypted_dsn))
        if not payload:
            raise bad_request("CONNECTION_ERROR", "Invalid S3 connection")
        return await asyncio.to_thread(_load_s3_csv_frames, payload, conn.config or {})
    raise bad_request("CONNECTION_ERROR", f"Unsupported file source: {ct}")


async def fetch_file_source_schema(conn: Connection) -> list[dict[str, Any]]:
    frames = await load_file_source_frames(conn)
    return _schema_from_frames(frames)


def _execute_duckdb(
    frames: dict[str, pd.DataFrame],
    sql: str,
    bound_values: dict[str, Any],
    *,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    import duckdb

    if not frames:
        raise bad_request("CLIENT_DB_ERROR", "No tables available to query")

    duck_sql = re.sub(r":(\w+)\b", r"$\1", sql)

    con = duckdb.connect(":memory:")
    try:
        for tname, df in frames.items():
            con.register(tname, df)
        result = con.execute(duck_sql, bound_values)
        raw = result.fetchall()
        columns = [d[0] for d in (result.description or [])]
    finally:
        con.close()

    rows_serialized = _serialize_rows(raw, columns)
    total = len(rows_serialized)
    start = (page - 1) * page_size
    return {
        "rows": rows_serialized[start : start + page_size],
        "columns": columns,
        "total": total,
        "page": page,
        "page_size": page_size,
        "cached": False,
    }


async def execute_file_source_query(
    conn: Connection,
    sql_text: str,
    *,
    param_defs: list[dict] | None = None,
    param_values: dict[str, Any] | None = None,
    bound_values: dict[str, Any] | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """Run a SELECT against a file-source connection (CSV, Excel, Sheets, S3).

    Two calling modes are supported:
    1. Pass the original SQL with ``{{name}}`` placeholders plus ``param_defs`` /
       ``param_values`` — this function will substitute them.
    2. Pass SQL that already has ``:name`` placeholders along with ``bound_values``
       — used by :func:`execute_studio_query`, which performs substitution once
       so the cache key reflects the bound values.
    """
    if not _is_select_only(sql_text):
        raise bad_request(
            "INVALID_SQL",
            "Only SELECT statements are permitted in Entivia Studio.",
        )

    from app.services.studio_query_service import _inject_limit, _PARAM_PATTERN

    limited_sql = _inject_limit(sql_text.strip(), _MAX_ROWS)
    if bound_values is None:
        bound_values = {}
        if _PARAM_PATTERN.search(limited_sql):
            limited_sql, bound_values = apply_params(
                limited_sql, param_defs or [], param_values or {}
            )

    frames = await load_file_source_frames(conn)
    return await asyncio.to_thread(
        _execute_duckdb,
        frames,
        limited_sql,
        bound_values,
        page=page,
        page_size=page_size,
    )
