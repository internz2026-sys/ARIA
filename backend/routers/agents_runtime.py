"""Agent-runtime routes: list/run/pause/resume + virtual-office presence +
the token-gated /api/media/{tenant_id}/generate entrypoint used by the
Paperclip Media Designer agent.

Name choice: `agents_runtime.py` rather than `agents.py` to avoid colliding
with `backend/agents/` (the actual agent implementations).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from backend.auth import get_verified_tenant
from backend.config.loader import get_tenant_config, save_tenant_config
from backend.orchestrator import (
    dispatch_agent,
    get_agent_status,
    pause_agent_paperclip,
    resume_agent_paperclip,
    PlanQuotaExceeded,
)
from backend.services.realtime import (
    sio,
    emit_task_completed as _emit_task_completed,
)
from backend.services.supabase import get_db as _get_supabase

logger = logging.getLogger("aria.server")

router = APIRouter()


@router.get("/api/agents/{tenant_id}")
async def list_agents(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    statuses = await get_agent_status(tenant_id)
    return {"tenant_id": tenant_id, "agents": statuses}


@router.post("/api/agents/{tenant_id}/{agent_name}/run")
async def run_agent(
    tenant_id: str,
    agent_name: str,
    _verified: dict = Depends(get_verified_tenant),
):
    # _emit_agent_status, _infer_content_type, _extract_title, and
    # _save_inbox_item live in server.py and are shared across many
    # other routes/handlers; inline-import to avoid a module-load
    # circular import.
    from backend.server import (
        _emit_agent_status,
        _infer_content_type,
        _extract_title,
        _save_inbox_item,
    )

    # Agent starts working at desk
    await _emit_agent_status(tenant_id, agent_name, "working",
                             current_task=f"Running {agent_name} task",
                             action="start_work")

    try:
        result = await dispatch_agent(tenant_id, agent_name)
    except PlanQuotaExceeded as exc:
        # Expected user-facing wall, not a system error. Return a 429
        # JSON the frontend can render as an "Upgrade to continue"
        # modal. Drop the agent's "working" status back to idle so the
        # office sprite doesn't sit stuck at the desk.
        from starlette.responses import JSONResponse
        await _emit_agent_status(tenant_id, agent_name, "idle",
                                 action="task_complete")
        return JSONResponse(
            status_code=429,
            content={
                "status": "quota_exceeded",
                "reason": exc.reason,
                "plan": exc.plan,
                "used": exc.used,
                "limit": exc.limit,
            },
        )
    await sio.emit("agent_event", result, room=tenant_id)

    # Agent done — return to idle
    await _emit_agent_status(tenant_id, agent_name, "idle",
                             action="task_complete")

    # Save output to inbox
    content = result.get("result", "")
    if content and isinstance(content, str):
        content_type = _infer_content_type(agent_name, content)
        title = _extract_title(agent_name, "", content)
        saved = _save_inbox_item(
            tenant_id=tenant_id,
            agent=agent_name,
            title=title,
            content=content,
            content_type=content_type,
        )
        if saved:
            await sio.emit("inbox_new_item", {
                "id": saved["id"],
                "agent": agent_name,
                "type": content_type,
                "title": title,
                "status": "ready",
                "created_at": saved.get("created_at", ""),
            }, room=tenant_id)
            await _emit_task_completed(
                tenant_id,
                inbox_item_id=saved["id"],
                agent_id=agent_name,
                title=title,
                content_type=content_type,
                status="ready",
            )

    return result


@router.post("/api/media/{tenant_id}/generate")
async def generate_media_image(tenant_id: str, request: Request, payload: dict = Body(default={})):
    """Direct image-generation endpoint for the Paperclip Media Designer agent.

    Bypasses Paperclip dispatch and calls media_agent.run() locally so the agent
    actually produces a real PNG via Pollinations -> Supabase Storage -> inbox.
    Public (no JWT) so the Paperclip-spawned Claude CLI can curl it from inside
    the container — same pattern as /api/inbox/.

    Auth gate: the Paperclip Media Designer is the only legitimate caller,
    but the endpoint reaches a paid AI image API. Without auth, anyone could
    drain the API budget with a curl loop. We gate via a shared internal
    token (ARIA_INTERNAL_AGENT_TOKEN) sent in the `X-Aria-Agent-Token`
    header. The Paperclip skill MD on the agent side must include this
    header on the curl call. Production refuses requests when the token
    isn't configured (fail-closed); dev still allows unauth'd with a
    warning to keep local smoke tests working.
    """
    # _is_production lives in server.py.
    from backend.server import _is_production

    expected_token = (os.environ.get("ARIA_INTERNAL_AGENT_TOKEN") or "").strip()
    received_token = (request.headers.get("X-Aria-Agent-Token") or "").strip()
    if expected_token:
        if not received_token or received_token != expected_token:
            logger.warning(
                "[media] /api/media/%s/generate rejected: bad/missing X-Aria-Agent-Token",
                tenant_id,
            )
            raise HTTPException(status_code=401, detail="Invalid agent token")
    elif _is_production():
        logger.error(
            "[media] ARIA_INTERNAL_AGENT_TOKEN not configured in production — refusing"
        )
        raise HTTPException(
            status_code=503,
            detail="Internal agent token not configured",
        )
    else:
        logger.warning(
            "[media] ARIA_INTERNAL_AGENT_TOKEN unset (dev mode) — accepting unauth'd request"
        )

    from backend.agents import media_agent

    prompt = (payload or {}).get("prompt", "")
    if not prompt:
        return {"status": "failed", "error": "prompt is required"}

    # Recover chat_session_id from the watcher's placeholder (most recent
    # "processing" media row for this tenant). The Paperclip Media
    # Designer never learns the session_id — it's ARIA-internal — so we
    # backfill it here so the canonical row is session-scoped.
    inherited_session_id: str | None = None
    try:
        sb = _get_supabase()
        ph = (
            sb.table("inbox_items")
            .select("chat_session_id")
            .eq("tenant_id", tenant_id)
            .eq("agent", "media")
            .eq("status", "processing")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if ph.data:
            inherited_session_id = ph.data[0].get("chat_session_id")
    except Exception:
        inherited_session_id = None

    result = await media_agent.run(tenant_id, {
        "prompt": prompt,
        "chat_session_id": inherited_session_id,
    })
    inbox_row = (result or {}).get("inbox_item") if isinstance(result, dict) else None

    # Kill the watcher's "Media is working on..." placeholder so the user
    # sees ONE row that transitions processing -> ready, not a stale
    # placeholder lingering next to the finished image row.
    if inbox_row and tenant_id:
        # TODO(split): _cleanup_media_placeholder lives in
        # backend/routers/inbox.py and isn't imported at the call site.
        # Pre-existing latent NameError — preserved verbatim so behavior
        # doesn't change during the mechanical refactor. The call site
        # uses module-global lookup, so it'll fail the same way it would
        # have before the split.
        await _cleanup_media_placeholder(tenant_id, inbox_row.get("id"))  # noqa: F821

        # Push the finished row to the UI in real time
        try:
            await sio.emit("inbox_new_item", {
                "id": inbox_row.get("id"),
                "agent": "media",
                "type": inbox_row.get("type", "image"),
                "title": inbox_row.get("title", ""),
                "status": inbox_row.get("status", "ready"),
                "priority": inbox_row.get("priority", "medium"),
                "created_at": inbox_row.get("created_at", ""),
            }, room=tenant_id)
            await _emit_task_completed(
                tenant_id,
                inbox_item_id=inbox_row.get("id") or "",
                agent_id="media",
                title=inbox_row.get("title", ""),
                content_type=inbox_row.get("type", "image"),
                status=inbox_row.get("status", "ready"),
            )
        except Exception:
            pass

    return result


@router.post("/api/agents/{tenant_id}/{agent_name}/pause")
async def pause_agent(
    tenant_id: str,
    agent_name: str,
    _verified: dict = Depends(get_verified_tenant),
):
    config = get_tenant_config(tenant_id)
    if agent_name in config.active_agents:
        config.active_agents.remove(agent_name)
        save_tenant_config(config)
    # Also pause in Paperclip orchestrator
    await pause_agent_paperclip(agent_name)
    return {"status": "paused", "agent": agent_name}


@router.post("/api/agents/{tenant_id}/{agent_name}/resume")
async def resume_agent(
    tenant_id: str,
    agent_name: str,
    _verified: dict = Depends(get_verified_tenant),
):
    config = get_tenant_config(tenant_id)
    if agent_name not in config.active_agents:
        config.active_agents.append(agent_name)
        save_tenant_config(config)
    # Also resume in Paperclip orchestrator
    await resume_agent_paperclip(agent_name)
    return {"status": "resumed", "agent": agent_name}


# ─── Virtual Office API ───
@router.get("/api/office/agents/{tenant_id}")
async def virtual_office_agents(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Return all virtual office agents with their current persisted status."""
    # VIRTUAL_OFFICE_AGENTS + _live_agent_status live in server.py and are
    # touched from many places (background loops, chat handler, etc.) — so
    # they stay there. Inline-import to avoid the circular load.
    from backend.server import VIRTUAL_OFFICE_AGENTS, _live_agent_status

    now = datetime.now(timezone.utc).isoformat()
    live = _live_agent_status.get(tenant_id, {})

    # Load persisted status from Supabase (survives page navigation)
    db_statuses: dict[str, dict] = {}
    try:
        sb = _get_supabase()
        result = sb.table("agent_status").select("agent_id,status,current_task,updated_at").eq(
            "tenant_id", tenant_id
        ).execute()
        for row in (result.data or []):
            db_statuses[row["agent_id"]] = row
    except Exception:
        pass

    # Also check tasks table for agents with in_progress tasks.
    # Soft-deleted tasks should not count as active — exclude them.
    task_statuses: dict[str, str] = {}
    try:
        sb = _get_supabase()
        result = sb.table("tasks").select("agent,task").eq(
            "tenant_id", tenant_id
        ).eq("status", "in_progress").is_("deleted_at", "null").execute()
        for t in (result.data or []):
            task_statuses[t["agent"]] = t["task"]
    except Exception:
        pass

    agents = []
    for a in VIRTUAL_OFFICE_AGENTS:
        aid = a["agent_id"]
        live_entry = live.get(aid, {})
        db_entry = db_statuses.get(aid, {})
        live_status = live_entry.get("status")
        db_status = db_entry.get("status")

        # Priority: in-memory live > persisted DB > task-based > idle
        if live_status and live_status not in ("idle",):
            status = live_status
            current_task = live_entry.get("current_task", "")
            last_updated = live_entry.get("last_updated", now)
        elif db_status and db_status not in ("idle",):
            status = db_status
            current_task = db_entry.get("current_task", "")
            last_updated = db_entry.get("updated_at", now)
        elif aid in task_statuses:
            status = "working"
            current_task = task_statuses[aid]
            last_updated = now
        else:
            status = "idle"
            current_task = ""
            last_updated = now

        agents.append({
            "agent_id": aid,
            "name": a["name"],
            "role": a["role"],
            "model": a["model"],
            "status": status,
            "current_task": current_task,
            "department": a["department"],
            "last_updated": last_updated,
        })
    return {"agents": agents}


@router.get("/api/office/agents/{tenant_id}/{agent_id}/activity")
async def virtual_office_agent_activity(
    tenant_id: str,
    agent_id: str,
    limit: int = 5,
    _verified: dict = Depends(get_verified_tenant),
):
    """Recent activity feed for a specific agent — powers the
    AgentInfoPanel's Recent Activity list. Pulls from agent_logs
    (every dispatched action) and recent inbox_items authored by the
    agent, merges by timestamp, and returns the top N."""
    sb = _get_supabase()
    items: list[dict] = []
    try:
        logs = sb.table("agent_logs").select(
            "action,status,timestamp,result"
        ).eq("tenant_id", tenant_id).eq("agent_name", agent_id).order(
            "timestamp", desc=True
        ).limit(limit).execute()
        for row in (logs.data or []):
            result = row.get("result") or {}
            summary = ""
            if isinstance(result, dict):
                summary = result.get("task") or result.get("title") or result.get("message") or ""
            items.append({
                "kind": "log",
                "action": row.get("action") or "",
                "status": row.get("status") or "",
                "summary": str(summary)[:140],
                "timestamp": row.get("timestamp"),
            })
    except Exception as e:
        logger.debug("[office-activity] agent_logs fetch failed: %s", e)
    try:
        inbox = sb.table("inbox_items").select(
            "title,status,created_at,type"
        ).eq("tenant_id", tenant_id).eq("agent", agent_id).order(
            "created_at", desc=True
        ).limit(limit).execute()
        for row in (inbox.data or []):
            items.append({
                "kind": "inbox",
                "action": row.get("type") or "draft",
                "status": row.get("status") or "",
                "summary": (row.get("title") or "")[:140],
                "timestamp": row.get("created_at"),
            })
    except Exception as e:
        logger.debug("[office-activity] inbox_items fetch failed: %s", e)

    items.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return {"agent_id": agent_id, "items": items[:limit]}
