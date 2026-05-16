"""Schema introspection service — used by the onboarding route and the background worker.

Kept in the services layer so the worker process never has to import FastAPI code.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.connection_tester import introspect_schema
from app.infrastructure.database.repositories.schema_mapping_repository import SchemaMappingRepository

logger = logging.getLogger(__name__)


def infer_col(columns: list, *patterns: str) -> str | None:
    """Return the first column name that matches any pattern (exact, prefix, or suffix)."""
    col_map = {c.name.lower(): c.name for c in columns}
    for pat in patterns:
        for lower, original in col_map.items():
            if lower == pat or lower.endswith(f"_{pat}") or lower.startswith(f"{pat}_"):
                return original
    return None


async def auto_create_schema_mapping(
    db: AsyncSession,
    *,
    org_id: UUID,
    connection_id: UUID,
    plaintext_dsn: str,
    sslmode: str | None,
    entity_label: str | None,
    goal_label: str | None,
) -> None:
    """Introspect the client DB and upsert a best-guess schema mapping for the org.

    Scores tables by name heuristics, infers key columns, and writes (or updates)
    the schema_mappings row including the full raw_schema cache used by
    GET /onboarding/connection/schema.
    """
    try:
        tables = await introspect_schema(plaintext_dsn, sslmode=sslmode)
    except Exception:
        logger.warning("Schema introspection failed for connection %s — skipping auto-mapping", connection_id)
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
