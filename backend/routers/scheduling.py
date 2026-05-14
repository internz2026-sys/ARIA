"""Scheduled-tasks API + the unified calendar activity feed.

Covers /api/schedule/{tenant_id}/* (create/list/get/update + action POSTs:
cancel/approve/reject/reschedule/execute-now), plus /api/schedule/{tenant_id}
/calendar and /api/calendar/{tenant_id}/activity. All routes delegate to
backend.services.scheduler.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import get_verified_tenant
from backend.services import scheduler as scheduler_service
from backend.services.supabase import get_db as _get_supabase

logger = logging.getLogger("aria.server")

router = APIRouter()


class ScheduleTaskRequest(BaseModel):
    task_type: str
    title: str
    scheduled_at: str
    payload: dict = {}
    related_entity_type: str | None = None
    related_entity_id: str | None = None
    timezone: str = "UTC"
    approval_required: bool = False
    created_by: str = "user"


@router.post("/api/schedule/{tenant_id}/tasks")
async def create_scheduled_task(
    tenant_id: str,
    body: ScheduleTaskRequest,
    _verified: dict = Depends(get_verified_tenant),
):
    """Create a new scheduled task."""
    # _emit_scheduled_task_created is shared with the pending-schedule
    # watcher in server.py — keep it there and import here to avoid
    # introducing a second canonical version.
    from backend.server import _emit_scheduled_task_created
    result = scheduler_service.create_task(
        tenant_id=tenant_id,
        task_type=body.task_type,
        title=body.title,
        scheduled_at=body.scheduled_at,
        payload=body.payload,
        related_entity_type=body.related_entity_type,
        related_entity_id=body.related_entity_id,
        timezone_str=body.timezone,
        approval_status="pending" if body.approval_required else "none",
        created_by=body.created_by,
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    await _emit_scheduled_task_created(tenant_id, result.get("task"))
    return result


@router.get("/api/schedule/{tenant_id}/tasks")
async def list_scheduled_tasks(
    tenant_id: str,
    status: str = "",
    task_type: str = "",
    from_date: str = "",
    to_date: str = "",
    page: int = 1,
    page_size: int = 50,
    _verified: dict = Depends(get_verified_tenant),
):
    """List scheduled tasks with optional filters."""
    return scheduler_service.list_tasks(tenant_id, status, task_type, from_date, to_date, page, page_size)


@router.get("/api/schedule/{tenant_id}/tasks/{task_id}")
async def get_scheduled_task(
    tenant_id: str,
    task_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Get a single scheduled task."""
    task = scheduler_service.get_task(tenant_id, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


class UpdateScheduleRequest(BaseModel):
    scheduled_at: str | None = None
    timezone: str | None = None
    title: str | None = None
    status: str | None = None
    payload: dict | None = None


@router.patch("/api/schedule/{tenant_id}/tasks/{task_id}")
async def update_scheduled_task(
    tenant_id: str,
    task_id: str,
    body: UpdateScheduleRequest,
    _verified: dict = Depends(get_verified_tenant),
):
    """Update a scheduled task (reschedule, change title, etc.)."""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    return scheduler_service.update_task(tenant_id, task_id, updates)


@router.post("/api/schedule/{tenant_id}/tasks/{task_id}/cancel")
async def cancel_scheduled_task(
    tenant_id: str,
    task_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Cancel a scheduled task."""
    return scheduler_service.cancel_task(tenant_id, task_id)


@router.post("/api/schedule/{tenant_id}/tasks/{task_id}/approve")
async def approve_scheduled_task(
    tenant_id: str,
    task_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Approve a pending scheduled task — moves to 'scheduled' for execution."""
    return scheduler_service.approve_task(tenant_id, task_id)


@router.post("/api/schedule/{tenant_id}/tasks/{task_id}/reject")
async def reject_scheduled_task(
    tenant_id: str,
    task_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Reject a pending scheduled task."""
    return scheduler_service.reject_task(tenant_id, task_id)


class RescheduleRequest(BaseModel):
    scheduled_at: str
    timezone: str = ""


@router.post("/api/schedule/{tenant_id}/tasks/{task_id}/reschedule")
async def reschedule_task(
    tenant_id: str,
    task_id: str,
    body: RescheduleRequest,
    _verified: dict = Depends(get_verified_tenant),
):
    """Reschedule a task to a new time."""
    return scheduler_service.reschedule_task(tenant_id, task_id, body.scheduled_at, body.timezone)


@router.post("/api/schedule/{tenant_id}/tasks/{task_id}/execute-now")
async def execute_task_now(
    tenant_id: str,
    task_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Execute a scheduled task immediately (bypass schedule)."""
    task = scheduler_service.get_task(tenant_id, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("approval_status") == "pending":
        raise HTTPException(status_code=400, detail="Task requires approval before execution")
    result = await scheduler_service.execute_task(task)
    return {"executed": True, "result": result}


@router.get("/api/schedule/{tenant_id}/calendar")
async def get_calendar(
    tenant_id: str,
    start: str = "",
    end: str = "",
    _verified: dict = Depends(get_verified_tenant),
):
    """Get scheduled tasks for the calendar view."""
    if not start or not end:
        now = datetime.now(timezone.utc)
        start = start or (now - timedelta(days=7)).isoformat()
        end = end or (now + timedelta(days=60)).isoformat()
    return {"tasks": scheduler_service.calendar_tasks(tenant_id, start, end)}


@router.get("/api/calendar/{tenant_id}/activity")
async def get_calendar_activity(
    tenant_id: str,
    start: str = "",
    end: str = "",
    _verified: dict = Depends(get_verified_tenant),
):
    """Unified marketing activity feed for the calendar view.

    Returns events from multiple sources (scheduled tasks, inbox drafts,
    sent items) in a single normalized event shape, so the calendar
    becomes a 'marketing activity dashboard' instead of a 'things I
    explicitly queued' calendar.

    Each event has:
      - id: stable id (uuid or composite)
      - source: 'scheduled' | 'inbox_draft' | 'inbox_sent' | 'agent_run'
      - title: short display label
      - timestamp: ISO datetime to anchor on the calendar
      - status: optional status badge
      - agent: optional agent slug for color/icon
      - href: optional deep-link target inside ARIA
      - metadata: source-specific extras
    """
    sb = _get_supabase()
    if not start or not end:
        now = datetime.now(timezone.utc)
        start = start or (now - timedelta(days=30)).isoformat()
        end = end or (now + timedelta(days=60)).isoformat()

    events: list[dict] = []

    # 1. Scheduled tasks (existing source)
    try:
        tasks = scheduler_service.calendar_tasks(tenant_id, start, end)
        for t in tasks:
            tt = t.get("task_type", "")
            href = "/calendar"
            payload = t.get("payload") or {}
            inbox_id = payload.get("inbox_item_id")
            if inbox_id:
                href = f"/inbox?id={inbox_id}"
            events.append({
                "id": f"scheduled:{t.get('id')}",
                "source": "scheduled",
                "task_type": tt,
                "title": t.get("title") or tt,
                "timestamp": t.get("scheduled_at"),
                "status": t.get("status", ""),
                "approval_status": t.get("approval_status", ""),
                "href": href,
                "metadata": {
                    "timezone": t.get("timezone"),
                    "created_by": t.get("created_by"),
                    "raw_id": t.get("id"),
                },
            })
    except Exception as e:
        logger.warning("[calendar-activity] scheduled fetch failed: %s", e)

    # 2. Inbox items (drafts + sent). Drafts use created_at, sent items
    #    use updated_at when status is sent/published. Both within the
    #    requested date range. This is what makes the calendar useful
    #    even when nothing is explicitly scheduled.
    try:
        inbox_rows = (
            sb.table("inbox_items")
            .select("id,title,agent,type,status,created_at,updated_at")
            .eq("tenant_id", tenant_id)
            .gte("created_at", start)
            .lte("created_at", end)
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        )
        for row in (inbox_rows.data or []):
            status = row.get("status") or ""
            is_sent = status in ("sent", "published", "completed")
            # Choose timestamp: when it was sent (if applicable) or when
            # it was created (if it's still a draft / pending)
            ts = row.get("updated_at") if is_sent and row.get("updated_at") else row.get("created_at")
            events.append({
                "id": f"inbox:{row.get('id')}",
                "source": "inbox_sent" if is_sent else "inbox_draft",
                "task_type": row.get("type", ""),
                "title": row.get("title", "Inbox item"),
                "timestamp": ts,
                "status": status,
                "agent": row.get("agent", ""),
                "href": f"/inbox?id={row.get('id')}",
                "metadata": {
                    "raw_id": row.get("id"),
                    "type": row.get("type"),
                },
            })
    except Exception as e:
        logger.warning("[calendar-activity] inbox fetch failed: %s", e)

    # Sort by timestamp ascending so the calendar can render in chrono order
    events.sort(key=lambda e: (e.get("timestamp") or ""))

    return {
        "tenant_id": tenant_id,
        "start": start,
        "end": end,
        "events": events,
        "counts": {
            "total": len(events),
            "scheduled": sum(1 for e in events if e["source"] == "scheduled"),
            "inbox_draft": sum(1 for e in events if e["source"] == "inbox_draft"),
            "inbox_sent": sum(1 for e in events if e["source"] == "inbox_sent"),
        },
    }
