"""Dashboard, analytics, project-stagnation, and CEO action/triage routes.

The CEO chat endpoint (POST /api/ceo/chat) lives in backend/routers/ceo.py.
The two routes here — /api/ceo/triage and /api/ceo/{tenant_id}/action —
handle CEO-level task classification and the synchronous CEO business
action dispatcher respectively; they're not part of the chat session
state machine.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import get_verified_tenant
from backend.config.loader import get_tenant_config
from backend.services.realtime import sio
from backend.services.supabase import get_db as _get_supabase

logger = logging.getLogger("aria.server")

router = APIRouter()


# ─── Dashboard API ───
@router.get("/api/dashboard/{tenant_id}/config")
async def dashboard_config(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Return tenant business info for the dashboard."""
    try:
        config = get_tenant_config(tenant_id)
        return {
            "tenant_id": tenant_id,
            "business_name": config.business_name,
            "product_name": config.product.name,
            "product_description": config.product.description,
            "positioning": config.gtm_playbook.positioning,
            "channels": config.channels,
            "active_agents": config.active_agents,
            "brand_voice_tone": config.brand_voice.tone,
            "action_plan_30": config.gtm_playbook.action_plan_30,
            "messaging_pillars": config.gtm_playbook.messaging_pillars,
            "onboarding_status": config.onboarding_status,
            "skipped_fields": config.skipped_fields,
        }
    except Exception:
        return {"tenant_id": tenant_id, "business_name": None}


@router.get("/api/dashboard/{tenant_id}/stats")
async def dashboard_stats(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Real KPI counts from inbox_items + scheduled_tasks.

    All four queries run concurrently via asyncio.gather + to_thread,
    instead of the previous sequential blocking pattern. Each
    sb.table(...).execute() is a sync HTTP round-trip that blocks the
    event loop, so wrapping them in to_thread frees the loop AND
    gather lets them fly in parallel. ~4x faster dashboard load
    (200-800ms -> 50-200ms typical).
    """
    sb = _get_supabase()
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat()
    two_weeks_ago = (now - timedelta(days=14)).isoformat()

    _content_types = ("blog_post", "email_sequence", "social_post", "ad_campaign", "email", "blog", "social")
    _published_statuses = ("ready", "needs_review", "draft_pending_approval", "sent", "completed")

    # Each thread-wrapped lambda owns one query. Errors are swallowed
    # and produce a sentinel so a single failed query doesn't tank the
    # whole dashboard render.
    def _q_content():
        try:
            return sb.table("inbox_items").select("id,type,status,created_at", count="exact") \
                .eq("tenant_id", tenant_id) \
                .in_("type", list(_content_types)) \
                .in_("status", list(_published_statuses)) \
                .execute()
        except Exception as e:
            logger.warning("[dashboard-stats] content query failed: %s", e)
            return None

    def _q_sent_emails():
        try:
            return sb.table("inbox_items").select("id", count="exact") \
                .eq("tenant_id", tenant_id) \
                .in_("type", ("email_sequence", "email")) \
                .eq("status", "sent") \
                .execute()
        except Exception:
            return None

    def _q_social():
        try:
            return sb.table("inbox_items").select("id,created_at", count="exact") \
                .eq("tenant_id", tenant_id) \
                .in_("type", ("social_post", "social")) \
                .in_("status", ("sent", "ready", "completed")) \
                .execute()
        except Exception:
            return None

    def _q_ad_spend():
        try:
            return sb.table("campaigns").select("budget_spent").eq("tenant_id", tenant_id).execute()
        except Exception:
            return None  # campaigns table may not exist yet

    # Run all four queries concurrently. Each one stalls a thread, but
    # the asyncio event loop is free to handle other requests.
    content_res, sent_res, social_res, ad_res = await asyncio.gather(
        asyncio.to_thread(_q_content),
        asyncio.to_thread(_q_sent_emails),
        asyncio.to_thread(_q_social),
        asyncio.to_thread(_q_ad_spend),
    )

    # Content Published — total + 7d delta vs previous 7d
    if content_res is not None:
        all_rows = content_res.data or []
        content_total = content_res.count if content_res.count is not None else len(all_rows)
        content_this_week = sum(1 for r in all_rows if r.get("created_at", "") >= week_ago)
        content_prev_week = sum(1 for r in all_rows if two_weeks_ago <= r.get("created_at", "") < week_ago)
        content_delta = content_this_week - content_prev_week
        content_delta_pct = int((content_delta / content_prev_week) * 100) if content_prev_week > 0 else 0
    else:
        content_total = content_delta = content_delta_pct = 0

    # Emails Sent — count only
    if sent_res is not None:
        emails_sent_count = sent_res.count if sent_res.count is not None else len(sent_res.data or [])
    else:
        emails_sent_count = 0

    # Social Engagement — count + 7d delta
    if social_res is not None:
        social_rows = social_res.data or []
        social_count = social_res.count if social_res.count is not None else len(social_rows)
        social_this_week = sum(1 for r in social_rows if r.get("created_at", "") >= week_ago)
        social_prev_week = sum(1 for r in social_rows if two_weeks_ago <= r.get("created_at", "") < week_ago)
        social_delta_pct = int(((social_this_week - social_prev_week) / social_prev_week) * 100) if social_prev_week > 0 else 0
    else:
        social_count = social_delta_pct = 0

    # Ad Spend — sum across campaigns
    ad_spend_value = sum((r.get("budget_spent") or 0) for r in (ad_res.data or [])) if ad_res is not None else 0

    return {
        "tenant_id": tenant_id,
        "kpis": {
            "content_published": {
                "value": content_total,
                "delta": content_delta,
                "delta_pct": content_delta_pct,
            },
            "emails_sent": {
                "value": emails_sent_count,
                "open_rate": 0,    # placeholder until we wire Gmail tracking
                "click_rate": 0,
            },
            "social_engagement": {
                "value": social_count,
                "delta_pct": social_delta_pct,
            },
            "ad_spend": {
                "value": ad_spend_value,
                "roas": 0,
            },
        },
    }


@router.get("/api/dashboard/{tenant_id}/activity")
async def dashboard_activity(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Return recent activity from inbox items and tasks."""
    sb = _get_supabase()
    activity = []
    try:
        # Recent inbox deliverables
        inbox_result = sb.table("inbox_items").select("agent,type,title,created_at").eq(
            "tenant_id", tenant_id
        ).order("created_at", desc=True).limit(20).execute()
        for item in (inbox_result.data or []):
            activity.append({
                "agent": item["agent"],
                "action": f"Delivered: {item['title'][:60]}",
                "type": item["type"],
                "timestamp": item["created_at"],
            })
    except Exception:
        pass
    try:
        # Recent completed tasks
        task_result = sb.table("tasks").select("agent,task,status,created_at").eq(
            "tenant_id", tenant_id
        ).order("created_at", desc=True).limit(20).execute()
        for task in (task_result.data or []):
            status_verb = "Completed" if task["status"] == "done" else "Working on"
            activity.append({
                "agent": task["agent"],
                "action": f"{status_verb}: {task['task'][:60]}",
                "type": "task",
                "timestamp": task["created_at"],
            })
    except Exception:
        pass
    # Sort by timestamp, newest first
    activity.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return {"tenant_id": tenant_id, "activity": activity[:30]}


@router.get("/api/dashboard/{tenant_id}/inbox")
async def dashboard_inbox(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Return inbox items for the dashboard (latest 5)."""
    try:
        sb = _get_supabase()
        result = sb.table("inbox_items").select("id,title,agent,type,status,priority,created_at").eq("tenant_id", tenant_id).order("created_at", desc=True).limit(5).execute()
        return {"tenant_id": tenant_id, "items": result.data}
    except Exception:
        return {"tenant_id": tenant_id, "items": []}


@router.get("/api/analytics/{tenant_id}")
async def analytics_data(
    tenant_id: str,
    date_range: str = "7d",
    _verified: dict = Depends(get_verified_tenant),
):
    """Aggregated analytics for the Analytics page.

    Pulls data from inbox_items, tasks, agent_logs, and scheduled_tasks
    to produce the KPI cards + activity chart + breakdowns + recent
    feed the frontend renders. Every aggregation is best-effort: a
    missing table or bad row never crashes the endpoint, the affected
    bucket just returns empty.
    """
    days = 7 if date_range == "7d" else 30 if date_range == "30d" else 90
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    sb = _get_supabase()

    # ── Inbox items: source of most analytics ─────────────────────
    inbox_rows: list[dict] = []
    try:
        res = (
            sb.table("inbox_items")
            .select("id, agent, type, status, title, created_at")
            .eq("tenant_id", tenant_id)
            .gte("created_at", cutoff_iso)
            .order("created_at", desc=True)
            .limit(2000)
            .execute()
        )
        inbox_rows = list(res.data or [])
    except Exception as e:
        logger.debug("[analytics] inbox_items fetch failed: %s", e)

    # ── Aggregations ──────────────────────────────────────────────
    activity_by_day: dict[str, dict[str, int]] = {}
    by_agent: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}

    # Seed all days so the chart x-axis is continuous even on quiet days
    for i in range(days):
        day = (datetime.now(timezone.utc) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        activity_by_day[day] = {"total": 0}

    _TYPE_BUCKET = {
        "email_sequence": "email", "email": "email",
        "social_post": "social", "social": "social",
        "image": "image", "image_request": "image",
        "blog_post": "content", "article": "content", "landing_page": "content",
        "ad_campaign": "ad",
    }

    for row in inbox_rows:
        created = (row.get("created_at") or "")[:10]
        if created and created in activity_by_day:
            bucket = _TYPE_BUCKET.get(row.get("type") or "", "other")
            activity_by_day[created]["total"] = activity_by_day[created].get("total", 0) + 1
            activity_by_day[created][bucket] = activity_by_day[created].get(bucket, 0) + 1
        agent = row.get("agent") or "unknown"
        by_agent[agent] = by_agent.get(agent, 0) + 1
        rtype = row.get("type") or "unknown"
        by_type[rtype] = by_type.get(rtype, 0) + 1
        rstatus = row.get("status") or "unknown"
        by_status[rstatus] = by_status.get(rstatus, 0) + 1

    activity_series = [
        {"date": day, **counts} for day, counts in sorted(activity_by_day.items())
    ]

    # ── Recent activity feed (last 10 across all types) ───────────
    recent_activity = [
        {
            "id": r.get("id"),
            "agent": r.get("agent"),
            "type": r.get("type"),
            "status": r.get("status"),
            "title": (r.get("title") or "")[:120],
            "created_at": r.get("created_at"),
        }
        for r in inbox_rows[:10]
    ]

    # ── Task completion / scheduled task stats ────────────────────
    task_totals = {"total": 0, "completed": 0, "in_progress": 0, "failed": 0}
    try:
        # Skip soft-deleted tasks so the analytics totals reflect the
        # user's actual active workload, not their trash.
        tasks_res = (
            sb.table("tasks")
            .select("status")
            .eq("tenant_id", tenant_id)
            .gte("created_at", cutoff_iso)
            .is_("deleted_at", "null")
            .limit(2000)
            .execute()
        )
        for t in tasks_res.data or []:
            s = (t.get("status") or "").lower()
            task_totals["total"] += 1
            if s in ("done", "completed"):
                task_totals["completed"] += 1
            elif s in ("in_progress", "working", "running"):
                task_totals["in_progress"] += 1
            elif s in ("failed", "cancelled", "canceled", "error"):
                task_totals["failed"] += 1
    except Exception as e:
        logger.debug("[analytics] tasks fetch failed: %s", e)

    scheduled_totals = {"upcoming": 0, "executed": 0, "failed": 0}
    try:
        sched_res = (
            sb.table("scheduled_tasks")
            .select("status, scheduled_at")
            .eq("tenant_id", tenant_id)
            .gte("created_at", cutoff_iso)
            .limit(2000)
            .execute()
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        for t in sched_res.data or []:
            s = (t.get("status") or "").lower()
            if s in ("sent", "executed", "completed", "done"):
                scheduled_totals["executed"] += 1
            elif s in ("failed", "cancelled", "canceled", "error"):
                scheduled_totals["failed"] += 1
            elif (t.get("scheduled_at") or "") > now_iso:
                scheduled_totals["upcoming"] += 1
    except Exception as e:
        logger.debug("[analytics] scheduled_tasks fetch failed: %s", e)

    # ── Totals / KPIs derived from above ──────────────────────────
    totals = {
        "items": len(inbox_rows),
        "agents_active": len(by_agent),
        "types_active": len(by_type),
        "days_in_range": days,
    }

    return {
        "tenant_id": tenant_id,
        "date_range": date_range,
        "totals": totals,
        "activity_series": activity_series,
        "by_agent": [{"agent": k, "count": v} for k, v in sorted(by_agent.items(), key=lambda x: -x[1])],
        "by_type": [{"type": k, "count": v} for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
        "by_status": [{"status": k, "count": v} for k, v in sorted(by_status.items(), key=lambda x: -x[1])],
        "recent_activity": recent_activity,
        "tasks": task_totals,
        "scheduled_tasks": scheduled_totals,
        # Keep the old funnel shape so the demo endpoint callers don't break.
        "funnel": {
            "impressions": 0, "clicks": 0, "signups": 0,
            "activated": 0, "converted": 0, "retained": 0,
        },
    }


# ─── CEO Task Triage ───
class TriageRequest(BaseModel):
    title: str


@router.post("/api/ceo/triage")
async def ceo_triage(body: TriageRequest):
    """CEO agent analyzes a task and returns column, priority, and assigned agent."""
    from backend.tools.claude_cli import call_claude
    import json as _json

    system = (
        "You are the ARIA CEO, a Chief Marketing Strategist. "
        "Given a marketing task description, classify it by returning ONLY a JSON object with these fields:\n"
        '- "column": one of "backlog", "todo", "in_progress" (use your judgment: vague/aspirational ideas → backlog, concrete actionable tasks → todo, urgent/time-sensitive → in_progress)\n'
        '- "priority": one of "low", "medium", "high" (based on impact and urgency)\n'
        '- "agent": one of "ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist" (the best agent for the job)\n'
        '- "reason": one short sentence explaining your decision\n'
        "Return ONLY valid JSON, no markdown, no explanation outside the JSON."
    )
    try:
        raw = await call_claude(system, f"Triage this task: {body.title}", tenant_id="global")
        # Extract JSON from response
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            result = _json.loads(raw[start:end])
            # Validate values
            if result.get("column") not in ("backlog", "todo", "in_progress"):
                result["column"] = "todo"
            if result.get("priority") not in ("low", "medium", "high"):
                result["priority"] = "medium"
            if result.get("agent") not in ("ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist"):
                result["agent"] = "ceo"
            return result
        return {"column": "todo", "priority": "medium", "agent": "ceo", "reason": "Could not parse CEO response"}
    except Exception:
        return {"column": "todo", "priority": "medium", "agent": "ceo", "reason": "CEO agent unavailable, using defaults"}


# ─── CEO Action Execution ───
class CEOActionRequest(BaseModel):
    action: str
    params: dict = {}
    confirmed: bool = False


@router.post("/api/ceo/{tenant_id}/action")
async def ceo_execute_action(
    tenant_id: str,
    body: CEOActionRequest,
    _verified: dict = Depends(get_verified_tenant),
):
    """Execute a CEO business action with confirmation enforcement."""
    from backend.ceo_actions import execute_action, is_forbidden_request  # noqa: F401

    result = await execute_action(
        tenant_id=tenant_id,
        action_name=body.action,
        params=body.params,
        confirmed=body.confirmed,
    )

    if result["status"] == "needs_confirmation":
        return result  # Frontend shows confirmation dialog

    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result.get("message", "Action failed"))

    # Emit real-time update with entity type for targeted refresh
    action_def = None
    try:
        from backend.ceo_actions import ACTION_REGISTRY
        action_def = ACTION_REGISTRY.get(body.action, {})
    except Exception:
        pass
    await sio.emit("ceo_action_executed", {
        "action": body.action,
        "entity": action_def.get("entity", "") if action_def else "",
        "result": result,
    }, room=tenant_id)

    return result


# ─── Stagnation Monitor / "Buried Task" API ───
@router.get("/api/projects/stale/{tenant_id}")
async def list_stale_projects(
    tenant_id: str,
    hours: int = 24,
    limit: int = 20,
    _verified: dict = Depends(get_verified_tenant),
):
    """Return inbox drafts that have been waiting on the user for more
    than `hours` (default 24h), excluding rows that are currently
    snoozed. Powers the Priority Actions section on the Projects page
    and the sidebar pulse badge.

    Also returns `recent_count` (items created in the last 24h) so the
    frontend can decide whether the stale items are "buried" (per spec:
    when there are 5+ newer items, the sidebar should pulse harder)."""
    from backend.services.projects import find_stale_items, count_recent_items

    rows = await asyncio.to_thread(
        find_stale_items, tenant_id, hours_old=max(1, hours), limit=min(max(1, limit), 50),
    )
    recent_count = await asyncio.to_thread(count_recent_items, tenant_id, hours=24)
    return {
        "stale_items": rows,
        "stale_count": len(rows),
        "recent_count": recent_count,
        "is_buried": len(rows) > 0 and recent_count >= 5,
        "hours_threshold": hours,
    }


@router.post("/api/projects/{tenant_id}/snooze/{item_id}")
async def snooze_stale_project(
    tenant_id: str,
    item_id: str,
    payload: dict = Body(default={}),
    _verified: dict = Depends(get_verified_tenant),
):
    """Snooze a stale row for `hours` (default 24, capped at 168 = 1
    week so the user can't accidentally hide a draft forever). The row
    isn't marked done — just hidden from the stagnation feed until the
    snooze expires. Per spec: 'they must remain Incomplete until the
    user explicitly acts.'"""
    from backend.services.projects import snooze_item

    hours = int((payload or {}).get("hours", 24))
    hours = max(1, min(hours, 168))
    result = await asyncio.to_thread(snooze_item, tenant_id, item_id, hours=hours)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Snooze failed")
    return result
