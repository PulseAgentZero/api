"""Build encrypted DSN / metadata rows from API create payloads."""

from __future__ import annotations

from urllib.parse import quote

from app.api.schemas.connection import CreateConnectionRequest
from app.api.errors import bad_request
from app.infrastructure.connectors.payload import pulse_api_blob
from app.infrastructure.database.sql_connect import mssql_odbc_query


def _norm_connector(ct: str | None, db: str | None) -> str:
    if ct:
        return ct
    if db == "mysql":
        return "mysql"
    return "postgresql"


def build_encrypted_secret_and_row_fields(body: CreateConnectionRequest) -> dict:
    """Return kwargs for ``ConnectionRepository.create``.

    All connector-specific fields go into ``connection_meta`` so the
    connections table stays schema-agnostic. Callers only need to forward
    ``plaintext_secret``, ``connector_type``, and ``connection_meta``.
    """
    ct = _norm_connector(body.connector_type, body.db_type)

    if ct in ("postgresql", "mysql", "mssql", "redshift"):
        if not body.host or body.port is None:
            raise bad_request("BAD_REQUEST", "host and port are required for this connector")
        if not body.database_name or not body.username:
            raise bad_request("BAD_REQUEST", "database_name and username are required")
        if ct in ("postgresql", "mysql", "mssql") and not body.password:
            raise bad_request("BAD_REQUEST", "password is required")
        db_t = body.db_type or ("mysql" if ct == "mysql" else "postgresql")
        if ct == "mssql":
            db_t = "mssql"
        if ct == "redshift":
            db_t = "redshift"
        dsn = _make_sql_dsn(
            db_t, body.host, body.port, body.database_name, body.username,
            body.password or "", sslmode=body.sslmode,
        )
        return {
            "plaintext_secret": dsn,
            "connector_type": ct,
            "connection_meta": {
                "kind": "sql",
                "db_type": db_t,
                "host": body.host,
                "port": body.port,
                "database_name": body.database_name,
                "username": body.username,
                "sslmode": body.sslmode or "prefer",
            },
        }

    if ct == "sqlite":
        path = (body.database_name or "").strip()
        if not path:
            raise bad_request("BAD_REQUEST", "For SQLite, database_name must be the file path")
        return {
            "plaintext_secret": f"sqlite:///{path}",
            "connector_type": "sqlite",
            "connection_meta": {
                "kind": "sqlite",
                "db_type": "sqlite",
                "database_name": path,
                "sslmode": "disable",
            },
        }

    if ct in ("snowflake", "bigquery", "databricks"):
        url = (body.connection_url or "").strip()
        if not url:
            raise bad_request("BAD_REQUEST", "connection_url is required for this warehouse connector")
        return {
            "plaintext_secret": url,
            "connector_type": ct,
            "connection_meta": {"kind": "warehouse", "db_type": ct, "provider": ct},
        }

    if ct == "clickhouse":
        if body.connection_url and body.connection_url.strip().startswith("clickhouse"):
            blob = pulse_api_blob("clickhouse_native", dsn=body.connection_url.strip())
            mode = "native"
        elif body.clickhouse_https_url:
            blob = pulse_api_blob(
                "clickhouse_http",
                base_url=body.clickhouse_https_url.strip(),
                user=body.clickhouse_user or "",
                password=body.clickhouse_password or "",
            )
            mode = "http"
        else:
            raise bad_request(
                "BAD_REQUEST",
                "ClickHouse requires connection_url (native DSN) or clickhouse_https_url",
            )
        return {
            "plaintext_secret": blob,
            "connector_type": "clickhouse",
            "connection_meta": {"kind": "clickhouse", "db_type": "clickhouse", "mode": mode},
        }

    if ct == "airtable":
        if not body.airtable_pat:
            raise bad_request("BAD_REQUEST", "airtable_pat is required")
        blob = pulse_api_blob("airtable", pat=body.airtable_pat.strip())
        return {
            "plaintext_secret": blob,
            "connector_type": "airtable",
            "connection_meta": {
                "kind": "airtable",
                "db_type": "airtable",
                "database_name": (body.airtable_base_id or "").strip() or None,
            },
        }

    if ct == "google_sheets":
        if not body.google_sheets_api_key or not body.google_spreadsheet_id:
            raise bad_request("BAD_REQUEST", "google_sheets_api_key and google_spreadsheet_id are required")
        blob = pulse_api_blob(
            "google_sheets",
            api_key=body.google_sheets_api_key.strip(),
            spreadsheet_id=body.google_spreadsheet_id.strip(),
        )
        return {
            "plaintext_secret": blob,
            "connector_type": "google_sheets",
            "connection_meta": {
                "kind": "google_sheets",
                "db_type": "google_sheets",
                "spreadsheet_id": body.google_spreadsheet_id.strip(),
            },
        }

    if ct == "mongodb":
        if not body.mongodb_uri:
            raise bad_request("BAD_REQUEST", "mongodb_uri is required")
        blob = pulse_api_blob("mongodb", uri=body.mongodb_uri.strip())
        return {
            "plaintext_secret": blob,
            "connector_type": "mongodb",
            "connection_meta": {"kind": "mongodb", "db_type": "mongodb"},
        }

    if ct == "s3":
        if not body.s3_bucket or not body.s3_access_key_id or not body.s3_secret_access_key:
            raise bad_request("BAD_REQUEST", "s3_bucket, s3_access_key_id, and s3_secret_access_key are required")
        blob = pulse_api_blob(
            "s3",
            bucket=body.s3_bucket.strip(),
            access_key_id=body.s3_access_key_id.strip(),
            secret_access_key=body.s3_secret_access_key.strip(),
            region=body.s3_region or "us-east-1",
        )
        return {
            "plaintext_secret": blob,
            "connector_type": "s3",
            "connection_meta": {
                "kind": "s3",
                "db_type": "s3",
                "bucket": body.s3_bucket.strip(),
                "region": body.s3_region or "us-east-1",
            },
        }

    if ct == "gcs":
        if not body.gcs_bucket or not body.gcs_service_account_json:
            raise bad_request("BAD_REQUEST", "gcs_bucket and gcs_service_account_json are required")
        blob = pulse_api_blob(
            "gcs",
            bucket=body.gcs_bucket.strip(),
            service_account_json=body.gcs_service_account_json.strip(),
        )
        return {
            "plaintext_secret": blob,
            "connector_type": "gcs",
            "connection_meta": {
                "kind": "gcs",
                "db_type": "gcs",
                "bucket": body.gcs_bucket.strip(),
            },
        }

    raise bad_request("BAD_REQUEST", f"Unsupported connector_type: {ct}")


def _make_sql_dsn(
    db_type: str,
    host: str,
    port: int,
    database_name: str,
    username: str,
    password: str,
    *,
    sslmode: str | None = None,
) -> str:
    if db_type == "mysql":
        scheme = "mysql"
    elif db_type == "mssql":
        q = mssql_odbc_query()
        return (
            f"mssql+aioodbc://{quote(username, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{quote(database_name, safe='')}?{q}"
        )
    elif db_type == "redshift":
        scheme = "postgresql"
    else:
        scheme = "postgresql"
    return (
        f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database_name, safe='')}"
    )
