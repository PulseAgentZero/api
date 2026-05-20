"""Connectivity checks for SaaS, warehouses, and object storage."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def test_airtable(pat: str) -> tuple[bool, str]:
    pat = pat.strip()
    if not pat:
        return False, "Missing Airtable personal access token"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            "https://api.airtable.com/v0/meta/whoami",
            headers={"Authorization": f"Bearer {pat}"},
        )
        if r.status_code == 200:
            return True, "Airtable token accepted"
        return False, f"Airtable HTTP {r.status_code}: {r.text[:300]}"


async def test_google_sheets(*, api_key: str, spreadsheet_id: str) -> tuple[bool, str]:
    api_key, spreadsheet_id = api_key.strip(), spreadsheet_id.strip()
    if not api_key or not spreadsheet_id:
        return False, "google_sheets_api_key and google_spreadsheet_id required"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, params={"key": api_key, "fields": "properties/title"})
        if r.status_code == 200:
            return True, "Google Sheets API key can read spreadsheet"
        return False, f"Google Sheets HTTP {r.status_code}: {r.text[:300]}"


def _sheets_bearer_token(service_account_json: str) -> str:
    import json

    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    info = json.loads(service_account_json)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    creds.refresh(Request())
    if not creds.token:
        raise ValueError("Could not obtain access token from service account")
    return creds.token


async def test_google_sheets_service_account(
    *, service_account_json: str, spreadsheet_id: str
) -> tuple[bool, str]:
    spreadsheet_id = spreadsheet_id.strip()
    if not service_account_json.strip() or not spreadsheet_id:
        return False, "service_account_json and spreadsheet_id required"
    try:
        token = await asyncio.to_thread(_sheets_bearer_token, service_account_json.strip())
    except Exception as exc:
        return False, f"Invalid service account JSON: {exc}"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "properties/title"},
        )
        if r.status_code == 200:
            return True, "Service account can read spreadsheet"
        return False, f"Google Sheets HTTP {r.status_code}: {r.text[:300]}"


def _test_bigquery_service_account_sync(
    connection_url: str, service_account_json: str
) -> tuple[bool, str]:
    import json

    from google.cloud import bigquery
    from google.oauth2 import service_account

    from app.infrastructure.connectors.connector_credentials import _parse_bigquery_url

    project, _dataset = _parse_bigquery_url(connection_url)
    info = json.loads(service_account_json)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/bigquery"],
    )
    client = bigquery.Client(credentials=creds, project=project or info.get("project_id"))
    client.query("SELECT 1").result()
    return True, "BigQuery service account can run queries"


async def test_bigquery_service_account(
    connection_url: str, service_account_json: str
) -> tuple[bool, str]:
    try:
        return await asyncio.to_thread(
            _test_bigquery_service_account_sync, connection_url, service_account_json
        )
    except Exception as exc:
        return False, str(exc)


async def test_mongodb_uri(uri: str) -> tuple[bool, str]:
    uri = uri.strip()
    if not uri:
        return False, "Missing MongoDB URI"

    def _ping() -> None:
        from motor.motor_asyncio import AsyncIOMotorClient

        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
        try:
            import asyncio as _aio

            loop = _aio.get_event_loop()
            loop.run_until_complete(client.admin.command("ping"))
        finally:
            client.close()

    try:
        # Motor async API must run on loop — use inner async helper instead.
        async def _async_ping() -> None:
            from motor.motor_asyncio import AsyncIOMotorClient

            c = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
            try:
                await c.admin.command("ping")
            finally:
                c.close()

        await _async_ping()
        return True, "MongoDB ping successful"
    except Exception as exc:
        return False, f"MongoDB: {exc}"


async def test_clickhouse_https(base_url: str, *, user: str = "", password: str = "") -> tuple[bool, str]:
    base = base_url.strip().rstrip("/")
    if not base.startswith("http"):
        return False, "clickhouse_dsn must be an https:// or http:// URL"
    q = f"{base}/?query=SELECT+1"
    auth = (user, password) if user else None
    async with httpx.AsyncClient(timeout=20.0, verify=True) as client:
        r = await client.get(q, auth=auth)
        if r.status_code == 200 and r.text.strip().startswith("1"):
            return True, "ClickHouse HTTP SELECT 1 ok"
        return False, f"ClickHouse HTTP {r.status_code}: {r.text[:200]}"


def _test_clickhouse_native_sync(dsn: str) -> tuple[bool, str]:
    try:
        import clickhouse_connect
    except ImportError:
        return False, "Install clickhouse-connect for native ClickHouse DSN tests"
    try:
        client = clickhouse_connect.get_client(dsn=dsn)
        client.command("SELECT 1")
        client.close()
        return True, "ClickHouse native client ok"
    except Exception as exc:
        return False, str(exc)


async def test_clickhouse_native(dsn: str) -> tuple[bool, str]:
    return await asyncio.to_thread(_test_clickhouse_native_sync, dsn)


def _test_s3_sync(
    bucket: str,
    access_key_id: str,
    secret_access_key: str,
    region: str | None,
) -> tuple[bool, str]:
    import boto3
    from botocore.exceptions import ClientError

    try:
        s3 = boto3.client(
            "s3",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region or "us-east-1",
        )
        s3.head_bucket(Bucket=bucket)
        return True, "S3 bucket reachable"
    except ClientError as exc:
        return False, f"S3: {exc}"
    except Exception as exc:
        return False, str(exc)


async def test_s3_bucket(
    bucket: str,
    access_key_id: str,
    secret_access_key: str,
    region: str | None = None,
) -> tuple[bool, str]:
    return await asyncio.to_thread(_test_s3_sync, bucket, access_key_id, secret_access_key, region)


def _test_gcs_sync(bucket: str, service_account_json: str) -> tuple[bool, str]:
    try:
        from google.cloud import storage
        from google.oauth2 import service_account
    except ImportError:
        return False, "Install google-cloud-storage for GCS tests"
    try:
        info = json.loads(service_account_json)
        creds = service_account.Credentials.from_service_account_info(info)
        client = storage.Client(credentials=creds, project=info.get("project_id"))
        b = client.bucket(bucket)
        if not b.exists():
            return False, "GCS bucket not found or no access"
        return True, "GCS bucket exists"
    except Exception as exc:
        return False, str(exc)


async def test_gcs_bucket(bucket: str, service_account_json: str) -> tuple[bool, str]:
    return await asyncio.to_thread(_test_gcs_sync, bucket, service_account_json)


def _sync_sqlalchemy_ping(dsn: str) -> tuple[bool, str, str | None]:
    from sqlalchemy import create_engine, text

    eng = create_engine(dsn, pool_pre_ping=True, future=True)
    try:
        with eng.connect() as c:
            low = dsn.lower()
            if "snowflake" in low:
                row = c.execute(text("SELECT CURRENT_VERSION()")).scalar_one()
            elif "bigquery" in low or "databricks" in low:
                row = c.execute(text("SELECT 1")).scalar_one()
            elif "redshift" in low and "+" in low:
                row = c.execute(text("SELECT version()")).scalar_one()
            else:
                row = c.execute(text("SELECT 1")).scalar_one()
        return True, "Connection successful", str(row)[:500]
    finally:
        eng.dispose()


async def test_sync_sqlalchemy_dsn(dsn: str) -> tuple[bool, str, str | None]:
    return await asyncio.to_thread(_sync_sqlalchemy_ping, dsn)


async def test_pulse_api_payload(payload: dict[str, Any]) -> tuple[bool, str, str | None]:
    kind = str(payload.get("kind", ""))
    if kind == "airtable":
        ok, msg = await test_airtable(str(payload.get("pat", "")))
        return ok, msg, None
    if kind == "google_sheets":
        spreadsheet_id = str(payload.get("spreadsheet_id", ""))
        if payload.get("service_account_json"):
            ok, msg = await test_google_sheets_service_account(
                service_account_json=str(payload["service_account_json"]),
                spreadsheet_id=spreadsheet_id,
            )
        elif payload.get("api_key"):
            ok, msg = await test_google_sheets(
                api_key=str(payload["api_key"]),
                spreadsheet_id=spreadsheet_id,
            )
        else:
            return False, "Google Sheets credentials missing (api_key or service_account_json)", None
        return ok, msg, None
    if kind == "bigquery" and payload.get("service_account_json"):
        ok, msg = await test_bigquery_service_account(
            str(payload.get("connection_url", "")),
            str(payload["service_account_json"]),
        )
        return ok, msg, None
    if kind == "mongodb":
        ok, msg = await test_mongodb_uri(str(payload.get("uri", "")))
        return ok, msg, None
    if kind == "clickhouse_http":
        ok, msg = await test_clickhouse_https(
            str(payload.get("base_url", "")),
            user=str(payload.get("user", "")),
            password=str(payload.get("password", "")),
        )
        return ok, msg, None
    if kind == "clickhouse_native":
        ok, msg = await test_clickhouse_native(str(payload.get("dsn", "")))
        return ok, msg, None
    if kind == "s3":
        ok, msg = await test_s3_bucket(
            str(payload.get("bucket", "")),
            str(payload.get("access_key_id", "")),
            str(payload.get("secret_access_key", "")),
            payload.get("region"),
        )
        return ok, msg, None
    if kind == "gcs":
        ok, msg = await test_gcs_bucket(
            str(payload.get("bucket", "")),
            str(payload.get("service_account_json", "")),
        )
        return ok, msg, None
    return False, f"Unknown API connector kind: {kind}", None
