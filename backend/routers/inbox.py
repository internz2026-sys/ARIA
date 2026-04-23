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

    # Capture the pre-edit row so we can diff for style memory below and
    # so the socket payload carries tenant_id without a second query.
    pre_row = None
    try:
        pre_row = (
            sb.table("inbox_items")
            .select("id, tenant_id, agent, content, email_draft")
            .eq("id", item_id)
            .single()
            .execute()
        ).data or None
    except Exception:
        pre_row = None

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("inbox_items").update(updates).eq("id", item_id).execute()

    # Style memory capture — when the caller edited `content` or the
    # email_draft HTML, persist the diff so BaseAgent can replay it
    # into future prompts. Only fires when the change is substantive
    # (>20 chars delta) to skip typo fixes and micro-tweaks.
    try:
        if pre_row:
            before_text = ""
            after_text = ""
            if "content" in updates and isinstance(updates["content"], str):
                before_text = pre_row.get("content") or ""
                after_text = updates["content"]
            elif "email_draft" in updates and isinstance(updates["email_draft"], dict):
                before_draft = pre_row.get("email_draft") or {}
                before_text = (before_draft or {}).get("html_body") or ""
                after_text = updates["email_draft"].get("html_body") or ""
            if before_text and after_text and before_text != after_text:
                diff = abs(len(after_text) - len(before_text))
                if diff >= 20:
                    sb.table("style_adjustments").insert({
                        "tenant_id": pre_row["tenant_id"],
                        "agent": pre_row.get("agent") or "",
                        "inbox_item_id": item_id,
                        "original_content": before_text[:10000],
                        "edited_content": after_text[:10000],
                        "diff_chars": diff,
                    }).execute()
    except Exception:
        # Style memory is a best-effort improvement — never fail the
        # user's edit because the learning layer had a hiccup (or
        # because the SQL migration hasn't been applied yet).
        pass

    # Emit a socket update so any open inbox / conversations pages
    # refresh without a manual reload. Guarded so we don't crash the
    # write path on a rare socket-server hiccup.
    try:
        tenant_id = (pre_row or {}).get("tenant_id") if pre_row else None
        if tenant_id:
            from backend.server import sio
            await sio.emit("inbox_item_updated", {"id": item_id, **updates}, room=tenant_id)
    except Exception:
        pass

    return {"updated": item_id, **updates}


@router.delete("/api/inbox/{item_id}")
async def delete_inbox_item(item_id: str):
    """Delete an inbox item + cancel the matching Paperclip issue.

    When the user deletes (or cancels) an inbox row, we also tell
    Paperclip to cancel the underlying issue and block the safety-net
    poller from re-importing the agent's late reply. Without this,
    deletion was ARIA-only: the agent kept grinding on its assigned
    issue, wasted tokens, and the poller would eventually re-create
    an inbox row from the agent's reply comment.
    """
    sb = get_db()
    # Look up the paperclip_issue_id before deleting so we can cancel
    # the upstream issue. Silently skip if not found or not linked to
    # Paperclip — not every inbox row has an upstream issue.
    paperclip_issue_id = None
    try:
        row = (
            sb.table("inbox_items")
            .select("paperclip_issue_id")
            .eq("id", item_id)
            .limit(1)
            .execute()
        )
        if row.data:
            paperclip_issue_id = row.data[0].get("paperclip_issue_id")
    except Exception:
        pass

    if paperclip_issue_id:
        try:
            from backend.orchestrator import _urllib_request
            from backend.paperclip_office_sync import _add_processed
            _urllib_request("PATCH", f"/api/issues/{paperclip_issue_id}", data={
                "status": "cancelled",
            })
            # Block the global poller from re-importing this issue if
            # the agent finishes a late reply after cancellation.
            _add_processed(paperclip_issue_id)
        except Exception:
            pass

    sb.table("inbox_items").delete().eq("id", item_id).execute()
    return {"deleted": item_id, "paperclip_cancelled": bool(paperclip_issue_id)}
