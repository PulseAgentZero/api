"""Entivia Studio — SQL editor, visualizations, custom dashboards, and AI tools.

All endpoints require JWT auth (`Authorization: Bearer <token>`).
Roles: admin > manager > analyst > viewer. Most write operations require analyst+.
Destructive operations (delete, embed-token) require admin/manager.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.dependencies.plan_gate import check_studio_dashboard_limit
from app.api.errors import PulseHTTPException, bad_request, conflict, forbidden, not_found
from app.api.schemas.studio import (
    ChartType,
    DashboardExecuteItemResult,
    StudioDashboardAddItemRequest,
    StudioDashboardCreateRequest,
    StudioDashboardExecuteRequest,
    StudioDashboardExecuteResponse,
    StudioDashboardForkRequest,
    StudioDashboardItemResponse,
    StudioDashboardResponse,
    StudioDashboardUpdateRequest,
    StudioEmbedTokenRequest,
    StudioEmbedTokenResponse,
    StudioGenerateSQLIntakeRequest,
    StudioGenerateSQLIntakeResponse,
    StudioGenerateSQLRequest,
    StudioGenerateSQLResponse,
    StudioQueryCreateRequest,
    StudioQueryExplainResponse,
    StudioQueryExecuteRequest,
    StudioQueryResponse,
    StudioQueryResultResponse,
    StudioQueryRunRequest,
    StudioQueryRunResponse,
    StudioQueryUpdateRequest,
    StudioRecommendVizResponse,
    StudioVisualizationCreateRequest,
    StudioVisualizationResponse,
    StudioVisualizationUpdateRequest,
)
from app.infrastructure.audit import log_audit
from app.infrastructure.database.models.studio_query import StudioQuery
from app.infrastructure.database.models.studio_visualization import StudioVisualization
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.studio_dashboard_item_repository import (
    StudioDashboardItemRepository,
)
from app.infrastructure.database.repositories.studio_dashboard_repository import (
    StudioDashboardRepository,
)
from app.infrastructure.database.repositories.studio_query_repository import StudioQueryRepository
from app.infrastructure.database.repositories.studio_query_run_repository import (
    StudioQueryRunRepository,
)
from app.infrastructure.database.repositories.studio_star_repository import StudioStarRepository
from app.infrastructure.database.repositories.studio_visualization_repository import (
    StudioVisualizationRepository,
)
from app.infrastructure.database.session import get_db
from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis.keys import studio_embed, studio_run_result
from app.services.studio_query_service import (
    _is_select_only,
    execute_studio_query,
    extract_param_names,
)
from app.services.studio_time_range import merge_dashboard_param_values
from app.api.schemas.connection import IntrospectResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/studio", tags=["Studio"])

_MAX_DASHBOARD_ITEMS = 20
_SLUG_MAX_ATTEMPTS = 5


def _layout_slot_id(slot: dict) -> str:
    return str(slot.get("item_id", ""))


def _layout_for_db(layout: list) -> list:
    """JSON-serializable layout for JSONB (item_id must be str, not UUID)."""
    out: list = []
    for item in layout:
        if hasattr(item, "model_dump"):
            out.append(item.model_dump(mode="json"))
        else:
            row = dict(item)
            row["item_id"] = str(row["item_id"])
            out.append(row)
    return out


def _next_layout_y(layout: list) -> int:
    if not layout:
        return 0
    return max(int(s.get("y", 0)) + int(s.get("h", 4)) for s in layout)


def _append_layout_slot(layout: list | None, item_id: UUID, panel_type: str) -> list:
    """Append a default grid position for a new dashboard panel."""
    layout = list(layout or [])
    w, h = (12, 3) if panel_type == "text" else (6, 4)
    x, y = 0, _next_layout_y(layout)
    if panel_type != "text" and layout:
        last = layout[-1]
        last_w = int(last.get("w", 6))
        last_x = int(last.get("x", 0))
        last_y = int(last.get("y", 0))
        last_h = int(last.get("h", 4))
        if last_w < 12 and last_x + last_w + w <= 12:
            x, y = last_x + last_w, last_y
    layout.append({
        "item_id": str(item_id),
        "x": x,
        "y": y,
        "w": w,
        "h": h,
    })
    return layout


def _dashboard_viz_error_message(exc: Exception) -> str:
    """User-facing message for a failed dashboard visualization query."""
    if isinstance(exc, PulseHTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("message"):
            return str(detail["message"])
    logger.warning("Dashboard visualization query failed", exc_info=exc)
    return "Query failed. Check the underlying saved query and connection."


def _remove_layout_slot(layout: list | None, item_id: UUID) -> list:
    target = str(item_id)
    return [s for s in (layout or []) if _layout_slot_id(s) != target]


def _remap_layout_item_ids(
    source_layout: list | None,
    source_items: list,
    new_items: list,
) -> list:
    """Rebuild layout after fork when item UUIDs change."""
    old_to_new = {str(old.id): str(new.id) for old, new in zip(source_items, new_items)}
    slots_by_old = {_layout_slot_id(s): s for s in (source_layout or [])}
    layout: list = []
    y = 0
    for old, new in zip(source_items, new_items):
        old_id = str(old.id)
        slot = slots_by_old.get(old_id)
        if slot:
            entry = {**slot, "item_id": str(new.id)}
        else:
            w, h = (12, 3) if getattr(old, "panel_type", "visualization") == "text" else (6, 4)
            entry = {"item_id": str(new.id), "x": 0, "y": y, "w": w, "h": h}
        layout.append(entry)
        y = int(entry.get("y", 0)) + int(entry.get("h", 4))
    return layout


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _generate_slug(name: str, repo: StudioDashboardRepository) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    for _ in range(_SLUG_MAX_ATTEMPTS):
        slug = f"{base}-{secrets.token_hex(2)}"
        if not await repo.slug_exists(slug):
            return slug
    raise conflict("SLUG_CONFLICT", "Could not generate a unique slug — try a different name")


def _assert_owner_or_elevated(resource_created_by: UUID | None, current_user: User) -> None:
    if current_user.role in ("admin", "manager"):
        return
    if resource_created_by is not None and resource_created_by == current_user.id:
        return
    raise forbidden("FORBIDDEN", "You don't have permission to modify this resource")


async def _get_starred_ids(db: AsyncSession, user_id: UUID, resource_type: str) -> set[UUID]:
    return await StudioStarRepository(db).get_starred_ids(user_id, resource_type)


def _apply_starred(response: StudioQueryResponse | StudioDashboardResponse, obj_id: UUID, starred_ids: set[UUID]) -> None:
    response.starred = obj_id in starred_ids


# ── Ad-hoc SQL execution ──────────────────────────────────────────────────────

@router.post("/query/execute", response_model=StudioQueryResultResponse)
async def execute_query(
    body: StudioQueryExecuteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudioQueryResultResponse:
    """Execute a one-off SQL query against the org's connected database.

    **Roles required:** any authenticated user

    **Request:**
    - `sql_text` — SELECT statement (max 50,000 chars). Use `{{param_name}}` placeholders.
    - `connection_id` — optional; defaults to the org's primary connection.
    - `param_values` — dict of `{name: value}` for any `{{placeholders}}`.
    - `page` / `page_size` — pagination (default 1 / 100, max page_size 1000).

    **Response:** `{rows, columns, total, page, page_size, cached}`

    **Errors:**
    - 400 INVALID_SQL — not a SELECT statement or contains DML/DDL.
    - 400 CLIENT_DB_ERROR — could not connect or query failed.
    - 400 PARAM_MISSING — a placeholder has no value and no default.
    - 429 RATE_LIMITED — daily execution budget exceeded (free plan).
    """
    from app.api.dependencies.plan_gate import get_org_plan

    redis = await get_redis()
    org_plan = await get_org_plan(db, current_user.org_id)
    result = await execute_studio_query(
        db, current_user.org_id, body.connection_id, body.sql_text,
        param_defs=[], param_values=body.param_values,
        page=body.page, page_size=body.page_size, redis=redis, org_plan=org_plan,
    )
    await log_audit(
        db, org_id=current_user.org_id, user_id=current_user.id,
        action="studio.query_execute", resource="studio_query",
        metadata={"row_count": result["total"]},
    )
    return StudioQueryResultResponse(**result)


# ── Schema browser ───────────────────────────────────────────────────────────

_SCHEMA_CACHE_TTL = 1800  # 30 minutes
_SCHEMA_CACHE_PREFIX = "studio:schema:"


async def _fetch_live_schema(
    db: AsyncSession, org_id: UUID, connection_id: UUID | None
) -> IntrospectResponse:
    """Fetch tables + columns directly from the client DB or file sources."""
    from app.infrastructure.connectors.payload import parse_pulse_api_payload
    from app.infrastructure.crypto import decrypt_dsn
    from app.infrastructure.database.connection_tester import introspect_schema
    from app.services.studio_file_source_service import (
        fetch_file_source_schema,
        get_connection_for_studio,
        supports_studio_file_queries,
    )
    from app.services.studio_query_service import _get_specific_engine

    conn_row = await get_connection_for_studio(db, org_id, connection_id)
    if supports_studio_file_queries(conn_row):
        tables = await fetch_file_source_schema(conn_row)
        return IntrospectResponse(tables=tables)

    if not conn_row.encrypted_dsn:
        raise not_found(
            "Connection has no schema to browse — re-create or test the connection first"
        )

    dsn_plain = decrypt_dsn(conn_row.encrypted_dsn)
    if parse_pulse_api_payload(dsn_plain) is not None:
        tables = await introspect_schema(dsn_plain, sslmode=conn_row.sslmode)
    else:
        engine, conn = await _get_specific_engine(db, org_id, conn_row.id)
        try:
            tables = await introspect_schema(dsn_plain, sslmode=conn.sslmode)
        finally:
            await engine.dispose()

    return IntrospectResponse(tables=tables)


@router.get("/schema", response_model=IntrospectResponse, summary="Browse client database schema")
async def get_schema(
    connection_id: UUID | None = Query(None, description="Specific connection ID; defaults to the org's primary connection"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IntrospectResponse:
    """Return all tables and columns from the org's connected client database.

    **Roles required:** any authenticated user

    Used by the Studio SQL editor to power schema autocomplete and table/column browsers.

    **How it works:**
    1. Checks Redis cache first (TTL 30 minutes). Cache key is per-org + per-connection.
    2. On cache miss, falls back to the `raw_schema` stored in `schema_mappings` (set during onboarding).
    3. If neither is available, queries the live client DB directly.

    **Query params:**
    - `connection_id` — optional. If omitted, uses the org's primary active connection.

    **Response:** `{tables: [{name, columns: [{name, data_type, nullable}]}]}`

    **Errors:**
    - 400 CLIENT_DB_ERROR — could not connect to the client database.
    - 404 — no connection configured for this org.
    """
    redis = await get_redis()
    cache_key = f"{_SCHEMA_CACHE_PREFIX}{current_user.org_id}:{connection_id or 'primary'}"

    # 1. Redis cache
    if redis is not None:
        try:
            cached = await redis.get(cache_key)
            if cached:
                return IntrospectResponse.model_validate_json(cached)
        except Exception:
            pass

    # 2. raw_schema stored in schema_mappings (set during onboarding/pipeline runs)
    if connection_id is None:
        from app.infrastructure.database.repositories.schema_mapping_repository import SchemaMappingRepository
        mappings = await SchemaMappingRepository(db).list_by_org(current_user.org_id)
        if mappings:
            raw = (mappings[0].raw_schema or {}).get("tables") or []
            if raw:
                result = IntrospectResponse.model_validate({"tables": raw})
                if redis is not None:
                    try:
                        await redis.set(cache_key, result.model_dump_json(), ex=_SCHEMA_CACHE_TTL)
                    except Exception:
                        pass
                return result

    # 3. Live query
    try:
        result = await _fetch_live_schema(db, current_user.org_id, connection_id)
    except Exception as exc:
        from app.api.safe_errors import log_and_bad_request

        raise log_and_bad_request("CLIENT_DB_ERROR", exc) from exc

    if redis is not None:
        try:
            await redis.set(cache_key, result.model_dump_json(), ex=_SCHEMA_CACHE_TTL)
        except Exception:
            pass

    return result


@router.post("/schema/refresh", response_model=IntrospectResponse, summary="Refresh client database schema")
async def refresh_schema(
    connection_id: UUID | None = Query(None, description="Specific connection ID; defaults to the org's primary connection"),
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> IntrospectResponse:
    """Force a fresh schema read from the client database, bypassing all caches.

    **Roles required:** analyst, manager, admin

    Call this after adding new tables or columns to the client database so the
    Studio SQL editor picks up the changes immediately.

    Clears the Redis cache for this connection, queries the live DB, then repopulates the cache.

    **Response:** Fresh `{tables: [{name, columns: [{name, data_type, nullable}]}]}`

    **Errors:**
    - 400 CLIENT_DB_ERROR — could not connect to the client database.
    - 404 — no connection configured for this org.
    """
    redis = await get_redis()
    cache_key = f"{_SCHEMA_CACHE_PREFIX}{current_user.org_id}:{connection_id or 'primary'}"

    # Clear cache first
    if redis is not None:
        try:
            await redis.delete(cache_key)
        except Exception:
            pass

    # Live fetch
    try:
        result = await _fetch_live_schema(db, current_user.org_id, connection_id)
    except Exception as exc:
        from app.api.safe_errors import log_and_bad_request

        raise log_and_bad_request("CLIENT_DB_ERROR", exc) from exc

    # Repopulate cache
    if redis is not None:
        try:
            await redis.set(cache_key, result.model_dump_json(), ex=_SCHEMA_CACHE_TTL)
        except Exception:
            pass

    return result


# ── Saved queries ─────────────────────────────────────────────────────────────

@router.get("/queries", summary="List saved queries")
async def list_queries(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    q: str | None = Query(None, description="Search by name or description"),
    tags: str | None = Query(None, description="Comma-separated tag filter"),
    starred: bool = Query(False, description="Return only starred queries"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List saved queries for the org with optional search, tag, and star filtering.

    **Roles required:** any authenticated user

    **Query params:**
    - `q` — search term matched against name and description.
    - `tags` — comma-separated tags, e.g. `finance,revenue`. All tags must match.
    - `starred` — if true, returns only queries the calling user has starred.
    - `page` / `limit` — pagination.

    **Response:** `{queries: [...], total, page}`
    """
    repo = StudioQueryRepository(db)
    offset = (page - 1) * limit
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    starred_ids: set[UUID] | None = None
    if starred:
        starred_ids = await _get_starred_ids(db, current_user.id, "query")

    queries = await repo.search(
        current_user.org_id, q=q, tags=tag_list, starred_ids=starred_ids,
        limit=limit, offset=offset,
    )
    total = await repo.search_count(
        current_user.org_id, q=q, tags=tag_list, starred_ids=starred_ids
    )
    all_starred = await _get_starred_ids(db, current_user.id, "query")
    results = []
    for qobj in queries:
        r = StudioQueryResponse.model_validate(qobj)
        r.starred = qobj.id in all_starred
        results.append(r)
    return {"queries": results, "total": total, "page": page}


@router.post(
    "/queries/generate/intake",
    response_model=StudioGenerateSQLIntakeResponse,
    summary="Intake questions before AI SQL generation",
)
async def generate_sql_intake(
    body: StudioGenerateSQLIntakeRequest,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> StudioGenerateSQLIntakeResponse:
    """Return connections, schema preview, and clarifying questions (no SQL generated)."""
    from app.services.studio_ai_service import generate_sql_intake as run_intake

    result = await run_intake(
        db, current_user.org_id, body.goal, body.connection_id,
    )
    return StudioGenerateSQLIntakeResponse(**result)


@router.post("/queries/generate", response_model=StudioGenerateSQLResponse, summary="Generate SQL from natural language (AI)")
async def generate_sql(
    body: StudioGenerateSQLRequest,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> StudioGenerateSQLResponse:
    """Generate a SQL query from a plain-English goal using AI.

    **Roles required:** analyst, manager, admin

    **Request:**
    - `goal` — natural language description, e.g. "top 10 customers by revenue last quarter".
    - `connection_id` — optional; uses the org's primary connection if omitted.

    **Response:** `{sql, explanation, params}` — the SQL is NOT saved. Review and save it manually.

    **Errors:**
    - 400 AI_NOT_CONFIGURED — Anthropic API key not set.
    - 400 CLIENT_DB_ERROR — could not connect to the database to read schema.
    - 400 NO_SCHEMA — no tables found in the database.
    """
    from app.services.studio_ai_service import generate_sql_from_goal
    result = await generate_sql_from_goal(
        db,
        current_user.org_id,
        body.goal,
        body.connection_id,
        time_window=body.time_window,
        segments=body.segments,
        filters_to_parameterize=body.filters_to_parameterize,
        extra_context=body.extra_context,
    )
    return StudioGenerateSQLResponse(**result)


@router.post("/queries", status_code=201, response_model=StudioQueryResponse, summary="Save a query")
async def create_query(
    body: StudioQueryCreateRequest,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> StudioQueryResponse:
    """Save a SQL query with optional parameters and auto-refresh schedule.

    **Roles required:** analyst, manager, admin

    **Request:**
    - `sql_text` — must be a SELECT statement. Use `{{param_name}}` for parameters.
    - `params` — declare each placeholder: `[{name, type, default_value, label}]`.
    - `tags` — list of tag strings for organization (max 20).
    - `refresh_cron` — standard cron expression for server-side cache warming, e.g. `"0 */6 * * *"`.
    - `refresh_enabled` — set true to activate cron refresh (picked up by the scheduler process within ~5 minutes).

    **Errors:**
    - 400 INVALID_SQL — not a SELECT.
    - 400 PARAM_UNDECLARED — SQL contains `{{name}}` not listed in `params`.
    """
    if not _is_select_only(body.sql_text):
        raise bad_request("INVALID_SQL", "Only SELECT statements can be saved.")
    declared = {p.name for p in body.params}
    used = set(extract_param_names(body.sql_text))
    if used - declared:
        raise bad_request(
            "PARAM_UNDECLARED",
            f"SQL contains undeclared parameters: {sorted(used - declared)}. Add them to params.",
        )
    q = await StudioQueryRepository(db).create(
        current_user.org_id, current_user.id,
        name=body.name, description=body.description,
        sql_text=body.sql_text, connection_id=body.connection_id,
        params=[p.model_dump() for p in body.params],
        refresh_cron=body.refresh_cron, refresh_enabled=body.refresh_enabled,
        tags=body.tags,
    )
    await db.commit()
    await db.refresh(q)
    return StudioQueryResponse.model_validate(q)


@router.get("/queries/{query_id}", response_model=StudioQueryResponse, summary="Get a saved query")
async def get_query(
    query_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudioQueryResponse:
    """Fetch a single saved query by ID.

    **Roles required:** any authenticated user

    **Response:** Full query object including `params`, `tags`, `refresh_cron`.

    **Errors:** 404 — query not found or belongs to a different org.
    """
    q = await StudioQueryRepository(db).get_by_id_and_org(query_id, current_user.org_id)
    if not q:
        raise not_found("Query not found")
    starred_ids = await _get_starred_ids(db, current_user.id, "query")
    r = StudioQueryResponse.model_validate(q)
    r.starred = q.id in starred_ids
    return r


@router.patch("/queries/{query_id}", response_model=StudioQueryResponse, summary="Update a saved query")
async def update_query(
    query_id: UUID,
    body: StudioQueryUpdateRequest,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> StudioQueryResponse:
    """Update a saved query's SQL, name, params, tags, or schedule.

    **Roles required:** analyst (own queries only), manager/admin (any query)

    All fields are optional — only provided fields are updated.
    Changing `refresh_cron` or `refresh_enabled` is applied by the scheduler process within ~5 minutes.

    **Errors:**
    - 403 FORBIDDEN — analyst trying to edit another user's query.
    - 400 INVALID_SQL — updated SQL is not a SELECT.
    - 400 PARAM_UNDECLARED — SQL placeholders not all declared.
    """
    repo = StudioQueryRepository(db)
    q = await repo.get_by_id_and_org(query_id, current_user.org_id)
    if not q:
        raise not_found("Query not found")
    _assert_owner_or_elevated(q.created_by, current_user)

    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if body.sql_text is not None:
        if not _is_select_only(body.sql_text):
            raise bad_request("INVALID_SQL", "Only SELECT statements are allowed.")
        updates["sql_text"] = body.sql_text
    if body.connection_id is not None:
        updates["connection_id"] = body.connection_id
    if body.params is not None:
        effective_sql = body.sql_text or q.sql_text
        declared = {p.name for p in body.params}
        used = set(extract_param_names(effective_sql))
        if used - declared:
            raise bad_request("PARAM_UNDECLARED", f"SQL contains undeclared parameters: {sorted(used - declared)}.")
        updates["params"] = [p.model_dump() for p in body.params]
    if body.tags is not None:
        updates["tags"] = body.tags
    if body.refresh_cron is not None:
        updates["refresh_cron"] = body.refresh_cron
    if body.refresh_enabled is not None:
        updates["refresh_enabled"] = body.refresh_enabled

    if updates:
        q = await repo.update(q, **updates)

    await db.commit()
    await db.refresh(q)

    return StudioQueryResponse.model_validate(q)


@router.delete("/queries/{query_id}", status_code=204, summary="Delete a saved query")
async def delete_query(
    query_id: UUID,
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Permanently delete a saved query and all its visualizations.

    **Roles required:** manager, admin

    Cascade: visualizations linked to this query are also deleted via DB cascade.
    """
    q = await StudioQueryRepository(db).get_by_id_and_org(query_id, current_user.org_id)
    if not q:
        raise not_found("Query not found")
    await StudioQueryRepository(db).delete(q)
    await db.commit()


@router.post("/queries/{query_id}/run", response_model=StudioQueryRunResponse, status_code=202, summary="Run a saved query (async)")
async def run_saved_query(
    query_id: UUID,
    body: StudioQueryRunRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudioQueryRunResponse:
    """Enqueue a saved query for async execution via the background worker.

    **Roles required:** any authenticated user

    Returns immediately with `{id, status: "pending"}`. Poll `GET /studio/runs/{id}`
    until `status` is `completed` or `failed`.

    **Request body (optional):**
    - `param_values` — override param defaults for this run.
    - `page` / `page_size` — for the result returned at poll time.

    **Fallback:** If Redis is unavailable, executes synchronously and returns `status: "completed"` with results inline.

    **Errors:** 404 — query not found.
    """
    from app.services.pipeline_queue import enqueue_studio_query_job

    repo = StudioQueryRepository(db)
    q = await repo.get_by_id_and_org(query_id, current_user.org_id)
    if not q:
        raise not_found("Query not found")

    run_body = body or StudioQueryRunRequest()
    redis = await get_redis()
    run_repo = StudioQueryRunRepository(db)
    run = await run_repo.create(current_user.org_id, query_id, current_user.id, run_body.param_values)
    await db.commit()
    await db.refresh(run)

    queued = await enqueue_studio_query_job(
        run_id=run.id, query_id=query_id,
        org_id=current_user.org_id, param_values=run_body.param_values,
    )

    if not queued:
        from app.api.dependencies.plan_gate import get_org_plan

        org_plan = await get_org_plan(db, current_user.org_id)
        await run_repo.mark_running(run)
        try:
            result = await execute_studio_query(
                db, current_user.org_id, q.connection_id, q.sql_text,
                param_defs=q.params or [], param_values=run_body.param_values,
                page=1, page_size=5000, redis=None, org_plan=org_plan,
            )
            await run_repo.mark_completed(run, result["total"])
            await repo.touch_last_run(q, result["total"])
            await db.commit()
            await db.refresh(run)
            resp = StudioQueryRunResponse.model_validate(run)
            resp.result = StudioQueryResultResponse(**result)
            return resp
        except Exception as exc:
            await run_repo.mark_failed(run, str(exc))
            await db.commit()
            await db.refresh(run)
            return StudioQueryRunResponse.model_validate(run)

    return StudioQueryRunResponse.model_validate(run)


@router.get("/runs/{run_id}", response_model=StudioQueryRunResponse, summary="Poll a query run")
async def get_query_run(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudioQueryRunResponse:
    """Poll the status of an async query run. Returns results when completed.

    **Roles required:** any authenticated user

    **Response statuses:**
    - `pending` — queued, not started yet.
    - `running` — worker is executing the query.
    - `completed` — results are available in the `result` field (valid for 1 hour).
    - `failed` — check the `error` field.

    Results are fetched from Redis and included in the `result` field when `completed`.
    Results expire after 1 hour — re-run the query if needed.
    """
    run = await StudioQueryRunRepository(db).get_by_id_and_org(run_id, current_user.org_id)
    if not run:
        raise not_found("Query run not found")
    response = StudioQueryRunResponse.model_validate(run)
    if run.status == "completed":
        redis = await get_redis()
        if redis is not None:
            try:
                raw = await redis.get(studio_run_result(str(run_id)))
                if raw:
                    response.result = StudioQueryResultResponse(**json.loads(raw))
            except Exception:
                logger.warning("Could not fetch run result from Redis for run=%s", run_id)
    return response


@router.get("/runs/{run_id}/download", summary="Download query run results")
async def download_run_results(
    run_id: UUID,
    format: str = Query("csv", pattern="^(csv|json)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream query run results as a CSV or newline-delimited JSON file.

    **Roles required:** any authenticated user

    **Query params:**
    - `format` — `csv` (default) or `json` (newline-delimited JSON, one object per line).

    Results are read from Redis — they expire 1 hour after the run completes.

    **Errors:**
    - 400 RUN_NOT_COMPLETE — run has not finished yet.
    - 404 — run not found or results have expired.
    """
    run = await StudioQueryRunRepository(db).get_by_id_and_org(run_id, current_user.org_id)
    if not run:
        raise not_found("Query run not found")
    if run.status != "completed":
        raise bad_request("RUN_NOT_COMPLETE", "Query run has not completed yet")

    redis = await get_redis()
    if redis is None:
        raise not_found("Result not available — Redis is not configured")
    raw = await redis.get(studio_run_result(str(run_id)))
    if not raw:
        raise not_found("Results have expired. Re-run the query to regenerate them.")

    data = json.loads(raw)
    rows: list[dict] = data.get("rows", [])
    columns: list[str] = data.get("columns", [])

    if format == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="run_{run_id}.csv"'},
        )
    else:
        def _ndjson():
            for row in rows:
                yield json.dumps(row, default=str) + "\n"
        return StreamingResponse(
            _ndjson(),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="run_{run_id}.ndjson"'},
        )


@router.get("/queries/{query_id}/runs", summary="List execution history for a query")
async def list_query_runs(
    query_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the execution history for a saved query (most recent first).

    **Roles required:** any authenticated user

    Useful for debugging scheduled refresh failures.
    """
    q = await StudioQueryRepository(db).get_by_id_and_org(query_id, current_user.org_id)
    if not q:
        raise not_found("Query not found")
    runs = await StudioQueryRunRepository(db).list_by_query(query_id, current_user.org_id, limit=limit)
    return {"runs": [StudioQueryRunResponse.model_validate(r) for r in runs]}


# ── Star / Unstar ─────────────────────────────────────────────────────────────

@router.post("/queries/{query_id}/star", status_code=204, summary="Star a query")
async def star_query(
    query_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Mark a query as starred (favorited). Idempotent — safe to call multiple times.

    **Roles required:** any authenticated user
    """
    q = await StudioQueryRepository(db).get_by_id_and_org(query_id, current_user.org_id)
    if not q:
        raise not_found("Query not found")
    await StudioStarRepository(db).upsert(current_user.id, current_user.org_id, "query", query_id)
    await db.commit()


@router.delete("/queries/{query_id}/star", status_code=204, summary="Unstar a query")
async def unstar_query(
    query_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a star from a query. Idempotent — safe to call even if not starred.

    **Roles required:** any authenticated user
    """
    await StudioStarRepository(db).delete(current_user.id, "query", query_id)
    await db.commit()


# ── AI query tools ────────────────────────────────────────────────────────────

@router.get("/queries/{query_id}/explain", response_model=StudioQueryExplainResponse, summary="Explain a query in plain English (AI)")
async def explain_query(
    query_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudioQueryExplainResponse:
    """Get a plain-English explanation of what a saved query does.

    **Roles required:** any authenticated user

    Uses the fast Haiku model — typically responds in under 2 seconds.

    **Errors:**
    - 400 AI_NOT_CONFIGURED — Anthropic API key not set.
    - 404 — query not found.
    """
    from app.services.studio_ai_service import explain_query as _explain
    q = await StudioQueryRepository(db).get_by_id_and_org(query_id, current_user.org_id)
    if not q:
        raise not_found("Query not found")
    explanation = await _explain(q.sql_text)
    return StudioQueryExplainResponse(explanation=explanation)


@router.post("/queries/{query_id}/recommend-viz", response_model=StudioRecommendVizResponse, summary="Recommend a chart type (AI)")
async def recommend_viz(
    query_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudioRecommendVizResponse:
    """Suggest the best chart type and axis config for a saved query.

    **Roles required:** any authenticated user

    Runs the query (or uses cached results), analyzes column types and cardinality,
    then asks the AI to recommend a visualization.

    **Response:** `{chart_type, config, reasoning}`

    **Errors:**
    - 400 AI_NOT_CONFIGURED — Anthropic API key not set.
    - 400 CLIENT_DB_ERROR — could not execute the query to analyze results.
    - 404 — query not found.
    """
    from app.services.studio_ai_service import recommend_visualization
    redis = await get_redis()
    result = await recommend_visualization(db, current_user.org_id, query_id, redis=redis)
    return StudioRecommendVizResponse(**result)


# ── Visualizations ────────────────────────────────────────────────────────────

@router.get("/visualizations", summary="List org visualizations or bulk-fetch by IDs")
async def list_org_visualizations(
    ids: str | None = Query(None, description="Comma-separated visualization UUIDs"),
    query_id: UUID | None = Query(None, description="Filter by parent query"),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List visualizations for the org, or fetch specific IDs in one request.

    When `ids` is provided, returns only matching visualizations (unknown IDs are omitted).
    Otherwise returns a paginated org-wide list, optionally filtered by `query_id`.
    """
    repo = StudioVisualizationRepository(db)
    if ids:
        id_list: list[UUID] = []
        for part in ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                id_list.append(UUID(part))
            except ValueError:
                continue
        vizs = await repo.list_by_ids(current_user.org_id, id_list)
        return {"visualizations": [StudioVisualizationResponse.model_validate(v) for v in vizs]}
    offset = (page - 1) * limit
    vizs = await repo.list_by_org(
        current_user.org_id,
        limit=limit,
        offset=offset,
        query_id=query_id,
    )
    return {"visualizations": [StudioVisualizationResponse.model_validate(v) for v in vizs]}


@router.get("/queries/{query_id}/visualizations", summary="List visualizations for a query")
async def list_visualizations(
    query_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List all visualizations attached to a saved query.

    **Roles required:** any authenticated user
    """
    q = await StudioQueryRepository(db).get_by_id_and_org(query_id, current_user.org_id)
    if not q:
        raise not_found("Query not found")
    vizs = await StudioVisualizationRepository(db).list_by_query(query_id, current_user.org_id)
    return {"visualizations": [StudioVisualizationResponse.model_validate(v) for v in vizs]}


@router.post("/queries/{query_id}/visualizations", status_code=201, response_model=StudioVisualizationResponse, summary="Add a visualization to a query")
async def create_visualization(
    query_id: UUID,
    body: StudioVisualizationCreateRequest,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> StudioVisualizationResponse:
    """Create a chart/visualization for a saved query.

    **Roles required:** analyst, manager, admin

    **Request:**
    - `chart_type` — one of: bar, line, area, pie, scatter, table, number, funnel, heatmap, gauge, waterfall, trend.
    - `config` — `{x_axis, y_axis, color, title, value_column, label_column}`.
    - `column_formats` — per-column display rules: `{"amount": {"type": "currency", "symbol": "$"}}`.

    **Errors:** 404 — parent query not found.
    """
    q = await StudioQueryRepository(db).get_by_id_and_org(query_id, current_user.org_id)
    if not q:
        raise not_found("Query not found")
    viz = await StudioVisualizationRepository(db).create(
        current_user.org_id, query_id, current_user.id,
        name=body.name, chart_type=body.chart_type,
        config=body.config.model_dump(exclude_none=True),
        column_formats={k: v.model_dump(exclude_none=True) for k, v in body.column_formats.items()},
    )
    await db.commit()
    await db.refresh(viz)
    return StudioVisualizationResponse.model_validate(viz)


@router.patch("/queries/{query_id}/visualizations/{viz_id}", response_model=StudioVisualizationResponse, summary="Update a visualization")
async def update_visualization(
    query_id: UUID,
    viz_id: UUID,
    body: StudioVisualizationUpdateRequest,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> StudioVisualizationResponse:
    """Update a visualization's chart type, config, or column formats.

    **Roles required:** analyst (own visualizations only), manager/admin (any)

    All fields are optional — only provided fields are updated.
    """
    viz_repo = StudioVisualizationRepository(db)
    viz = await viz_repo.get_by_id_and_org(viz_id, current_user.org_id)
    if not viz or viz.query_id != query_id:
        raise not_found("Visualization not found")
    _assert_owner_or_elevated(viz.created_by, current_user)

    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.chart_type is not None:
        updates["chart_type"] = body.chart_type
    if body.config is not None:
        updates["config"] = body.config.model_dump(exclude_none=True)
    if body.column_formats is not None:
        updates["column_formats"] = {k: v.model_dump(exclude_none=True) for k, v in body.column_formats.items()}

    if updates:
        viz = await viz_repo.update(viz, **updates)

    await db.commit()
    await db.refresh(viz)
    return StudioVisualizationResponse.model_validate(viz)


@router.delete("/queries/{query_id}/visualizations/{viz_id}", status_code=204, summary="Delete a visualization")
async def delete_visualization(
    query_id: UUID,
    viz_id: UUID,
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a visualization. Dashboard items pointing to it will show as empty panels.

    **Roles required:** manager, admin
    """
    viz = await StudioVisualizationRepository(db).get_by_id_and_org(viz_id, current_user.org_id)
    if not viz or viz.query_id != query_id:
        raise not_found("Visualization not found")
    await StudioVisualizationRepository(db).delete(viz)
    await db.commit()


# ── Dashboards ────────────────────────────────────────────────────────────────

@router.get("/dashboards", summary="List dashboards")
async def list_dashboards(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    q: str | None = Query(None, description="Search by name or description"),
    tags: str | None = Query(None, description="Comma-separated tag filter"),
    starred: bool = Query(False, description="Return only starred dashboards"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List dashboards for the org with optional search, tag, and star filtering.

    **Roles required:** any authenticated user

    **Response:** `{dashboards: [...], total, page}`
    """
    repo = StudioDashboardRepository(db)
    offset = (page - 1) * limit
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    starred_ids: set[UUID] | None = None
    if starred:
        starred_ids = await _get_starred_ids(db, current_user.id, "dashboard")

    dashboards = await repo.search(
        current_user.org_id, q=q, tags=tag_list, starred_ids=starred_ids,
        limit=limit, offset=offset,
    )
    total = await repo.search_count(current_user.org_id, q=q, tags=tag_list, starred_ids=starred_ids)
    all_starred = await _get_starred_ids(db, current_user.id, "dashboard")
    results = []
    for d in dashboards:
        r = StudioDashboardResponse.model_validate(d)
        r.starred = d.id in all_starred
        results.append(r)
    return {"dashboards": results, "total": total, "page": page}


@router.post("/dashboards", status_code=201, response_model=StudioDashboardResponse, summary="Create a dashboard")
async def create_dashboard(
    body: StudioDashboardCreateRequest,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> StudioDashboardResponse:
    """Create a new empty dashboard.

    **Roles required:** analyst, manager, admin

    **Request:**
    - `name` — display name.
    - `is_public` — if true, generates a shareable slug URL immediately.
    - `dashboard_params` — global filter definitions for the dashboard (same format as query params).
      These appear as filter inputs when viewing the dashboard.
    - `tags` — list of tag strings.
    - `layout` — initial grid layout (can be set/updated later via PATCH).

    **Plan limits:** Free plan allows 5 dashboards. Pro and self-hosted are unlimited.

    **Errors:**
    - 402 PLAN_LIMIT_REACHED — dashboard limit reached on free plan.
    - 409 SLUG_CONFLICT — could not generate a unique slug (try a different name).
    """
    await check_studio_dashboard_limit(db, current_user.org_id)
    repo = StudioDashboardRepository(db)
    slug: str | None = None
    if body.is_public:
        slug = await _generate_slug(body.name, repo)

    time_range_dict = (
        body.time_range.model_dump(by_alias=True) if body.time_range else {}
    )
    dashboard = await repo.create(
        current_user.org_id, current_user.id,
        name=body.name, description=body.description,
        is_public=body.is_public, slug=slug,
        layout=_layout_for_db(body.layout),
        dashboard_params=[p.model_dump() for p in body.dashboard_params],
        tags=body.tags,
        refresh_interval_seconds=body.refresh_interval_seconds,
        time_range=time_range_dict,
    )
    await db.commit()
    await db.refresh(dashboard)
    return StudioDashboardResponse.model_validate(dashboard)


@router.get("/dashboards/{dashboard_id}", response_model=StudioDashboardResponse, summary="Get a dashboard")
async def get_dashboard(
    dashboard_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudioDashboardResponse:
    """Fetch a dashboard with all its items (visualization panels and text panels).

    **Roles required:** any authenticated user

    **Response:** Full dashboard object with `items` array populated.
    Each item includes `panel_type` ("visualization" or "text") and optionally `content`.
    """
    dashboard = await StudioDashboardRepository(db).get_by_id_and_org(dashboard_id, current_user.org_id)
    if not dashboard:
        raise not_found("Dashboard not found")
    items = await StudioDashboardItemRepository(db).list_by_dashboard(dashboard_id, current_user.org_id)
    starred_ids = await _get_starred_ids(db, current_user.id, "dashboard")
    response = StudioDashboardResponse.model_validate(dashboard)
    response.items = [StudioDashboardItemResponse.model_validate(i) for i in items]
    response.starred = dashboard_id in starred_ids
    return response


@router.post("/dashboards/{dashboard_id}/execute", response_model=StudioDashboardExecuteResponse, summary="Run all dashboard charts with filter values")
async def execute_dashboard(
    dashboard_id: UUID,
    body: StudioDashboardExecuteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudioDashboardExecuteResponse:
    """Execute all visualizations on a dashboard with dashboard-level filter values.

    **Roles required:** any authenticated user

    This is the primary endpoint for applying dashboard filters. Pass `param_values` matching
    the dashboard's `dashboard_params` definitions — they override each query's defaults.
    The dashboard `time_range` (or optional `time_range` in the request body) injects
    `__time_from` and `__time_to` into every query.

    **Request:**
    - `param_values` — dict of `{param_name: value}` matching the dashboard's filter definitions.
    - `time_range` — optional override of the dashboard's saved time range preset.

    **Response:**
    - `results` — one entry per visualization: `{visualization_id, result | null, error | null}`.
      Individual query failures do not fail the whole response — check `error` per item.

    **Errors:** 404 — dashboard not found.
    """
    dashboard = await StudioDashboardRepository(db).get_by_id_and_org(dashboard_id, current_user.org_id)
    if not dashboard:
        raise not_found("Dashboard not found")

    from app.api.dependencies.plan_gate import get_org_plan

    items = await StudioDashboardItemRepository(db).list_by_dashboard(dashboard_id, current_user.org_id)
    redis = await get_redis()
    org_plan = await get_org_plan(db, current_user.org_id)

    effective_time_range = (
        body.time_range.model_dump(by_alias=True)
        if body.time_range is not None
        else (dashboard.time_range or {})
    )
    merged_params = merge_dashboard_param_values(body.param_values, effective_time_range)

    results: list[DashboardExecuteItemResult] = []

    for item in items:
        if item.panel_type != "visualization" or not item.visualization_id:
            continue
        viz: StudioVisualization | None = await db.get(StudioVisualization, item.visualization_id)
        if not viz:
            continue
        q: StudioQuery | None = await db.get(StudioQuery, viz.query_id)
        if not q:
            continue
        try:
            raw = await execute_studio_query(
                db, current_user.org_id, q.connection_id, q.sql_text,
                param_defs=q.params or [], param_values=merged_params,
                page=1, page_size=500, redis=redis, org_plan=org_plan,
            )
            results.append(DashboardExecuteItemResult(
                visualization_id=item.visualization_id,
                result=StudioQueryResultResponse(**raw),
            ))
        except Exception as exc:
            results.append(DashboardExecuteItemResult(
                visualization_id=item.visualization_id,
                error=_dashboard_viz_error_message(exc),
            ))

    return StudioDashboardExecuteResponse(results=results)


@router.patch("/dashboards/{dashboard_id}", response_model=StudioDashboardResponse, summary="Update a dashboard")
async def update_dashboard(
    dashboard_id: UUID,
    body: StudioDashboardUpdateRequest,
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> StudioDashboardResponse:
    """Update dashboard metadata, layout, filters, tags, or visibility.

    **Roles required:** manager, admin

    Setting `is_public: true` for the first time auto-generates a shareable slug.
    Setting `dashboard_params` defines the filter inputs shown on the dashboard.

    **Errors:**
    - 404 — dashboard not found.
    - 409 SLUG_CONFLICT — unique slug could not be generated.
    """
    repo = StudioDashboardRepository(db)
    dashboard = await repo.get_by_id_and_org(dashboard_id, current_user.org_id)
    if not dashboard:
        raise not_found("Dashboard not found")

    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if body.layout is not None:
        updates["layout"] = _layout_for_db(body.layout)
    if body.dashboard_params is not None:
        updates["dashboard_params"] = [p.model_dump() for p in body.dashboard_params]
    if body.tags is not None:
        updates["tags"] = body.tags
    if "refresh_interval_seconds" in body.model_fields_set:
        updates["refresh_interval_seconds"] = body.refresh_interval_seconds
    if body.time_range is not None:
        updates["time_range"] = body.time_range.model_dump(by_alias=True)
    if body.is_public is not None:
        updates["is_public"] = body.is_public
        if body.is_public and not dashboard.slug:
            updates["slug"] = await _generate_slug(body.name or dashboard.name, repo)

    if updates:
        dashboard = await repo.update(dashboard, **updates)

    await db.commit()
    await db.refresh(dashboard)
    return StudioDashboardResponse.model_validate(dashboard)


@router.delete("/dashboards/{dashboard_id}", status_code=204, summary="Delete a dashboard")
async def delete_dashboard(
    dashboard_id: UUID,
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Permanently delete a dashboard and all its items.

    **Roles required:** manager, admin

    The underlying queries and visualizations are NOT deleted — only the dashboard structure.
    """
    dashboard = await StudioDashboardRepository(db).get_by_id_and_org(dashboard_id, current_user.org_id)
    if not dashboard:
        raise not_found("Dashboard not found")
    await StudioDashboardRepository(db).delete(dashboard)
    await db.commit()


@router.post("/dashboards/{dashboard_id}/fork", status_code=201, response_model=StudioDashboardResponse, summary="Fork a dashboard")
async def fork_dashboard(
    dashboard_id: UUID,
    body: StudioDashboardForkRequest,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> StudioDashboardResponse:
    """Create a copy of a dashboard under the current user's ownership.

    **Roles required:** analyst, manager, admin

    The forked dashboard is always private (is_public=false) regardless of the source.
    The underlying queries and visualizations are shared — not duplicated.

    **Plan limits:** The fork counts toward the org's dashboard limit.

    **Errors:**
    - 404 — source dashboard not found.
    - 402 PLAN_LIMIT_REACHED — dashboard limit reached.
    """
    repo = StudioDashboardRepository(db)
    source = await repo.get_by_id_and_org(dashboard_id, current_user.org_id)
    if not source:
        raise not_found("Dashboard not found")

    await check_studio_dashboard_limit(db, current_user.org_id)
    new_name = body.name or f"Copy of {source.name}"

    new_dashboard = await repo.create(
        current_user.org_id, current_user.id,
        name=new_name, description=source.description,
        is_public=False, slug=None,
        layout=source.layout,
        dashboard_params=source.dashboard_params,
        tags=source.tags,
        refresh_interval_seconds=source.refresh_interval_seconds,
        time_range=source.time_range or {},
    )

    items = await StudioDashboardItemRepository(db).list_by_dashboard(dashboard_id, current_user.org_id)
    item_repo = StudioDashboardItemRepository(db)
    forked_items = []
    for item in items:
        forked_items.append(
            await item_repo.create(
                current_user.org_id,
                new_dashboard.id,
                item.visualization_id,
                item.position,
                panel_type=item.panel_type,
                content=item.content,
            )
        )
    await repo.update(
        new_dashboard,
        layout=_remap_layout_item_ids(source.layout, items, forked_items),
    )

    await log_audit(
        db, org_id=current_user.org_id, user_id=current_user.id,
        action="studio.dashboard_fork", resource="studio_dashboard",
        resource_id=new_dashboard.id,
        metadata={"source_id": str(dashboard_id)},
    )
    await db.commit()
    await db.refresh(new_dashboard)
    return StudioDashboardResponse.model_validate(new_dashboard)


@router.post("/dashboards/{dashboard_id}/star", status_code=204, summary="Star a dashboard")
async def star_dashboard(
    dashboard_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Mark a dashboard as starred (favorited). Idempotent.

    **Roles required:** any authenticated user
    """
    dashboard = await StudioDashboardRepository(db).get_by_id_and_org(dashboard_id, current_user.org_id)
    if not dashboard:
        raise not_found("Dashboard not found")
    await StudioStarRepository(db).upsert(current_user.id, current_user.org_id, "dashboard", dashboard_id)
    await db.commit()


@router.delete("/dashboards/{dashboard_id}/star", status_code=204, summary="Unstar a dashboard")
async def unstar_dashboard(
    dashboard_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a star from a dashboard. Idempotent.

    **Roles required:** any authenticated user
    """
    await StudioStarRepository(db).delete(current_user.id, "dashboard", dashboard_id)
    await db.commit()


@router.post("/dashboards/{dashboard_id}/embed-token", status_code=201, response_model=StudioEmbedTokenResponse, summary="Generate an embed token")
async def create_embed_token(
    dashboard_id: UUID,
    body: StudioEmbedTokenRequest,
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> StudioEmbedTokenResponse:
    """Generate a time-limited token for embedding a private dashboard in an iframe.

    **Roles required:** manager, admin

    The embed URL (`/api/public/v1/studio/embed/{token}`) works without authentication
    and can be used in `<iframe src="...">` for embedding in internal tools.
    Tokens expire after `expires_in_hours` (default 24h, max 720h / 30 days).

    **Requires Redis** — tokens are stored in Redis.

    **Errors:**
    - 400 REDIS_REQUIRED — Redis is not configured.
    - 404 — dashboard not found.
    """
    dashboard = await StudioDashboardRepository(db).get_by_id_and_org(dashboard_id, current_user.org_id)
    if not dashboard:
        raise not_found("Dashboard not found")

    redis = await get_redis()
    if redis is None:
        raise bad_request("REDIS_REQUIRED", "Embed tokens require Redis to be configured (set REDIS_URL)")

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=body.expires_in_hours)
    payload = json.dumps({
        "dashboard_id": str(dashboard_id),
        "org_id": str(current_user.org_id),
        "expires_at": expires_at.isoformat(),
    })
    await redis.set(studio_embed(token), payload, ex=body.expires_in_hours * 3600)

    embed_url = f"/api/public/v1/studio/embed/{token}"
    return StudioEmbedTokenResponse(token=token, embed_url=embed_url, expires_at=expires_at)


# ── Dashboard items ───────────────────────────────────────────────────────────

@router.post("/dashboards/{dashboard_id}/items", status_code=201, response_model=StudioDashboardItemResponse, summary="Add item to dashboard")
async def add_dashboard_item(
    dashboard_id: UUID,
    body: StudioDashboardAddItemRequest,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> StudioDashboardItemResponse:
    """Add a visualization panel or text/markdown panel to a dashboard.

    **Roles required:** analyst, manager, admin

    **Request:**
    - `panel_type: "visualization"` — requires `visualization_id`.
    - `panel_type: "text"` — requires `content` (markdown supported). No `visualization_id`.
    - `position` — ordering hint (integer, lower = higher in grid).

    **Limits:** Max 20 items per dashboard.

    **Errors:**
    - 400 DASHBOARD_ITEM_LIMIT — 20-item limit reached.
    - 404 — dashboard or visualization not found.
    """
    dashboard = await StudioDashboardRepository(db).get_by_id_and_org(dashboard_id, current_user.org_id)
    if not dashboard:
        raise not_found("Dashboard not found")

    item_repo = StudioDashboardItemRepository(db)
    count = await item_repo.count_by_dashboard(dashboard_id)
    if count >= _MAX_DASHBOARD_ITEMS:
        raise bad_request("DASHBOARD_ITEM_LIMIT", f"Dashboards are limited to {_MAX_DASHBOARD_ITEMS} items.")

    viz_id: UUID | None = None
    if body.panel_type == "visualization":
        viz = await StudioVisualizationRepository(db).get_by_id_and_org(body.visualization_id, current_user.org_id)
        if not viz:
            raise not_found("Visualization not found")
        viz_id = body.visualization_id

    item = await item_repo.create(
        current_user.org_id, dashboard_id, viz_id, body.position,
        panel_type=body.panel_type, content=body.content,
    )
    dash_repo = StudioDashboardRepository(db)
    await dash_repo.update(
        dashboard,
        layout=_append_layout_slot(dashboard.layout, item.id, body.panel_type),
    )
    await db.commit()
    await db.refresh(item)
    return StudioDashboardItemResponse.model_validate(item)


@router.delete("/dashboards/{dashboard_id}/items/{item_id}", status_code=204, summary="Remove item from dashboard")
async def remove_dashboard_item(
    dashboard_id: UUID,
    item_id: UUID,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a panel from a dashboard. Does not delete the underlying visualization or query.

    **Roles required:** analyst (own dashboard only), manager/admin (any dashboard)
    """
    dashboard = await StudioDashboardRepository(db).get_by_id_and_org(dashboard_id, current_user.org_id)
    if not dashboard:
        raise not_found("Dashboard not found")
    _assert_owner_or_elevated(dashboard.created_by, current_user)

    item_repo = StudioDashboardItemRepository(db)
    item = await item_repo.get_by_id_and_org(item_id, current_user.org_id)
    if not item or item.dashboard_id != dashboard_id:
        raise not_found("Dashboard item not found")

    await item_repo.delete(item)
    await StudioDashboardRepository(db).update(
        dashboard,
        layout=_remove_layout_slot(dashboard.layout, item_id),
    )
    await db.commit()
