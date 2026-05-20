"""Connector catalog: auth method and read stack (matches product / infra choices)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    connector: str
    auth_method: str
    read_approach: str
    notes: str = ""


CONNECTOR_REGISTRY: dict[str, ConnectorSpec] = {
    "postgresql": ConnectorSpec(
        "postgresql", "DSN / credentials", "asyncpg", "Postgres-compatible wire protocol.",
    ),
    "mysql": ConnectorSpec("mysql", "DSN / credentials", "aiomysql", ""),
    "mssql": ConnectorSpec(
        "mssql",
        "DSN / credentials",
        "aioodbc",
        "Requires ODBC Driver 18+ (or MSSQL_ODBC_DRIVER). Linux images need unixODBC.",
    ),
    "sqlite": ConnectorSpec(
        "sqlite",
        "File path",
        "aiosqlite",
        "database_name = path on Pulse host; use read-only file permissions where possible.",
    ),
    "redshift": ConnectorSpec(
        "redshift",
        "DSN + IAM / password",
        "asyncpg",
        "Use postgres:// or postgresql:// URL to Redshift; IAM auth via URL params supported by driver.",
    ),
    "snowflake": ConnectorSpec(
        "snowflake",
        "Account + user + key / OAuth URL",
        "snowflake-connector-python",
        "Paste full snowflake:// URL; uses sync connector for test/introspect.",
    ),
    "clickhouse": ConnectorSpec(
        "clickhouse",
        "HTTP(S) + user/password",
        "clickhouse-connect / HTTP",
        "Use clickhouse_dsn HTTPS URL; ping via clickhouse-connect or HTTP ?query=SELECT+1.",
    ),
    "bigquery": ConnectorSpec(
        "bigquery",
        "Service account JSON / OAuth",
        "google-cloud-bigquery",
        "Paste bigquery:// URL or use connection_url with project; optional service_account_json field.",
    ),
    "google_sheets": ConnectorSpec(
        "google_sheets",
        "API key or OAuth",
        "Google Sheets API v4 (httpx)",
        "API key path for public sheets; OAuth not wired in this minimal build.",
    ),
    "s3": ConnectorSpec(
        "s3",
        "Access key + secret (+ session)",
        "boto3",
        "Object storage; list/preview not used by SQL pipeline until file bridge exists.",
    ),
    "gcs": ConnectorSpec(
        "gcs",
        "Service account JSON",
        "google-cloud-storage",
        "Bucket access for Parquet/CSV objects.",
    ),
    "csv": ConnectorSpec("csv", "File upload", "temp file + parser", "CSV/TSV upload; queryable in Studio."),
    "excel": ConnectorSpec(
        "excel",
        "File upload",
        "openpyxl / xlrd + pandas",
        "Excel workbook; each sheet is a Studio table.",
    ),
    "airtable": ConnectorSpec("airtable", "PAT / OAuth", "Airtable REST (httpx)", ""),
    "mongodb": ConnectorSpec("mongodb", "URI", "motor (async)", "Read-only usage via find/aggregate in future agents."),
    "databricks": ConnectorSpec(
        "databricks",
        "HTTP path + token",
        "databricks-sql-connector / SQL warehouse",
        "Paste databricks:// URL from workspace SQL warehouse.",
    ),
}
