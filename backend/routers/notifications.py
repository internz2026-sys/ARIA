"""Notification counts, listing, mark-read, and mark-seen endpoints."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth import get_verified_tenant
from backend.services.supabase import get_db as _get_supabase
from backend.services.realtime import sio

logger = logging.getLogger("aria.server")

router = APIRouter()


@router.get("/api/notifications/{tenant_id}/counts")
async def notification_counts(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Return counts used by the sidebar badges.

    The sidebar's "Inbox" badge is meant to tell the user how many inbox
    items are waiting on THEIR action — not a raw count of system events.
    We compute it directly from inbox_items (pending_approval + needs_review
    + failed) so the badge always matches the tab totals the user sees on
    the inbox page. The old `inbox_unread` field is kept for back-compat
    with any caller that still reads it.
    """
    sb = _get_supabase()

    # Per-category notification counts (used by Conversations + System badges).
    notif_result = sb.table("notifications").select("category", count="exact").eq(
        "tenant_id", tenant_id
    ).eq("is_read", False).execute()
    notif_counts: dict[str, int] = {}
    for row in (notif_result.data or []):
        cat = row.get("category", "other")
        notif_counts[cat] = notif_counts.get(cat, 0) + 1

    # Inbox action-needed count — drives the sidebar Inbox badge.
    try:
        inbox_result = sb.table("inbox_items").select("status").eq(
            "tenant_id", tenant_id
        ).in_("status", ["draft_pending_approval", "needs_review", "failed"]).execute()
        inbox_action_needed = len(inbox_result.data or [])
    except Exception as e:
        logger.warning("inbox action count failed: %s", e)
        inbox_action_needed = 0

    total = inbox_action_needed + notif_counts.get("conversation", 0) + notif_counts.get("system", 0)

    return {
        # Sidebar uses this for the Inbox badge.
        "inbox_unread": inbox_action_needed,
        "inbox_action_needed": inbox_action_needed,
        "conversations_unread": notif_counts.get("conversation", 0),
        "system_unread": notif_counts.get("system", 0),
        "status_unread": notif_counts.get("status", 0),
        "total_unread": total,
    }


@router.get("/api/notifications/{tenant_id}")
async def list_notifications(
    tenant_id: str,
    category: str = "",
    unread_only: bool = False,
    limit: int = 30,
    _verified: dict = Depends(get_verified_tenant),
):
    """List recent notifications for a tenant."""
    sb = _get_supabase()
    query = sb.table("notifications").select("*").eq("tenant_id", tenant_id)
    if category:
        query = query.eq("category", category)
    if unread_only:
        query = query.eq("is_read", False)
    result = query.order("created_at", desc=True).limit(limit).execute()
    return {"notifications": result.data or []}


class MarkReadRequest(BaseModel):
    ids: list[str] = []  # empty = mark all


@router.post("/api/notifications/{tenant_id}/mark-read")
async def mark_notifications_read(
    tenant_id: str,
    body: MarkReadRequest,
    _verified: dict = Depends(get_verified_tenant),
):
    """Mark specific notification IDs (or all) as read.

    Emits `notifications_read` via Socket.IO so other tabs / windows
    open on the same tenant can drop their local is_read flags without
    a manual refetch. Payload: `{ids: [...]}` where an empty array
    means "mark-all-read".
    """
    sb = _get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    if body.ids:
        sb.table("notifications").update({"is_read": True, "updated_at": now}).eq(
            "tenant_id", tenant_id
        ).in_("id", body.ids).execute()
    else:
        sb.table("notifications").update({"is_read": True, "updated_at": now}).eq(
            "tenant_id", tenant_id
        ).eq("is_read", False).execute()

    # Best-effort multi-tab sync. A socket hiccup shouldn't fail the
    # API call — the caller's optimistic local update still holds.
    try:
        await sio.emit(
            "notifications_read",
            {"ids": body.ids or [], "tenant_id": tenant_id},
            room=tenant_id,
        )
    except Exception as e:
        logger.debug("notifications_read emit failed (non-fatal): %s", e)

    return {"ok": True}


@router.post("/api/notifications/{tenant_id}/mark-seen")
async def mark_notifications_seen(
    tenant_id: str,
    body: MarkReadRequest,
    _verified: dict = Depends(get_verified_tenant),
):
    """Mark specific notification IDs (or all) as seen."""
    sb = _get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    if body.ids:
        sb.table("notifications").update({"is_seen": True, "updated_at": now}).eq(
            "tenant_id", tenant_id
        ).in_("id", body.ids).execute()
    else:
        sb.table("notifications").update({"is_seen": True, "updated_at": now}).eq(
            "tenant_id", tenant_id
        ).eq("is_seen", False).execute()
    return {"ok": True}
