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


_EDITABLE_FIELDS = ("status", "title", "content", "metadata", "social_posts", "email_draft")


@router.patch("/api/inbox/{item_id}")
async def update_inbox_item(item_id: str, request: Request):
    """Partial-update an inbox item.

    Used by the UI to edit sub-agent outputs in place (blog text, ad
    copy, social posts, image metadata) instead of regenerating. Only
    whitelisted fields are accepted — tenant_id, agent, created_at, etc.
    are immutable from this endpoint.

    social_posts, when supplied, is written to the `content` column as a
    JSON-encoded `{"posts": [...]}` blob because that's the shape the
    existing read paths already parse (see inbox/page.tsx
    `parseSocialPosts`). Saves the caller from having to hand-encode it.
    """
    import json
    from datetime import datetime, timezone

    sb = get_db()
    body = await request.json() or {}
    updates: dict = {}

    # Pass-through scalar fields
    for key in ("status", "title", "content"):
        if key in body and body[key] is not None:
            updates[key] = body[key]

    # Structured fields — JSONB columns in Supabase, pass dicts verbatim.
    if "metadata" in body and isinstance(body["metadata"], dict):
        updates["metadata"] = body["metadata"]
    if "email_draft" in body and isinstance(body["email_draft"], dict):
        updates["email_draft"] = body["email_draft"]

    # Social posts — the canonical storage path is the `content` column
    # as a JSON blob, which is what the existing parseSocialPosts in
    # inbox/page.tsx already reads. Write to `content` if the caller
    # didn't also send a raw `content`.
    if "social_posts" in body and isinstance(body["social_posts"], list):
        updates["content"] = json.dumps({"posts": body["social_posts"]})

    if not updates:
        return {"updated": item_id, "no_changes": True}

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("inbox_items").update(updates).eq("id", item_id).execute()

    # Emit a socket update so any open inbox / conversations pages
    # refresh without a manual reload. Guarded so we don't crash the
    # write path on a rare socket-server hiccup.
    try:
        tenant_row = (
            sb.table("inbox_items").select("tenant_id").eq("id", item_id).single().execute()
        )
        tenant_id = (tenant_row.data or {}).get("tenant_id") if tenant_row else None
        if tenant_id:
            from backend.server import sio
            await sio.emit("inbox_item_updated", {"id": item_id, **updates}, room=tenant_id)
    except Exception:
        pass

    return {"updated": item_id, **updates}


@router.delete("/api/inbox/{item_id}")
async def delete_inbox_item(item_id: str):
    """Delete an inbox item."""
    sb = get_db()
    sb.table("inbox_items").delete().eq("id", item_id).execute()
    return {"deleted": item_id}
