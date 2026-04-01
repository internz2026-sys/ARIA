"""Inbox Router — inbox items listing, updates, deletion."""
from __future__ import annotations

from fastapi import APIRouter, Request

from backend.services import inbox as inbox_service
from backend.services.supabase import get_db

router = APIRouter(tags=["Inbox"])


@router.get("/api/inbox/{tenant_id}/counts")
async def inbox_status_counts(tenant_id: str):
    """Return counts per status for inbox tabs."""
    return {"counts": inbox_service.status_counts(tenant_id)}


@router.get("/api/inbox/{tenant_id}")
async def list_inbox(tenant_id: str, status: str = "", page: int = 1, page_size: int = 20):
    """List inbox items for a tenant with pagination."""
    try:
        return inbox_service.list_items(tenant_id, status, page, page_size)
    except Exception as e:
        return {"items": [], "total": 0, "page": 1, "page_size": page_size, "total_pages": 1, "error": str(e)}


@router.patch("/api/inbox/{item_id}")
async def update_inbox_item(item_id: str, request: Request):
    """Update an inbox item's status."""
    sb = get_db()
    body = await request.json()
    updates = {}
    if "status" in body:
        updates["status"] = body["status"]
    if updates:
        from datetime import datetime, timezone
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        sb.table("inbox_items").update(updates).eq("id", item_id).execute()
    return {"updated": item_id, **updates}


@router.delete("/api/inbox/{item_id}")
async def delete_inbox_item(item_id: str):
    """Delete an inbox item."""
    sb = get_db()
    sb.table("inbox_items").delete().eq("id", item_id).execute()
    return {"deleted": item_id}
