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
async def delete_inbox_item(item_id: str, permanent: bool = False, reason: str = ""):
    """Soft-delete (default) or hard-delete an inbox item.

    DEFAULT (`permanent=false`): update status to `cancelled` and
    stash the previous status in metadata.previous_status + the
    optional cancel reason in metadata.cancel_reason. The row stays
    in the database — the frontend's Cancelled tab shows these rows
    with Restore / Delete Forever affordances. This is the path the
    "Delete" button in the inbox hits.

    PERMANENT (`permanent=true`): hard DELETE the row. Used by the
    "Delete Forever" button in the Cancelled tab, and by any flow
    that needs a clean removal (bulk purges, GDPR, test cleanup).

    Both paths also PATCH the linked Paperclip issue to cancelled
    and add it to _processed_issues so the safety-net poller doesn't
    re-import the agent's late reply.
    """
    from datetime import datetime, timezone
    sb = get_db()
    # Look up the row first so we can capture the previous status
    # (for restore) and the paperclip_issue_id (to cancel upstream).
    previous_status = None
    paperclip_issue_id = None
    existing_metadata: dict = {}
    try:
        row = (
            sb.table("inbox_items")
            .select("status, paperclip_issue_id, metadata")
            .eq("id", item_id)
            .limit(1)
            .execute()
        )
        if row.data:
            previous_status = row.data[0].get("status")
            paperclip_issue_id = row.data[0].get("paperclip_issue_id")
            md = row.data[0].get("metadata")
            if isinstance(md, dict):
                existing_metadata = md
    except Exception:
        pass

    # Always cancel upstream — user's intent is "stop this work"
    # whether they soft-delete or hard-delete.
    if paperclip_issue_id:
        try:
            from backend.orchestrator import _urllib_request
            from backend.paperclip_office_sync import _add_processed
            _urllib_request("PATCH", f"/api/issues/{paperclip_issue_id}", data={
                "status": "cancelled",
            })
            _add_processed(paperclip_issue_id)
        except Exception:
            pass

    if permanent:
        sb.table("inbox_items").delete().eq("id", item_id).execute()
        return {"deleted": item_id, "permanent": True, "paperclip_cancelled": bool(paperclip_issue_id)}

    # Soft-delete: flip to cancelled, stash previous_status + reason
    # in metadata so Restore can put the row back where it was.
    new_metadata = dict(existing_metadata)
    if previous_status and previous_status != "cancelled":
        new_metadata["previous_status"] = previous_status
    if reason:
        new_metadata["cancel_reason"] = reason
    new_metadata["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    try:
        sb.table("inbox_items").update({
            "status": "cancelled",
            "metadata": new_metadata,
        }).eq("id", item_id).execute()
    except Exception:
        # Worst case fall back to hard delete if the update fails
        # (shouldn't happen, but better than leaving the row stuck)
        sb.table("inbox_items").delete().eq("id", item_id).execute()
        return {"deleted": item_id, "permanent": True, "fallback_hard_delete": True}
    return {
        "deleted": item_id,
        "permanent": False,
        "soft": True,
        "previous_status": previous_status,
        "paperclip_cancelled": bool(paperclip_issue_id),
    }


@router.post("/api/inbox/{item_id}/restore")
async def restore_inbox_item(item_id: str):
    """Move a cancelled inbox row back to its previous status (or
    needs_review if none was recorded). Clears cancel_reason from
    metadata but keeps the cancelled_at timestamp so we have a
    history trail.
    """
    sb = get_db()
    restored_to = "needs_review"
    try:
        row = (
            sb.table("inbox_items")
            .select("metadata, status")
            .eq("id", item_id)
            .limit(1)
            .execute()
        )
        if row.data:
            md = row.data[0].get("metadata") or {}
            if isinstance(md, dict) and md.get("previous_status"):
                restored_to = md["previous_status"]
    except Exception:
        pass

    # Clean up the cancel fields but keep cancelled_at as history
    new_md: dict = {}
    try:
        row = (
            sb.table("inbox_items")
            .select("metadata")
            .eq("id", item_id)
            .limit(1)
            .execute()
        )
        if row.data and isinstance(row.data[0].get("metadata"), dict):
            new_md = {k: v for k, v in row.data[0]["metadata"].items() if k not in ("previous_status", "cancel_reason")}
    except Exception:
        pass

    sb.table("inbox_items").update({
        "status": restored_to,
        "metadata": new_md,
    }).eq("id", item_id).execute()
    return {"restored": item_id, "status": restored_to}
