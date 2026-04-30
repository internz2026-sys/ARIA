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

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.services.supabase import get_db

logger = logging.getLogger("aria.routers.tasks")

router = APIRouter(prefix="/api/tasks", tags=["Tasks"])


class TaskUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None


def _verify_task_owner(request: Request, sb, task_id: str) -> dict:
    """Look up `task_id`, verify the JWT user owns the tenant that owns
    the task, and return the row. 403 on mismatch, 404 on missing.

    The auth middleware in server.py only checks tenant ownership when
    a tenant_id appears in the URL path. /api/tasks/{task_id} doesn't
    carry one, so without this check any authenticated user could
    PATCH or DELETE any task in the system. Adding the gate here
    closes that hole — same email/sub comparison the middleware
    already does for tenant-prefixed paths.
    """
    user = getattr(request.state, "user", None) or {}

    # Dev mode (no JWT secret configured) — middleware short-circuits
    # auth and stamps a synthetic dev-user. Skip ownership for parity.
    if not user or user.get("sub") == "dev-user":
        result = sb.table("tasks").select("*").eq("id", task_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Task not found")
        return result.data[0]

    result = (
        sb.table("tasks")
        .select("id, tenant_id, agent, task, status")
        .eq("id", task_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Task not found")
    task = result.data[0]

    # Pull the tenant config and compare ownership the same way the
    # auth middleware does in server.py — owner_email match (case-
    # insensitive) or tenant_id == JWT sub. Empty owner_email falls
    # through with a warning so legacy tenants don't lock their
    # owners out.
    user_email = (user.get("email") or "").lower().strip()
    user_id = user.get("sub") or ""
    try:
        from backend.config.loader import get_tenant_config
        config = get_tenant_config(task["tenant_id"])
        owner_email = (config.owner_email or "").lower().strip()
        if owner_email and user_email and owner_email == user_email:
            return task
        if str(config.tenant_id) == user_id:
            return task
        if not owner_email:
            logger.warning(
                "[tasks] owner check: tenant %s has no owner_email — allowing (legacy)",
                task["tenant_id"],
            )
            return task
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[tasks] owner check failed for task %s: %s", task_id, e)
        # Fail closed on a lookup hiccup — better to 404 than to leak
        # a destructive mutation past a transient DB error.
        raise HTTPException(status_code=404, detail="Task not found")

    logger.warning(
        "[tasks] ownership denied: user=%s tenant=%s task=%s",
        user_id, task["tenant_id"], task_id,
    )
    raise HTTPException(status_code=403, detail="You don't have access to this task")


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
async def update_task(task_id: str, body: TaskUpdate, request: Request):
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

    # Verify the JWT user owns the tenant that owns this task. Returns
    # the row + raises 403/404 — also doubles as the "task details"
    # fetch we need for the post-update agent-status emit.
    task = _verify_task_owner(request, sb, task_id)

    sb.table("tasks").update(updates).eq("id", task_id).execute()

    # Sync agent visual status with task status change
    if body.status and task:
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
async def delete_task(task_id: str, request: Request):
    """Delete a task. If it was in_progress, sync agent back to idle."""
    from backend.server import _emit_agent_status

    sb = get_db()

    # Owner check + fetch in one shot — refuses 403/404 before any
    # destructive write happens.
    task = _verify_task_owner(request, sb, task_id)

    sb.table("tasks").delete().eq("id", task_id).execute()

    # If the deleted task was in_progress, check whether the agent has
    # OTHER active tasks before flipping it to idle.
    if task and task.get("status") == "in_progress":
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
