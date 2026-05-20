"""Schema introspection service — used by the onboarding route and the background worker.

Kept in the services layer so the worker process never has to import FastAPI code.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.connection import ColumnInfo, TableInfo
from app.infrastructure.crypto import decrypt_dsn
from app.infrastructure.database.connection_tester import introspect_schema
from app.infrastructure.database.models.connection import Connection
from app.infrastructure.database.repositories.schema_mapping_repository import SchemaMappingRepository
from app.services.studio_file_source_service import (
    fetch_file_source_schema,
    supports_studio_file_queries,
)

logger = logging.getLogger(__name__)


def infer_col(columns: list, *patterns: str) -> str | None:
    """Return the first column name that matches any pattern (exact, prefix, or suffix)."""
    col_map = {c.name.lower(): c.name for c in columns}
    for pat in patterns:
        for lower, original in col_map.items():
            if lower == pat or lower.endswith(f"_{pat}") or lower.startswith(f"{pat}_"):
                return original
    return None


def file_schema_dicts_to_table_info(tables: list[dict[str, Any]]) -> list[TableInfo]:
    """Normalize file-source schema dicts into TableInfo objects."""
    result: list[TableInfo] = []
    for table in tables:
        columns = [
            ColumnInfo(
                name=str(col["name"]),
                data_type=str(col.get("data_type") or "text"),
                nullable=bool(col.get("nullable", True)),
            )
            for col in table.get("columns") or []
        ]
        result.append(TableInfo(name=str(table["name"]), columns=columns))
    return result


async def introspect_connection_tables(conn: Connection) -> list[TableInfo]:
    """Introspect tables for any connection type (SQL DB or file source)."""
    if supports_studio_file_queries(conn):
        raw = await fetch_file_source_schema(conn)
        return file_schema_dicts_to_table_info(raw)
    if not conn.encrypted_dsn:
        raise ValueError("Connection has no credentials for schema introspection")
    dsn = decrypt_dsn(conn.encrypted_dsn)
    return await introspect_schema(dsn, sslmode=conn.sslmode)


async def auto_create_schema_mapping(
    db: AsyncSession,
    *,
    org_id: UUID,
    conn: Connection,
    entity_label: str | None,
    goal_label: str | None,
) -> None:
    """Introspect the client data source and upsert a best-guess schema mapping for the org.

    Scores tables by name heuristics, infers key columns, and writes (or updates)
    the schema_mappings row including the full raw_schema cache used by
    GET /onboarding/connection/schema.
    """
    connection_id = conn.id
    try:
        tables = await introspect_connection_tables(conn)
    except Exception:
        logger.warning(
            "Schema introspection failed for connection %s — skipping auto-mapping",
            connection_id,
        )
        return

    if not tables:
        return

    entity_kw = (entity_label or "").lower().strip()

    def _score(t) -> tuple:
        name = t.name.lower()
        if entity_kw and (entity_kw in name or name in entity_kw):
            return (3, len(t.columns))
        if any(kw in name for kw in (
            "customer", "user", "subscriber", "patient", "client",
            "member", "account", "employee", "product", "item", "sku",
        )):
            return (2, len(t.columns))
        if any(kw in name for kw in (
            "log", "audit", "config", "setting", "migration",
            "session", "token", "permission",
        )):
            return (0, len(t.columns))
        return (1, len(t.columns))

    best = max(tables, key=_score)
    cols = best.columns

    entity_id_col = infer_col(cols, "id", "uuid", "key", "pk") or cols[0].name
    entity_name_col = infer_col(cols, "name", "full_name", "fullname", "display_name", "title", "email", "username")
    timestamp_col = infer_col(cols, "created_at", "timestamp", "date", "updated_at", "event_date", "recorded_at")

    goal_kw = (goal_label or "").lower()
    target_col = None
    if "churn" in goal_kw:
        target_col = infer_col(cols, "churned", "churn", "is_active", "active", "status", "cancelled")
    elif any(kw in goal_kw for kw in ("stock", "inventor")):
        target_col = infer_col(cols, "stock", "quantity", "inventory", "available", "qty")
    elif "risk" in goal_kw:
        target_col = infer_col(cols, "risk", "risk_score", "risk_tier", "score")

    raw_schema = {
        "tables": [
            {
                "name": t.name,
                "columns": [
                    {"name": c.name, "data_type": c.data_type, "nullable": c.nullable}
                    for c in t.columns
                ],
            }
            for t in tables
        ]
    }

    repo = SchemaMappingRepository(db)
    existing = await repo.list_by_org(org_id)
    if existing:
        await repo.update(
            existing[0].id,
            connection_id=connection_id,
            entity_table=best.name,
            entity_id_col=entity_id_col,
            entity_name_col=entity_name_col,
            timestamp_col=timestamp_col,
            target_column=target_col,
            raw_schema=raw_schema,
        )
    else:
        await repo.create(
            org_id=org_id,
            connection_id=connection_id,
            entity_table=best.name,
            entity_id_col=entity_id_col,
            entity_name_col=entity_name_col,
            timestamp_col=timestamp_col,
            target_column=target_col,
            raw_schema=raw_schema,
        )


async def trigger_auto_schema_mapping(
    db: AsyncSession,
    *,
    org_id: UUID,
    conn: Connection,
) -> None:
    """Run auto-mapping using org entity/goal labels (inline fallback when Redis queue is unavailable)."""
    from app.infrastructure.database.repositories.organization_repository import OrganizationRepository

    org = await OrganizationRepository(db).get_by_id(org_id)
    await auto_create_schema_mapping(
        db,
        org_id=org_id,
        conn=conn,
        entity_label=org.entity_label if org else None,
        goal_label=org.goal_label if org else None,
    )
