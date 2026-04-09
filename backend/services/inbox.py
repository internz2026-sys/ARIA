"""Inbox Service — shared inbox operations.

Used by API endpoints, CEO dispatcher, and agent modules. This is the
single canonical interface for reading and writing inbox_items rows —
agents should not call sb.table("inbox_items") directly.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from backend.services.supabase import get_db

logger = logging.getLogger("aria.services.inbox")


def create_item(
    tenant_id: str,
    agent: str,
    title: str,
    content: str,
    *,
    type: str = "general",
    status: str = "ready",
    priority: str = "medium",
    task_id: str | None = None,
    chat_session_id: str | None = None,
    email_draft: dict | None = None,
) -> dict | None:
    """Insert a single inbox_items row and return the saved record.

    All optional fields use keyword-only args to make call sites
    self-documenting and avoid argument-order bugs. Returns None on
    failure (logged at error level) so callers can decide whether to
    raise or fall through.
    """
    try:
        row: dict = {
            "tenant_id": tenant_id,
            "agent": agent,
            "type": type,
            "title": title,
            "content": content,
            "status": status,
            "priority": priority,
        }
        if task_id:
            row["task_id"] = task_id
        if chat_session_id:
            row["chat_session_id"] = chat_session_id
        if email_draft:
            row["email_draft"] = email_draft

        result = get_db().table("inbox_items").insert(row).execute()
        saved = result.data[0] if result.data else None
        if saved:
            logger.info("Saved inbox item: agent=%s title=%s status=%s", agent, title[:60], status)
        return saved
    except Exception as e:
        logger.error("Failed to save inbox item (agent=%s): %s", agent, e)
        return None


def list_items(tenant_id: str, status: str = "", page: int = 1, page_size: int = 20) -> dict:
    sb = get_db()
    count_query = sb.table("inbox_items").select("id", count="exact").eq("tenant_id", tenant_id)
    if status:
        count_query = count_query.eq("status", status)
    count_result = count_query.execute()
    total = count_result.count if count_result.count is not None else len(count_result.data)

    offset = (max(page, 1) - 1) * page_size
    query = sb.table("inbox_items").select("*").eq("tenant_id", tenant_id)
    if status:
        query = query.eq("status", status)
    result = query.order("created_at", desc=True).range(offset, offset + page_size - 1).execute()

    return {
        "items": result.data,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, -(-total // page_size)),
    }


def status_counts(tenant_id: str) -> dict:
    sb = get_db()
    result = sb.table("inbox_items").select("status").eq("tenant_id", tenant_id).execute()
    counts: dict[str, int] = {}
    total = 0
    for row in (result.data or []):
        s = row.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
        total += 1
    counts["all"] = total
    return counts


def update_status(tenant_id: str, item_id: str, new_status: str) -> dict:
    sb = get_db()
    sb.table("inbox_items").update({
        "status": new_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", item_id).eq("tenant_id", tenant_id).execute()
    return {"updated": item_id, "new_status": new_status}


def delete_item(tenant_id: str, item_id: str) -> dict:
    sb = get_db()
    sb.table("inbox_items").delete().eq("id", item_id).eq("tenant_id", tenant_id).execute()
    return {"deleted": item_id}
