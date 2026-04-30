"""Tasks Router — /api/tasks/* endpoints.

Slice 3 of the multi-batch refactor. Lifts the three tasks routes out
of server.py (where they sat next to ~7,400 other lines) into a
dedicated module that owns ONLY task CRUD.

Behavior is unchanged — same routes, same payloads, same response
shapes. Only the file location moved.

Cross-module dependency: update_task / delete_task call
`_emit_agent_status` to keep the Virtual Office's walking-sprite
status in sync with task transitions. That helper still lives in
server.py because it owns `_live_agent_status` (module-level) + the
`sio` instance. We lazy-import it inside each handler so this router
can be loaded BEFORE server.py finishes initializing without
triggering a circular import at module load time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.supabase import get_db

logger = logging.getLogger("aria.routers.tasks")

router = APIRouter(prefix="/api/tasks", tags=["Tasks"])


class TaskUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None


@router.get("/{tenant_id}")
async def list_tasks(tenant_id: str):
    """List all tasks for a tenant, ordered by creation date."""
    try:
        sb = get_db()
        result = (
            sb.table("tasks")
            .select("*")
            .eq("tenant_id", tenant_id)
            .order("created_at", desc=True)
            .execute()
        )
        return {"tasks": result.data}
    except Exception as e:
        return {"tasks": [], "error": str(e)}


@router.patch("/{task_id}")
async def update_task(task_id: str, body: TaskUpdate):
    """Update a task's status or priority. Syncs agent visual status in Virtual Office."""
    # Lazy import — _emit_agent_status owns the Virtual Office state
    # in server.py. Importing at top of file would create a circular
    # load when server.py imports this router on app startup.
    from backend.server import _emit_agent_status

    sb = get_db()
    updates: dict = {}
    if body.status:
        updates["status"] = body.status
    if body.priority:
        updates["priority"] = body.priority
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Fetch task details before updating (for status sync)
    task_result = sb.table("tasks").select("agent,tenant_id,task").eq("id", task_id).execute()

    sb.table("tasks").update(updates).eq("id", task_id).execute()

    # Sync agent visual status with task status change
    if body.status and task_result.data:
        task = task_result.data[0]
        agent_id = task["agent"]
        tid = task["tenant_id"]
        if body.status == "in_progress":
            await _emit_agent_status(
                tid, agent_id, "working",
                current_task=task.get("task", ""),
                action="task_started",
            )
        elif body.status in ("done", "to_do", "backlog"):
            # Only flip the agent to idle if it has no OTHER in_progress
            # tasks — otherwise a parallel task would silently get its
            # walking-sprite turned off.
            other = (
                sb.table("tasks")
                .select("id")
                .eq("tenant_id", tid)
                .eq("agent", agent_id)
                .eq("status", "in_progress")
                .neq("id", task_id)
                .limit(1)
                .execute()
            )
            if not other.data:
                await _emit_agent_status(
                    tid, agent_id, "idle",
                    action="task_status_changed",
                )

    return {"ok": True}


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    """Delete a task. If it was in_progress, sync agent back to idle."""
    from backend.server import _emit_agent_status

    sb = get_db()

    # Fetch before deleting for status sync
    task_result = sb.table("tasks").select("agent,tenant_id,status").eq("id", task_id).execute()

    sb.table("tasks").delete().eq("id", task_id).execute()

    # If the deleted task was in_progress, check whether the agent has
    # OTHER active tasks before flipping it to idle.
    if task_result.data and task_result.data[0].get("status") == "in_progress":
        task = task_result.data[0]
        agent_id = task["agent"]
        tid = task["tenant_id"]
        other = (
            sb.table("tasks")
            .select("id")
            .eq("tenant_id", tid)
            .eq("agent", agent_id)
            .eq("status", "in_progress")
            .limit(1)
            .execute()
        )
        if not other.data:
            await _emit_agent_status(
                tid, agent_id, "idle",
                action="task_deleted",
            )

    return {"ok": True}
