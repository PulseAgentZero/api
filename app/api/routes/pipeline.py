"""Pipeline management API routes — trigger runs and inspect run history."""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.errors import bad_request, not_found, validation_error
from app.infrastructure.database.base import touch_updated_at, utcnow
from app.infrastructure.database.models.pipeline_run import PipelineRun
from app.infrastructure.database.models.pipeline_schedule import PipelineSchedule
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.pipeline_run_repository import (
    PipelineRunRepository,
)
from app.infrastructure.audit import log_audit
from app.infrastructure.database.session import get_db
from app.services.pipeline_trigger import claim_and_trigger_pipeline, serialize_pipeline_run

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pipeline", tags=["Pipeline"])

_WINDOWS_TIMEZONE_ALIASES = {
    "utc": "UTC",
    "gmt": "UTC",
    "z": "UTC",
    "coordinated universal time": "UTC",
    "w. central africa standard time": "Africa/Lagos",
    "west central africa standard time": "Africa/Lagos",
    "west africa standard time": "Africa/Lagos",
}


def _resolve_schedule_timezone(value: str | None) -> tuple[str, ZoneInfo]:
    """Return a canonical IANA timezone name and ZoneInfo object.

    The API stores IANA names, but dashboard widgets and browsers can send
    friendlier labels such as "UTC+1" or "West Africa Standard Time".
    """
    raw = (value or "UTC").strip()
    if not raw:
        raw = "UTC"

    try:
        return raw, ZoneInfo(raw)
    except Exception:
        pass

    normalized = re.sub(r"\s+", " ", raw).strip().lower()
    alias = _WINDOWS_TIMEZONE_ALIASES.get(normalized)
    if alias:
        return alias, ZoneInfo(alias)

    # Common UI labels sometimes include the IANA value in prose.
    iana_match = re.search(r"\b[A-Za-z_]+/[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)?\b", raw)
    if iana_match:
        candidate = iana_match.group(0)
        try:
            return candidate, ZoneInfo(candidate)
        except Exception:
            pass

    # Accept fixed whole-hour offsets like UTC+1, UTC+01:00, GMT-5.
    # POSIX "Etc/GMT" signs are intentionally reversed by the tz database.
    offset_match = re.search(r"\b(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\b", raw, re.I)
    if offset_match:
        sign, hours_raw, minutes_raw = offset_match.groups()
        hours = int(hours_raw)
        minutes = int(minutes_raw or "0")
        if 0 <= hours <= 14 and minutes == 0:
            etc_sign = "-" if sign == "+" else "+"
            candidate = f"Etc/GMT{etc_sign}{hours}" if hours else "UTC"
            return candidate, ZoneInfo(candidate)

    raise ValueError("Invalid timezone")


class TriggerBody(BaseModel):
    mapping_id: UUID | None = None


class ScheduleBody(BaseModel):
    cron_expression: str = "0 */6 * * *"
    timezone: str = "UTC"
    is_active: bool = True
    mapping_id: UUID | None = None


class SchedulePreviewBody(BaseModel):
    cron_expression: str = Field(..., description="5-field crontab expression")
    timezone: str = "UTC"
    count: int = Field(5, ge=1, le=20)


async def _trigger_common(
    current_user: User,
    db: AsyncSession,
    mapping_id: UUID | None = None,
) -> dict:
    result = await claim_and_trigger_pipeline(
        db,
        current_user.org_id,
        mapping_id=mapping_id,
        triggered_by=current_user.id,
        trigger_source="manual",
    )
    run_id = result.get("run_id")
    await log_audit(
        db,
        org_id=current_user.org_id,
        user_id=current_user.id,
        action="pipeline.triggered",
        resource="pipeline_run",
        resource_id=UUID(run_id) if run_id else None,
        metadata={
            "trigger_source": "manual",
            "mapping_id": str(mapping_id) if mapping_id else None,
        },
    )
    return result


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def trigger_pipeline_legacy(
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
):
    """Trigger an async pipeline run (legacy alias for POST /pipeline/trigger).

    Returns 202 immediately. Poll `GET /pipeline/runs/{run_id}` or stream progress via
    `GET /pipeline/runs/{run_id}/stream`. Requires admin or manager role.
    """
    return await _trigger_common(current_user, db)


@router.post("/trigger", status_code=status.HTTP_202_ACCEPTED)
async def trigger_pipeline(
    body: TriggerBody | None = None,
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
):
    """Trigger an async pipeline run.

    Optionally pass `mapping_id` to target a specific schema mapping; otherwise the
    org's active mapping is used. Returns 202 with `run_id` immediately — the pipeline
    executes in the background worker. Requires admin or manager role.
    """
    mid = body.mapping_id if body else None
    return await _trigger_common(current_user, db, mapping_id=mid)


@router.post("/run/sync")
async def trigger_pipeline_sync(
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
):
    """Run the pipeline synchronously and return results.

    Use this for testing/demo — blocks until the pipeline completes.
    Not recommended for production use on large datasets.
    """
    from app.agents.orchestrators.pipeline import PipelineOrchestrator

    repo = PipelineRunRepository(db)
    active = await repo.get_active_for_org(current_user.org_id)
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PIPELINE_ALREADY_RUNNING",
                "message": "Pipeline run already in progress",
                "run_id": str(active.id),
                "status": active.status,
            },
        )

    orchestrator = PipelineOrchestrator(db)
    state = await orchestrator.execute(
        current_user.org_id, trigger_source="manual_sync"
    )

    return {
        "run_id": state.get("pipeline_run_id"),
        "status": state.get("current_step", "unknown"),
        "org_id": str(current_user.org_id),
        "org_name": state.get("org_name"),
        "error": state.get("error"),
        "risk_summary": state.get("risk_summary", {}),
        "recommendation_stats": state.get("recommendation_stats", {}),
        "pipeline_metrics": state.get("pipeline_metrics", {}),
        "started_at": state.get("started_at"),
        "completed_at": state.get("completed_at"),
    }


@router.get("/runs")
async def list_pipeline_runs(
    limit: int = 25,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the most recent pipeline runs for the current org."""
    limit = max(1, min(limit, 100))
    runs = await PipelineRunRepository(db).list_by_org(current_user.org_id, limit=limit)
    return {"runs": [serialize_pipeline_run(r) for r in runs]}


@router.get("/runs/{run_id}")
async def get_pipeline_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return detail for one pipeline run, including step metrics."""
    try:
        rid = UUID(run_id)
    except ValueError as exc:
        raise bad_request("BAD_REQUEST", "Invalid run_id") from exc

    run = await PipelineRunRepository(db).get_by_id(rid)
    if run is None or run.org_id != current_user.org_id:
        raise not_found("Pipeline run not found")

    payload = serialize_pipeline_run(run)
    payload["step_metrics"] = run.step_metrics or []
    payload["generation_caps"] = run.generation_caps or {}
    return payload


@router.get("/runs/{run_id}/stream")
async def stream_pipeline_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stream real-time pipeline progress via Server-Sent Events (SSE).

    Connect with `EventSource` in the browser. Three event types:
    - `progress` — `{ current_step, step_count, last_step }` emitted whenever a step changes.
    - `done` — `{ status, current_step }` emitted when the run reaches a terminal state.
    - `error` — `{ error }` emitted if the run record disappears.

    The stream closes automatically when the run completes, fails, or is cancelled.
    """
    try:
        rid = UUID(run_id)
    except ValueError as exc:
        raise bad_request("BAD_REQUEST", "Invalid run_id") from exc
    run = await PipelineRunRepository(db).get_by_id(rid)
    if run is None or run.org_id != current_user.org_id:
        raise not_found("Pipeline run not found")

    terminal = ("succeeded", "failed", "cancelled")

    async def gen():
        last_sig: tuple[str | None, int] | None = None
        while True:
            await asyncio.sleep(0.75)
            await db.expire_all()
            fresh = await db.get(PipelineRun, rid)
            if fresh is None or fresh.org_id != current_user.org_id:
                yield f"event: error\ndata: {json.dumps({'error': 'run_not_found'})}\n\n"
                return
            if fresh.status in terminal:
                yield f"event: done\ndata: {json.dumps({'status': fresh.status, 'current_step': fresh.current_step})}\n\n"
                return
            sm = fresh.step_metrics or []
            sig = (fresh.current_step, len(sm))
            if sig != last_sig:
                last_sig = sig
                payload = {
                    "current_step": fresh.current_step,
                    "step_count": len(sm),
                    "last_step": sm[-1] if sm else None,
                }
                yield f"event: progress\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/runs/{run_id}/cancel")
async def cancel_pipeline_run(
    run_id: str,
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Request cancellation of an in-progress pipeline run.

    Sets run status to `cancelled`. The worker checks this flag between pipeline steps,
    so cancellation may not be instant. Returns 422 if the run has already completed.
    Requires admin or manager role.
    """
    try:
        rid = UUID(run_id)
    except ValueError as exc:
        raise bad_request("BAD_REQUEST", "Invalid run_id") from exc
    run = await PipelineRunRepository(db).get_by_id(rid)
    if run is None or run.org_id != current_user.org_id:
        raise not_found("Pipeline run not found")
    if run.status in ("succeeded", "failed", "cancelled"):
        raise bad_request("BAD_REQUEST", "Run already completed")
    run.status = "cancelled"
    run.completed_at = utcnow()
    touch_updated_at(run)
    await db.commit()
    return {"message": "Cancellation requested"}


@router.get("/schedule")
async def get_schedule(
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> dict | None:
    """Return the org's pipeline schedule, or null if no schedule has been configured.

    The default schedule is every 6 hours (`0 */6 * * *`), created automatically when
    onboarding completes. Requires admin or manager role.
    """
    r = await db.execute(
        select(PipelineSchedule).where(PipelineSchedule.org_id == current_user.org_id).limit(1)
    )
    row = r.scalar_one_or_none()
    if row is None:
        return None
    return {
        "id": str(row.id),
        "cron_expression": row.cron_expression,
        "timezone": row.timezone,
        "is_active": row.is_active,
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "mapping_id": str(row.mapping_id) if row.mapping_id else None,
    }


@router.post("/schedule/preview-next")
async def preview_schedule_next(
    body: SchedulePreviewBody,
    current_user: User = Depends(require_role("admin", "manager")),
) -> dict:
    """Return the next N fire times for a cron expression and timezone — without persisting.

    Lets the UI preview "Next 5 runs" before the user clicks Save. Returns both UTC and
    timezone-localized ISO timestamps so the UI can show whichever is clearer.
    """
    try:
        croniter(body.cron_expression)
    except Exception:
        raise validation_error(
            "Invalid cron expression",
            fields={"cron_expression": "Unparseable cron expression"},
        )
    try:
        timezone_name, tz = _resolve_schedule_timezone(body.timezone)
    except ValueError:
        raise validation_error(
            "Invalid timezone",
            fields={
                "timezone": "Use a valid IANA timezone, e.g. UTC or Africa/Lagos, or a UTC/GMT offset like UTC+1"
            },
        )

    now_utc = datetime.now(timezone.utc)
    base = now_utc.astimezone(tz)
    it = croniter(body.cron_expression, base)
    next_runs = []
    for _ in range(body.count):
        nxt_local = it.get_next(datetime)
        nxt_utc = nxt_local.astimezone(timezone.utc)
        next_runs.append({
            "utc": nxt_utc.isoformat(),
            "local": nxt_local.isoformat(),
        })

    return {
        "cron_expression": body.cron_expression,
        "timezone": timezone_name,
        "next_runs": next_runs,
    }


@router.get("/scheduler/status")
async def scheduler_status(
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Surface scheduler freshness, health, and invocation counters to the UI.

    `healthy` is true when the most recent heartbeat is fresher than the configured
    stale-after window (default 180s). `age_seconds` is null until the scheduler
    process writes its first heartbeat.
    """
    from app.infrastructure.database.models.scheduler_heartbeat import (
        SchedulerHeartbeat,
    )
    from app.services.schedulers.heartbeat import (
        HEARTBEAT_KIND,
        HEARTBEAT_STALE_AFTER_SECONDS,
    )

    r = await db.execute(
        select(SchedulerHeartbeat).where(SchedulerHeartbeat.kind == HEARTBEAT_KIND).limit(1)
    )
    row = r.scalar_one_or_none()
    if row is None:
        return {
            "kind": HEARTBEAT_KIND,
            "last_seen_at": None,
            "age_seconds": None,
            "healthy": False,
            "process_id": None,
            "host": None,
            "scheduled_runs_total": 0,
            "stale_after_seconds": HEARTBEAT_STALE_AFTER_SECONDS,
        }

    now = datetime.now(timezone.utc)
    last_seen = row.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    age = (now - last_seen).total_seconds()
    return {
        "kind": row.kind,
        "last_seen_at": last_seen.isoformat(),
        "age_seconds": round(age, 1),
        "healthy": age <= HEARTBEAT_STALE_AFTER_SECONDS,
        "process_id": row.process_id,
        "host": row.host,
        "scheduled_runs_total": int(row.scheduled_runs_total or 0),
        "stale_after_seconds": HEARTBEAT_STALE_AFTER_SECONDS,
    }


@router.put("/schedule")
async def put_schedule(
    body: ScheduleBody,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create or update the org's pipeline schedule. Requires admin role.

    `cron_expression` must be a valid 5-field cron string (e.g. `"0 */6 * * *"` for every
    6 hours). `timezone` is a tz database name (e.g. `"Africa/Lagos"`). Set `is_active: false`
    to pause the schedule without deleting it.
    """
    try:
        croniter(body.cron_expression)
    except Exception:
        raise validation_error(
            "Invalid cron expression",
            fields={"cron_expression": "Unparseable cron expression"},
        )
    try:
        timezone_name, schedule_tz = _resolve_schedule_timezone(body.timezone)
    except ValueError:
        raise validation_error(
            "Invalid timezone",
            fields={
                "timezone": "Use a valid IANA timezone, e.g. UTC or Africa/Lagos, or a UTC/GMT offset like UTC+1"
            },
        )
    r = await db.execute(
        select(PipelineSchedule).where(PipelineSchedule.org_id == current_user.org_id).limit(1)
    )
    row = r.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    next_at = None
    if body.is_active:
        base = now.astimezone(schedule_tz)
        next_at = croniter(body.cron_expression, base).get_next(datetime)
    if row:
        row.cron_expression = body.cron_expression
        row.timezone = timezone_name
        row.is_active = body.is_active
        row.mapping_id = body.mapping_id
        row.next_run_at = next_at
    else:
        row = PipelineSchedule(
            org_id=current_user.org_id,
            cron_expression=body.cron_expression,
            timezone=timezone_name,
            is_active=body.is_active,
            mapping_id=body.mapping_id,
            next_run_at=next_at,
        )
        db.add(row)
    await db.commit()
    await db.refresh(row)

    from app.services.schedulers.pipeline_scheduler import publish_schedule_reload

    await publish_schedule_reload()

    return {
        "id": str(row.id),
        "cron_expression": row.cron_expression,
        "timezone": row.timezone,
        "is_active": row.is_active,
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "mapping_id": str(row.mapping_id) if row.mapping_id else None,
    }
