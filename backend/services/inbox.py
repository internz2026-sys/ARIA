"""Inbox Service — shared inbox operations.

Used by both server.py API endpoints and ceo_actions.py CEO dispatcher.
"""
from __future__ import annotations

from datetime import datetime, timezone

from backend.services.supabase import get_db


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
