"""Assemble a JSON-serializable export bundle for one organization (tenant-scoped)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.agent_conversation import AgentConversation
from app.infrastructure.database.models.api_key import ApiKey
from app.infrastructure.database.models.connection import Connection
from app.infrastructure.database.models.invitation import Invitation
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.pipeline_schedule import PipelineSchedule
from app.infrastructure.database.models.recommendation import Recommendation
from app.infrastructure.database.models.schema_mapping import SchemaMapping
from app.infrastructure.database.models.user import User


def _dt(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _user_public(u: User) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "email": u.email,
        "full_name": u.full_name,
        "role": u.role,
        "is_active": u.is_active,
        "is_verified": u.is_verified,
        "profile_image_url": u.profile_image_url,
        "auth_provider": u.auth_provider,
        "last_login_at": _dt(u.last_login_at),
        "created_at": _dt(u.created_at),
    }


def _connection_public(c: Connection) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "name": c.name,
        "connector_type": c.connector_type,
        "db_type": c.db_type,
        "host": c.host,
        "port": c.port,
        "database_name": c.database_name,
        "username": c.username,
        "sslmode": c.sslmode,
        "status": c.status,
        "connection_meta": getattr(c, "connection_meta", None) or {},
        "config": c.config or {},
        "has_encrypted_secret": bool(c.encrypted_dsn),
        "last_tested_at": _dt(c.last_tested_at),
        "last_test_error": c.last_test_error,
        "created_at": _dt(c.created_at),
        "deleted_at": _dt(c.deleted_at),
    }


def _org_public(o: Organization) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "name": o.name,
        "slug": o.slug,
        "industry": o.industry,
        "business_context": o.business_context,
        "entity_label": o.entity_label,
        "goal_label": o.goal_label,
        "plan": o.plan,
        "timezone": o.timezone,
        "logo_url": o.logo_url,
        "tour_guide": getattr(o, "tour_guide", None) or {},
        "onboarding_done": o.onboarding_done,
        "deployment_mode": o.deployment_mode,
        "created_at": _dt(o.created_at),
        "updated_at": _dt(o.updated_at),
    }


async def build_organization_export(db: AsyncSession, org_id: UUID) -> dict[str, Any]:
    """Load all org-scoped rows for ``org_id``. Secrets (DSN, API keys) are never included."""
    org = await db.get(Organization, org_id)
    if org is None:
        return {}

    users = (
        await db.execute(select(User).where(User.org_id == org_id).order_by(User.created_at))
    ).scalars().all()
    conns = (
        await db.execute(
            select(Connection).where(Connection.org_id == org_id).order_by(Connection.created_at)
        )
    ).scalars().all()
    mappings = (
        await db.execute(
            select(SchemaMapping).where(SchemaMapping.org_id == org_id).order_by(SchemaMapping.created_at)
        )
    ).scalars().all()
    recs = (
        await db.execute(
            select(Recommendation).where(Recommendation.org_id == org_id).order_by(Recommendation.created_at)
        )
    ).scalars().all()
    invites = (
        await db.execute(
            select(Invitation).where(Invitation.org_id == org_id).order_by(Invitation.created_at)
        )
    ).scalars().all()
    schedules = (
        await db.execute(select(PipelineSchedule).where(PipelineSchedule.org_id == org_id))
    ).scalars().all()
    convos = (
        await db.execute(
            select(AgentConversation).where(AgentConversation.org_id == org_id).order_by(AgentConversation.updated_at)
        )
    ).scalars().all()
    api_keys = (
        await db.execute(select(ApiKey).where(ApiKey.org_id == org_id).order_by(ApiKey.created_at))
    ).scalars().all()

    def mapping_row(m: SchemaMapping) -> dict[str, Any]:
        return {
            "id": str(m.id),
            "connection_id": str(m.connection_id),
            "name": m.name,
            "entity_table": m.entity_table,
            "entity_id_col": m.entity_id_col,
            "entity_name_col": m.entity_name_col,
            "signal_columns": m.signal_columns,
            "timestamp_col": m.timestamp_col,
            "risk_config": m.risk_config,
            "raw_schema": m.raw_schema,
            "target_column": m.target_column,
            "rag_config": m.rag_config,
            "is_active": m.is_active,
            "created_at": _dt(m.created_at),
        }

    def rec_row(r: Recommendation) -> dict[str, Any]:
        return {
            "id": str(r.id),
            "entity_id": r.entity_id,
            "title": r.title,
            "type": r.type,
            "urgency": r.urgency,
            "status": r.status,
            "created_at": _dt(r.created_at),
        }

    def inv_row(i: Invitation) -> dict[str, Any]:
        return {
            "id": str(i.id),
            "email": i.email,
            "role": i.role,
            "expires_at": _dt(i.expires_at),
            "accepted_at": _dt(i.accepted_at),
            "created_at": _dt(i.created_at),
        }

    def sched_row(s: PipelineSchedule) -> dict[str, Any]:
        return {
            "id": str(s.id),
            "mapping_id": str(s.mapping_id),
            "cron_expression": s.cron_expression,
            "timezone": s.timezone,
            "is_active": s.is_active,
            "next_run_at": _dt(s.next_run_at),
        }

    def convo_row(c: AgentConversation) -> dict[str, Any]:
        msgs = c.messages
        n = len(msgs) if isinstance(msgs, list) else 0
        return {
            "id": str(c.id),
            "user_id": str(c.user_id) if c.user_id else None,
            "updated_at": _dt(c.updated_at),
            "message_count": n,
        }

    def api_key_row(k: ApiKey) -> dict[str, Any]:
        return {
            "id": str(k.id),
            "name": k.name,
            "key_prefix": k.key_prefix,
            "scope": k.scope,
            "last_used_at": _dt(k.last_used_at),
            "expires_at": _dt(k.expires_at),
            "revoked_at": _dt(k.revoked_at),
            "created_at": _dt(k.created_at),
        }

    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "organization": _org_public(org),
        "users": [_user_public(u) for u in users],
        "connections": [_connection_public(c) for c in conns],
        "schema_mappings": [mapping_row(m) for m in mappings],
        "recommendations": [rec_row(r) for r in recs],
        "invitations": [inv_row(i) for i in invites],
        "pipeline_schedules": [sched_row(s) for s in schedules],
        "agent_conversations": [convo_row(c) for c in convos],
        "api_keys": [api_key_row(k) for k in api_keys],
    }
