"""Scheduler Service — CRUD and execution for time-based tasks.

Handles scheduled emails, posts, campaigns, follow-ups, and reminders.
The background executor in server.py polls this service every 30s.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.services.supabase import get_db

logger = logging.getLogger("aria.scheduler")

VALID_STATUSES = {"draft", "pending_approval", "approved", "scheduled", "running", "sent", "published", "failed", "cancelled"}
VALID_TASK_TYPES = {"send_email", "publish_post", "publish_campaign", "follow_up_task", "reminder_task"}
EXECUTABLE_STATUSES = {"scheduled", "approved"}


# ─── CRUD ────────────────────────────────────────────────────────────────────

def create_task(
    tenant_id: str,
    task_type: str,
    title: str,
    scheduled_at: str,
    payload: dict | None = None,
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
    timezone_str: str = "UTC",
    status: str = "scheduled",
    approval_status: str = "none",
    created_by: str = "user",
    triggered_by_agent: str | None = None,
) -> dict:
    """Create a new scheduled task."""
    if task_type not in VALID_TASK_TYPES:
        return {"error": f"Invalid task_type: {task_type}. Valid: {', '.join(VALID_TASK_TYPES)}"}

    # If approval is required, start in pending_approval
    if approval_status == "pending":
        status = "pending_approval"

    sb = get_db()
    row = {
        "tenant_id": tenant_id,
        "task_type": task_type,
        "title": title,
        "scheduled_at": scheduled_at,
        "timezone": timezone_str,
        "payload": payload or {},
        "status": status,
        "approval_status": approval_status,
        "created_by": created_by,
        "triggered_by_agent": triggered_by_agent,
    }
    if related_entity_type:
        row["related_entity_type"] = related_entity_type
    if related_entity_id:
        row["related_entity_id"] = related_entity_id

    result = sb.table("scheduled_tasks").insert(row).execute()
    task = result.data[0] if result.data else None
    return {"task": task}


def list_tasks(
    tenant_id: str,
    status: str = "",
    task_type: str = "",
    from_date: str = "",
    to_date: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """List scheduled tasks with optional filters."""
    sb = get_db()
    # Single query with count="exact" — avoids duplicating the query for count
    query = sb.table("scheduled_tasks").select("*", count="exact").eq("tenant_id", tenant_id)

    if status:
        query = query.eq("status", status)
    if task_type:
        query = query.eq("task_type", task_type)
    if from_date:
        query = query.gte("scheduled_at", from_date)
    if to_date:
        query = query.lte("scheduled_at", to_date)

    offset = (max(page, 1) - 1) * page_size
    result = query.order("scheduled_at", desc=False).range(offset, offset + page_size - 1).execute()
    total = result.count if result.count is not None else len(result.data or [])

    return {"tasks": result.data or [], "total": total, "page": page, "page_size": page_size}


def get_task(tenant_id: str, task_id: str) -> dict:
    """Get a single scheduled task."""
    sb = get_db()
    result = sb.table("scheduled_tasks").select("*").eq("id", task_id).eq("tenant_id", tenant_id).single().execute()
    return result.data or {}


def update_task(tenant_id: str, task_id: str, updates: dict) -> dict:
    """Update a scheduled task (reschedule, change status, etc.)."""
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb = get_db()
    sb.table("scheduled_tasks").update(updates).eq("id", task_id).eq("tenant_id", tenant_id).execute()
    return {"updated": task_id, "changes": updates}


def delete_task(tenant_id: str, task_id: str) -> dict:
    """Delete a scheduled task."""
    sb = get_db()
    sb.table("scheduled_tasks").delete().eq("id", task_id).eq("tenant_id", tenant_id).execute()
    return {"deleted": task_id}


def cancel_task(tenant_id: str, task_id: str) -> dict:
    """Cancel a scheduled task (soft delete — keeps the record)."""
    return update_task(tenant_id, task_id, {"status": "cancelled"})


def approve_task(tenant_id: str, task_id: str) -> dict:
    """Approve a pending task — moves to 'scheduled' so the executor picks it up."""
    return update_task(tenant_id, task_id, {
        "approval_status": "approved",
        "status": "scheduled",
    })


def reject_task(tenant_id: str, task_id: str) -> dict:
    """Reject a pending task."""
    return update_task(tenant_id, task_id, {
        "approval_status": "rejected",
        "status": "cancelled",
    })


def reschedule_task(tenant_id: str, task_id: str, new_scheduled_at: str, new_timezone: str = "") -> dict:
    """Reschedule a task to a new time."""
    updates: dict[str, Any] = {"scheduled_at": new_scheduled_at}
    if new_timezone:
        updates["timezone"] = new_timezone
    # If task was already sent/published/failed, reset to scheduled
    task = get_task(tenant_id, task_id)
    if task.get("status") in ("sent", "published", "failed", "cancelled"):
        updates["status"] = "scheduled"
        updates["execution_result"] = None
        updates["executed_at"] = None
    return update_task(tenant_id, task_id, updates)


# ─── Calendar Query ──────────────────────────────────────────────────────────

def calendar_tasks(tenant_id: str, start: str, end: str) -> list[dict]:
    """Get all scheduled tasks in a date range for the calendar view."""
    sb = get_db()
    result = (
        sb.table("scheduled_tasks")
        .select("id,task_type,title,scheduled_at,timezone,status,approval_status,created_by,related_entity_type,payload")
        .eq("tenant_id", tenant_id)
        .gte("scheduled_at", start)
        .lte("scheduled_at", end)
        .neq("status", "cancelled")
        .order("scheduled_at")
        .execute()
    )
    return result.data or []


# ─── Executor — called by background loop ────────────────────────────────────

def get_due_tasks() -> list[dict]:
    """Find all tasks that are due for execution (across all tenants)."""
    sb = get_db()
    now = datetime.now(timezone.utc).isoformat()
    result = (
        sb.table("scheduled_tasks")
        .select("*")
        .in_("status", ["scheduled", "approved"])
        .lte("scheduled_at", now)
        .order("scheduled_at")
        .limit(20)
        .execute()
    )
    return result.data or []


def mark_running(task_id: str) -> None:
    sb = get_db()
    sb.table("scheduled_tasks").update({
        "status": "running",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", task_id).execute()


def mark_completed(task_id: str, status: str, result: dict) -> None:
    """Mark task as sent/published/failed with execution result."""
    sb = get_db()
    sb.table("scheduled_tasks").update({
        "status": status,
        "execution_result": result,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", task_id).execute()


async def execute_task(task: dict) -> dict:
    """Execute a single scheduled task. Returns execution result.

    Human-in-the-loop: external-facing tasks (send_email, publish_post)
    require explicit approval before execution. Tasks with approval_status
    "pending" or "none" for external actions are blocked.
    """
    task_type = task.get("task_type", "")
    tenant_id = task.get("tenant_id", "")
    payload = task.get("payload", {})
    task_id = task.get("id", "")

    # Don't execute if approval is pending
    if task.get("approval_status") == "pending":
        return {"skipped": True, "reason": "Awaiting approval"}

    # External-facing tasks MUST be explicitly approved — never auto-execute
    external_task_types = {"send_email", "publish_post", "publish_campaign"}
    if task_type in external_task_types and task.get("approval_status") not in ("approved", "none_required"):
        # Mark as needing approval instead of auto-executing
        mark_completed(task_id, "pending_approval", {"reason": "Requires human approval before sending"})
        return {"skipped": True, "reason": "External action requires approval. Review in Calendar."}

    mark_running(task_id)

    try:
        if task_type == "send_email":
            result = await _execute_send_email(tenant_id, payload)
        elif task_type in ("publish_post", "publish_campaign"):
            result = await _execute_publish_post(tenant_id, payload)
        elif task_type == "follow_up_task":
            result = await _execute_follow_up(tenant_id, payload)
        elif task_type == "reminder_task":
            result = await _execute_reminder(tenant_id, payload)
        else:
            result = {"error": f"Unknown task_type: {task_type}"}

        if result.get("error"):
            mark_completed(task_id, "failed", result)
        else:
            final_status = "sent" if task_type == "send_email" else "published"
            if task_type in ("follow_up_task", "reminder_task"):
                final_status = "sent"
            mark_completed(task_id, final_status, result)

        return result

    except Exception as e:
        logger.error("Scheduled task %s failed: %s", task_id, e)
        mark_completed(task_id, "failed", {"error": str(e)})
        return {"error": str(e)}


# ─── Task Type Executors ─────────────────────────────────────────────────────

async def _execute_send_email(tenant_id: str, payload: dict) -> dict:
    """Execute a scheduled email send."""
    from backend.tools import gmail_tool
    from backend.config.loader import get_tenant_config, save_tenant_config

    config = get_tenant_config(tenant_id)
    access_token = config.integrations.google_access_token
    refresh_token = config.integrations.google_refresh_token

    if not access_token and not refresh_token:
        return {"error": "Gmail not connected"}

    # Refresh if needed
    if not access_token and refresh_token:
        try:
            access_token = await gmail_tool.refresh_access_token(refresh_token)
            config.integrations.google_access_token = access_token
            save_tenant_config(config)
        except Exception as e:
            return {"error": f"Token refresh failed: {e}"}

    to = payload.get("to", "")
    subject = payload.get("subject", "")
    html_body = payload.get("html_body", payload.get("body", ""))

    if not to or not subject:
        return {"error": "Missing 'to' or 'subject' in payload"}

    result = await gmail_tool.send_email(
        access_token=access_token,
        to=to,
        subject=subject,
        html_body=html_body,
        from_email=config.owner_email,
    )

    # Retry with refresh on token expired
    if result.get("error") == "token_expired" and refresh_token:
        try:
            new_token = await gmail_tool.refresh_access_token(refresh_token)
            config.integrations.google_access_token = new_token
            save_tenant_config(config)
            result = await gmail_tool.send_email(
                access_token=new_token,
                to=to,
                subject=subject,
                html_body=html_body,
                from_email=config.owner_email,
            )
        except Exception:
            return {"error": "Gmail token expired and refresh failed"}

    # Update related inbox item if linked
    if not result.get("error") and payload.get("inbox_item_id"):
        try:
            sb = get_db()
            sb.table("inbox_items").update({"status": "sent"}).eq("id", payload["inbox_item_id"]).execute()
        except Exception:
            pass

    return result


async def _execute_publish_post(tenant_id: str, payload: dict) -> dict:
    """Execute a scheduled social post publish."""
    platform = payload.get("platform", "twitter").lower()
    text = payload.get("text", "")

    if not text:
        return {"error": "No post text in payload"}

    from backend.config.loader import get_tenant_config, save_tenant_config

    config = get_tenant_config(tenant_id)
    results = {}

    if platform in ("twitter", "x"):
        from backend.tools import twitter_tool
        access_token = config.integrations.twitter_access_token
        if not access_token:
            return {"error": "Twitter not connected"}
        result = await twitter_tool.post_tweet(access_token, text[:280])
        if result.get("error") == "token_expired" and config.integrations.twitter_refresh_token:
            try:
                tokens = await twitter_tool.refresh_access_token(config.integrations.twitter_refresh_token)
                access_token = tokens["access_token"]
                config.integrations.twitter_access_token = access_token
                save_tenant_config(config)
                result = await twitter_tool.post_tweet(access_token, text[:280])
            except Exception:
                return {"error": "Twitter token expired and refresh failed"}
        results = result

    elif platform == "linkedin":
        from backend.tools import linkedin_tool
        li_token = config.integrations.linkedin_access_token
        li_urn = config.integrations.linkedin_org_urn or config.integrations.linkedin_member_urn
        if not li_token or not li_urn:
            return {"error": "LinkedIn not connected"}
        results = await linkedin_tool.create_post(li_token, li_urn, text[:3000])

    else:
        return {"error": f"Unsupported platform: {platform}"}

    # Update related inbox item
    if not results.get("error") and payload.get("inbox_item_id"):
        try:
            sb = get_db()
            sb.table("inbox_items").update({"status": "sent"}).eq("id", payload["inbox_item_id"]).execute()
        except Exception:
            pass

    return results


async def _execute_follow_up(tenant_id: str, payload: dict) -> dict:
    """Execute a follow-up task — creates a notification/inbox item."""
    sb = get_db()
    title = payload.get("title", "Follow-up reminder")
    description = payload.get("description", "")

    # Create an inbox item as a reminder
    sb.table("inbox_items").insert({
        "tenant_id": tenant_id,
        "title": title,
        "content": description,
        "type": "follow_up",
        "status": "needs_review",
        "agent": payload.get("agent", "ceo"),
        "priority": payload.get("priority", "medium"),
    }).execute()

    # Also create a notification
    sb.table("notifications").insert({
        "tenant_id": tenant_id,
        "title": f"Follow-up: {title}",
        "body": description,
        "category": "inbox",
        "href": "/inbox",
    }).execute()

    return {"created": "follow_up", "title": title}


async def _execute_reminder(tenant_id: str, payload: dict) -> dict:
    """Execute a reminder — creates a notification."""
    sb = get_db()
    title = payload.get("title", "Reminder")
    body = payload.get("body", payload.get("description", ""))

    sb.table("notifications").insert({
        "tenant_id": tenant_id,
        "title": title,
        "body": body,
        "category": "status",
    }).execute()

    return {"notified": True, "title": title}
