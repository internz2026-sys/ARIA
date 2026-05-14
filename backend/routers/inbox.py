"""Inbox Router — inbox items listing, updates, deletion + the
agent-skill curl create endpoint.

Slice 5 of the multi-batch refactor (2026-04-30): consolidated the
inline POST /api/inbox/{tenant_id}/items handler from server.py into
this router along with its three companion helpers
(_looks_like_confirmation_message, _is_duplicate_media_write,
_merge_into_recent_social_row, _cleanup_media_placeholder). server.py
shrinks by ~500 lines; behavior is unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.auth import get_verified_tenant
from backend.services import inbox as inbox_service
from backend.services.realtime import sio, emit_task_completed as _emit_task_completed
from backend.services.supabase import get_db

logger = logging.getLogger("aria.routers.inbox")

router = APIRouter(tags=["Inbox"])


async def _verify_inbox_owner(request: Request, item_id: str) -> dict:
    """Look up the inbox row + verify the JWT user owns its tenant.

    Inbox PATCH / DELETE / restore routes take {item_id} (not tenant_id)
    in the path, so the standard router-level get_verified_tenant dep
    can't apply. This helper is the per-route equivalent — fetches the
    row by id, then runs get_verified_tenant against the row's tenant.

    Returns the row (id, tenant_id) on success, raises 404 if the row
    is missing and 403 (via get_verified_tenant) if the user doesn't
    own the tenant. Cost: one PK-equality lookup, sub-millisecond.
    """
    sb = get_db()
    try:
        result = (
            sb.table("inbox_items")
            .select("id, tenant_id")
            .eq("id", item_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.warning("[inbox] ownership lookup failed for %s: %s", item_id, e)
        raise HTTPException(status_code=404, detail="Inbox item not found")
    if not result.data:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    row = result.data[0]
    tenant_id = row.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Inbox item missing tenant_id")
    await get_verified_tenant(request, str(tenant_id))
    return row


@router.get("/api/inbox/item/{item_id}")
async def get_inbox_item(request: Request, item_id: str):
    """Fetch a single inbox row by id — the deep-link hydrator path.

    Used by the inbox page when a user navigates in with `?id=<uuid>`
    pointing at an item that isn't on the current paginated page (e.g.
    a 30-day-old draft, or an item the user filtered out). The
    frontend calls this, injects the returned row into its items
    state, and opens the detail pane — no need to load every page
    just to find one specific draft.

    Auth: keyed by item UUID alone (no tenant_id in URL), so we use
    the same _verify_inbox_owner pattern as the companion PATCH /
    DELETE / restore / resend handlers — fetches the row's tenant_id
    and runs it through get_verified_tenant. Raises 404 if the row
    doesn't exist, 403 if the caller doesn't own its tenant.
    Previously this route returned any inbox row to any
    authenticated user who could guess the UUID — a self-documented
    gap the rest of the file already closed.
    """
    await _verify_inbox_owner(request, item_id)
    sb = get_db()
    try:
        result = (
            sb.table("inbox_items")
            .select("*")
            .eq("id", item_id)
            .limit(1)
            .execute()
        )
        if not result.data:
            return {"item": None, "error": "not_found"}
        return {"item": result.data[0]}
    except Exception as e:
        logger.warning("[inbox] get_inbox_item failed for %s: %s", item_id, e)
        return {"item": None, "error": "fetch_failed"}


@router.get("/api/inbox/{tenant_id}/counts", dependencies=[Depends(get_verified_tenant)])
async def inbox_status_counts(tenant_id: str):
    """Return counts per status for inbox tabs."""
    return {"counts": inbox_service.status_counts(tenant_id)}


@router.get("/api/inbox/{tenant_id}", dependencies=[Depends(get_verified_tenant)])
async def list_inbox(tenant_id: str, status: str = "", page: int = 1, page_size: int = 20):
    """List inbox items for a tenant with pagination."""
    try:
        return inbox_service.list_items(tenant_id, status, page, page_size)
    except Exception as e:
        return {"items": [], "total": 0, "page": 1, "page_size": page_size, "total_pages": 1, "error": str(e)}


_EDITABLE_FIELDS = ("status", "title", "content", "metadata", "social_posts", "email_draft")

# Canonical inbox status set. Anything outside this is rejected from PATCH so
# typos / mis-cased strings (e.g. "Cancelled", "canceled") can't slip into the
# DB and break the Cancelled tab's eq("status","cancelled") filter.
_VALID_INBOX_STATUSES = frozenset({
    "processing",
    "ready",
    "draft_pending_approval",
    "needs_review",
    "sending",
    "sent",
    "completed",
    "failed",
    "cancelled",
})


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

    await _verify_inbox_owner(request, item_id)
    sb = get_db()
    body = await request.json() or {}
    updates: dict = {}

    # Pass-through scalar fields
    for key in ("status", "title", "content"):
        if key in body and body[key] is not None:
            updates[key] = body[key]

    # Status normalization + validation. Lowercase + strip so "Cancelled"
    # / "  cancelled  " all land as "cancelled". Reject anything outside
    # the canonical set with a 400 so a buggy caller can't silently
    # poison the row (e.g. "canceled" US-spelling -> Cancelled tab misses
    # it forever).
    if "status" in updates:
        raw_status = updates["status"]
        if isinstance(raw_status, str):
            normalized = raw_status.strip().lower()
            if normalized not in _VALID_INBOX_STATUSES:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid status {raw_status!r}; must be one of "
                    f"{sorted(_VALID_INBOX_STATUSES)}",
                )
            updates["status"] = normalized
        else:
            raise HTTPException(status_code=400, detail="status must be a string")

    # Capture the pre-edit row so we can diff for style memory below,
    # carry tenant_id into the socket payload without a second query,
    # AND merge metadata / email_draft instead of clobbering them
    # (image_url + other JSONB sidecar keys must survive partial edits).
    pre_row = None
    try:
        pre_row = (
            sb.table("inbox_items")
            .select("id, tenant_id, agent, content, email_draft, metadata")
            .eq("id", item_id)
            .single()
            .execute()
        ).data or None
    except Exception:
        pre_row = None

    # Structured fields — JSONB columns in Supabase. MERGE into existing
    # values instead of overwriting so the frontend's "send full row
    # back" save pattern can't accidentally wipe metadata.image_url /
    # email_draft.image_urls. Keys present in the incoming dict win;
    # keys not mentioned are preserved from the pre-edit row.
    if "metadata" in body and isinstance(body["metadata"], dict):
        existing_md = (pre_row or {}).get("metadata") if pre_row else None
        if isinstance(existing_md, dict):
            merged = dict(existing_md)
            merged.update(body["metadata"])
            updates["metadata"] = merged
        else:
            updates["metadata"] = body["metadata"]
    if "email_draft" in body and isinstance(body["email_draft"], dict):
        existing_draft = (pre_row or {}).get("email_draft") if pre_row else None
        if isinstance(existing_draft, dict):
            merged_draft = dict(existing_draft)
            merged_draft.update(body["email_draft"])
            updates["email_draft"] = merged_draft
        else:
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

    # Mirror inbox status -> Projects task status for ad_strategist
    # rows. Only fires when status actually moved to a mapped value
    # (approved/sent/needs_review/draft_pending_approval); the helper
    # is a no-op for unmapped statuses, so we can call unconditionally
    # for ad_strategist rows.
    if "status" in updates and pre_row and pre_row.get("agent") == "ad_strategist":
        try:
            from backend.services.projects import sync_task_status_from_inbox
            await asyncio.to_thread(
                sync_task_status_from_inbox,
                pre_row.get("tenant_id") or "",
                item_id,
                updates["status"],
            )
        except Exception as e:
            logger.debug("[inbox-patch] task status sync skipped: %s", e)

    return {"updated": item_id, **updates}


@router.delete("/api/inbox/{item_id}")
async def delete_inbox_item(item_id: str, request: Request, permanent: bool = False, reason: str = ""):
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
    await _verify_inbox_owner(request, item_id)
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
    #
    # CRITICAL: this branch must NEVER fall through to a hard DELETE.
    # We had a silent `except Exception: sb.delete()` fallback here
    # for ages — it swallowed every soft-cancel failure (JSONB merge
    # error, RLS hiccup, transient supabase-py error, anything) and
    # quietly nuked the row instead of leaving it intact for retry.
    # Symptom: bulk-cancel said "Moved to Cancelled" but rows actually
    # left the DB and the Cancelled tab stayed empty (counts response
    # had no `cancelled` key because zero rows landed in that status).
    # If the metadata merge is the failure point we retry once with a
    # minimal payload (status only) so the user's "stop this work"
    # intent still flips the row out of the active tabs; only after
    # that second try fails do we surface a 500 so the row stays in
    # the DB and the user can see something went wrong instead of
    # silently losing data.
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
    except Exception as e:
        logger.warning(
            "[inbox-delete] soft-cancel metadata write failed for %s: %s -- "
            "retrying with status-only update",
            item_id, e,
        )
        try:
            sb.table("inbox_items").update({
                "status": "cancelled",
            }).eq("id", item_id).execute()
        except Exception as e2:
            logger.error(
                "[inbox-delete] soft-cancel status-only retry ALSO failed for "
                "%s: %s -- leaving row intact and surfacing 500 (do NOT silently "
                "hard-delete; that masks data-loss bugs)", item_id, e2,
            )
            raise HTTPException(
                status_code=500,
                detail=f"soft-cancel failed: {e2}",
            )
    return {
        "deleted": item_id,
        "permanent": False,
        "soft": True,
        "previous_status": previous_status,
        "paperclip_cancelled": bool(paperclip_issue_id),
    }


@router.post("/api/inbox/{item_id}/restore")
async def restore_inbox_item(item_id: str, request: Request):
    """Move a cancelled inbox row back to its previous status (or
    needs_review if none was recorded). Clears cancel_reason from
    metadata but keeps the cancelled_at timestamp so we have a
    history trail.
    """
    await _verify_inbox_owner(request, item_id)
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


@router.post("/api/inbox/{item_id}/resend")
async def resend_inbox_item(item_id: str, request: Request):
    """Clone a sent (or failed) email inbox row as a fresh draft.

    Use case: user already sent an email but wants to send a follow-up
    or re-send to a different recipient. Once an inbox row's status
    flips to 'sent', the EmailEditor's Approve & Send button stops
    rendering (by design, to prevent accidental double-sends to the
    same recipient). This endpoint creates a NEW inbox row with the
    same email_draft cloned across, status reset to
    'draft_pending_approval' so the user can edit + re-approve.

    Source row stays intact -- this is a duplicate, not a re-activate.
    The duplicate gets a fresh paperclip_issue_id (None) and a title
    prefixed 'Resend:' so the user can tell them apart in the list.
    """
    # Same ownership gate every other {item_id} route uses
    src = await _verify_inbox_owner(request, item_id)

    sb = get_db()
    # Pull the full source row -- need email_draft + agent + tenant + title
    # so we can clone it faithfully.
    try:
        result = (
            sb.table("inbox_items")
            .select("tenant_id, agent, type, title, content, email_draft, priority, status")
            .eq("id", item_id)
            .single()
            .execute()
        )
    except Exception as e:
        logger.warning("[inbox-resend] source lookup failed for %s: %s", item_id, e)
        raise HTTPException(status_code=404, detail="Inbox item not found")
    row = result.data or {}
    if not row:
        raise HTTPException(status_code=404, detail="Inbox item not found")

    # Only resend rows that actually have an email payload. Resending a
    # non-email row would create a draft with no body -- not useful, so
    # 400 instead of silently producing a junk row.
    email_draft = row.get("email_draft") or None
    if not isinstance(email_draft, dict) or not email_draft:
        raise HTTPException(
            status_code=400,
            detail="This item has no email draft to resend.",
        )

    # Allow resend from sent / failed / cancelled. Drafts that are still
    # pending approval shouldn't need resending -- the user can just
    # edit + send the original.
    src_status = (row.get("status") or "").strip().lower()
    if src_status in ("draft_pending_approval", "processing", "sending"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resend — original is still {src_status}. Use the existing draft.",
        )

    # Reset email_draft.status to draft_pending_approval; preserve every
    # other field (to/subject/html_body/text_body/preview_snippet/
    # reply_to_thread_id, etc.) so the clone matches the original
    # content. Drop any sent-side metadata like provider_message_id since
    # this is a fresh send.
    cloned_draft = dict(email_draft)
    cloned_draft["status"] = "draft_pending_approval"
    cloned_draft.pop("provider_message_id", None)

    src_title = (row.get("title") or "Email draft").strip()
    new_title = (
        src_title if src_title.lower().startswith("resend:")
        else f"Resend: {src_title}"
    )[:200]

    new_row = {
        "tenant_id": row["tenant_id"],
        "agent": row.get("agent") or "email_marketer",
        "type": row.get("type") or "email_sequence",
        "title": new_title,
        "content": row.get("content") or "",
        "status": "draft_pending_approval",
        "priority": row.get("priority") or "normal",
        "email_draft": cloned_draft,
        # Don't carry forward paperclip_issue_id -- the original
        # delegation is closed; this new row stands on its own.
    }

    try:
        ins = sb.table("inbox_items").insert(new_row).execute()
    except Exception as e:
        logger.exception("[inbox-resend] insert failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to create resend draft")

    new_item = (ins.data or [None])[0]
    if not new_item:
        raise HTTPException(status_code=500, detail="Resend draft was not created")

    # Best-effort socket notify so the inbox list refreshes the moment
    # the new draft lands. If this fails the user just sees the new
    # draft on the next manual refresh.
    try:
        await sio.emit("inbox_new_item", {
            "tenant_id": row["tenant_id"],
            "item": new_item,
        }, room=row["tenant_id"])
    except Exception as e:
        logger.debug("[inbox-resend] socket emit failed (non-fatal): %s", e)

    return {
        "ok": True,
        "source_item_id": item_id,
        "new_item": new_item,
    }


# ──────────────────────────────────────────────────────────────────────────
# POST /api/inbox/{tenant_id}/items — agent skill-curl create endpoint
# ──────────────────────────────────────────────────────────────────────────
#
# Migrated from server.py in slice 5 (2026-04-30) along with its companion
# helpers below. All logic behavior is preserved verbatim — only the file
# location moved.
#
# Cross-module touchpoints (lazy-imported inside handler/helpers to avoid
# circular imports at module load time):
#   - sio (Socket.IO instance) lives in server.py
#   - _canon_agent_slug, _sanitize_social_post_text,
#     _parse_email_draft_from_text, _parse_social_drafts_from_text
#     all live in server.py and stay there pending future helper-extraction
#     slices

class CreateInboxItem(BaseModel):
    title: str
    content: str
    type: str = "blog"
    agent: str = "content_writer"
    priority: str = "medium"
    status: str = "needs_review"
    email_draft: dict | None = None
    paperclip_issue_id: str | None = None


def _looks_like_confirmation_message(content: str) -> bool:
    """True if the incoming content is an agent's "saved!" status message.

    These show up as SECOND inbox writes right after the agent's real
    content — rejecting them prevents duplicate rows with text like
    "✅ Email draft saved to ARIA Inbox" cluttering the inbox next to
    the actual email they're confirming.
    """
    text = (content or "").strip().lower()
    if not text:
        return False
    return (
        "saved to aria inbox" in text
        or "saved to inbox" in text
        or "successfully saved" in text
        or "draft created and saved" in text
        or "draft id:" in text
        or text.startswith((
            "✅",
            ":white_check_mark:",
            "[saved]",
            "[done]",
            "## task complete",
            "## email draft complete",
            "email draft created",
        ))
    )


def _is_duplicate_media_write(tenant_id: str, body: "CreateInboxItem") -> bool:
    """Reject duplicate media writes from the legacy aria-backend-api skill.

    When the Media Designer agent has both the new instructions AND the old
    aria-backend-api skill enabled, it hits TWO endpoints per image request:
    /api/media/.../generate (creates the canonical row with the rendered PNG)
    and /api/inbox/.../items (creates a text summary). The second is noise.

    If a media row for this tenant was created in the last 60s, treat any
    new media POST to /api/inbox/ as a duplicate. The 60s window is wide
    enough to cover Pollinations latency + agent reply lag, narrow enough
    that legitimate back-to-back requests still go through.
    """
    if body.agent != "media":
        return False
    try:
        sb = get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        existing = sb.table("inbox_items").select("id").eq("tenant_id", tenant_id).eq(
            "agent", "media"
        ).neq("status", "processing").gte("created_at", cutoff).limit(1).execute()
        return bool(existing.data)
    except Exception:
        return False


def _merge_into_recent_social_row(tenant_id: str, body: "CreateInboxItem") -> dict | None:
    """Merge a new social_post into a recent social_post row for the
    same tenant+agent, if one exists within the last 90 seconds.

    Agents sometimes split platforms into multiple POSTs (one for X,
    one for LinkedIn, one for Facebook). The frontend's platform-card
    UI only renders when all platforms live in one row's `content`
    JSON posts array. This helper finds the existing row and appends
    the incoming platforms to its posts array — no duplicate rows,
    all platforms render as cards inside a single inbox entry.

    Returns the updated row dict on successful merge, or None when
    no recent row exists and the caller should proceed with a normal
    insert.
    """
    try:
        sb = get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        recent = (
            sb.table("inbox_items")
            .select("id, content, title, status")
            .eq("tenant_id", tenant_id)
            .eq("agent", "social_manager")
            .eq("type", "social_post")
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not recent.data:
            return None
        existing = recent.data[0]
    except Exception as e:
        logger.debug("[social-merge] recent-row lookup failed: %s", e)
        return None

    def _extract_posts(text: str) -> list[dict]:
        if not text:
            return []
        try:
            s = text.find("{")
            e = text.rfind("}") + 1
            if s >= 0 and e > s:
                data = json.loads(text[s:e])
                posts = data.get("posts") or []
                if isinstance(posts, list):
                    return [p for p in posts if isinstance(p, dict)]
        except Exception:
            pass
        try:
            s = text.find("[")
            e = text.rfind("]") + 1
            if s >= 0 and e > s:
                parsed = json.loads(text[s:e])
                if isinstance(parsed, list):
                    return [p for p in parsed if isinstance(p, dict)]
        except Exception:
            pass
        return []

    existing_posts = _extract_posts(existing.get("content") or "")
    new_posts = _extract_posts(body.content or "")
    if not new_posts:
        return None

    # Merge by platform (case-insensitive). New platform text wins
    # when both rows have the same platform (latest data is freshest).
    by_platform: dict[str, dict] = {}
    for p in existing_posts:
        plat = (p.get("platform") or "").lower() or "unknown"
        by_platform[plat] = p
    for p in new_posts:
        plat = (p.get("platform") or "").lower() or "unknown"
        by_platform[plat] = p
    merged_posts = list(by_platform.values())

    merged_content = json.dumps({
        "action": "adapt_content",
        "posts": merged_posts,
    })

    title = existing.get("title") or body.title
    if title and any(t in title.lower() for t in (
        "linkedin post:", "twitter post:", "x post:", "facebook post:",
    )):
        for prefix in ("LinkedIn Post:", "Twitter Post:", "X Post:", "Facebook Post:"):
            if title.lower().startswith(prefix.lower()):
                title = "Social posts:" + title[len(prefix):]
                break

    try:
        sb = get_db()
        updated = (
            sb.table("inbox_items")
            .update({
                "content": merged_content,
                "title": title,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", existing["id"])
            .execute()
        )
        logger.info(
            "[social-merge] merged %d new platforms into row %s (total platforms: %d)",
            len(new_posts), existing["id"], len(merged_posts),
        )
        if updated.data:
            return updated.data[0]
        return existing
    except Exception as e:
        logger.warning("[social-merge] merge update failed: %s", e)
        return None


async def _cleanup_media_placeholder(tenant_id: str, keep_id: str | None) -> None:
    """Delete any 'processing' media inbox row for this tenant other than keep_id.

    Called whenever a finished media row is written via either path
    (/api/media/.../generate or /api/inbox/.../items) so the user doesn't
    see a stale 'Media is working on...' placeholder lingering after the
    real image arrives. Emits inbox_item_deleted so the frontend updates
    in real time.
    """
    if not tenant_id:
        return
    try:
        sb = get_db()
        q = sb.table("inbox_items").select("id").eq("tenant_id", tenant_id).eq(
            "agent", "media"
        ).eq("status", "processing")
        if keep_id:
            q = q.neq("id", keep_id)
        rows = q.execute().data or []
        for r in rows:
            pid = r.get("id")
            if not pid:
                continue
            try:
                sb.table("inbox_items").delete().eq("id", pid).execute()
            except Exception:
                continue
            try:
                await sio.emit("inbox_item_deleted", {"id": pid}, room=tenant_id)
            except Exception:
                pass
    except Exception:
        pass


@router.post("/api/inbox/{tenant_id}/items")
async def create_inbox_item(tenant_id: str, body: CreateInboxItem, request: Request):
    """Create an inbox item — used by Paperclip agents to store their output.

    Two paths can hit this endpoint:
      1. The agent's aria-backend-api skill curl from inside Paperclip
         (the agent's own POST after generating content)
      2. The watcher's _save_inbox_item fallback when its placeholder
         update fails

    For path 1, the agent rarely populates email_draft itself, so we
    parse the content here for the same email/social structured fields
    the watcher extracts. This is what makes the Approve & Send /
    Publish to X / Publish to LinkedIn buttons render in the inbox
    regardless of which write path created the row.

    Dedupe: if paperclip_issue_id is provided AND a row already exists
    for that issue (created by the watcher's placeholder), we UPDATE
    that row instead of creating a duplicate. The agent doesn't
    currently send paperclip_issue_id, but we accept it for the future
    when the skill MD is updated.

    Auth gate: this endpoint sits under /api/inbox/ which is in
    _PUBLIC_PREFIXES (JWT bypass) so the Paperclip-spawned Claude CLI
    can curl it from inside the container — same pattern as
    /api/media/.../generate. Without auth, anyone on the internet
    could inject arbitrary content into any tenant's inbox. We gate
    via a shared internal token (ARIA_INTERNAL_AGENT_TOKEN) sent in
    the `X-Aria-Agent-Token` header. Production refuses requests when
    the token isn't configured (fail-closed); dev still allows
    unauth'd with a warning to keep local smoke tests working. This
    mirrors the /api/media/{tenant_id}/generate gate in server.py.

    FIXME: After this lands, the Paperclip `aria-backend-api` skill MD
    needs to be updated (in Paperclip's instance — not in this repo)
    to send `X-Aria-Agent-Token: <ARIA_INTERNAL_AGENT_TOKEN>` on every
    POST. Until that update happens, Paperclip's inbox writes will 401
    and Path A (skill-curl) will fail. Path B (poll_completed_issues
    safety net in paperclip_office_sync.py) will still pull the agent
    output back, so user-visible behavior degrades from "near-instant"
    to "5-second poll" but doesn't break entirely.
    """
    # ── Auth gate ─────────────────────────────────────────────────────
    # Mirror /api/media/{tenant_id}/generate in server.py — same token,
    # same env var, same dev-mode bypass. Keep these parallel so the
    # next dev reading both sees they're the same.
    expected_token = (os.environ.get("ARIA_INTERNAL_AGENT_TOKEN") or "").strip()
    received_token = (request.headers.get("X-Aria-Agent-Token") or "").strip()
    if expected_token:
        if not received_token or received_token != expected_token:
            logger.warning(
                "[inbox-create] /api/inbox/%s/items rejected: bad/missing X-Aria-Agent-Token",
                tenant_id,
            )
            raise HTTPException(status_code=401, detail="Invalid agent token")
    elif (os.environ.get("ARIA_ENV") or os.environ.get("ENV") or "").lower() in ("prod", "production"):
        logger.error(
            "[inbox-create] ARIA_INTERNAL_AGENT_TOKEN not configured in production — refusing"
        )
        raise HTTPException(
            status_code=503,
            detail="Internal agent token not configured",
        )
    else:
        logger.warning(
            "[inbox-create] ARIA_INTERNAL_AGENT_TOKEN unset (dev mode) — accepting unauth'd request"
        )

    # sio + _emit_task_completed already imported at module top from
    # services/realtime.py. The remaining four still live in server.py
    # pending future helper-extraction work.
    from backend.server import (
        _canon_agent_slug,
        _sanitize_social_post_text,
        _parse_email_draft_from_text,
        _parse_social_drafts_from_text,
    )

    sb = get_db()

    # ── Pre-insert gates ──────────────────────────────────────────────
    if _looks_like_confirmation_message(body.content):
        logger.info(
            "[inbox-create] rejecting confirmation/status message from %s "
            "(content=%r)", body.agent, (body.content or "")[:120],
        )
        return {"item": None, "skipped": "confirmation_message"}

    if _is_duplicate_media_write(tenant_id, body):
        return {"item": None, "skipped": "duplicate_media_write"}

    # Normalize agent slug
    body.agent = _canon_agent_slug(body.agent) or body.agent

    # Strip agent meta-commentary
    sanitized = _sanitize_social_post_text(body.content or "")
    if sanitized and sanitized != body.content:
        body.content = sanitized

    # Social-post merge-window dedup
    if body.agent == "social_manager" and body.type == "social_post":
        merged = _merge_into_recent_social_row(tenant_id, body)
        if merged:
            return {"item": merged, "merged": True}

    title = body.title
    content_type = body.type
    status = body.status
    email_draft = body.email_draft

    if body.agent == "email_marketer":
        parsed = _parse_email_draft_from_text(body.content)
        if parsed:
            if email_draft:
                merged = dict(parsed)
                for k, v in email_draft.items():
                    if not v:
                        continue
                    if k == "subject" and isinstance(v, str) and v.lstrip().startswith("<"):
                        continue
                    if k == "to" and isinstance(v, str) and (v.startswith("<") or "font" in v.lower()):
                        continue
                    merged[k] = v
                email_draft = merged
            else:
                email_draft = parsed
            content_type = "email_sequence"
            status = "draft_pending_approval"
            parsed_subject = email_draft.get("subject", "") if email_draft else ""
            subject_is_clean = (
                parsed_subject
                and parsed_subject != "Untitled email"
                and not parsed_subject.lstrip().startswith("<")
            )
            if subject_is_clean:
                if not title or title.lower().startswith(("draft", "marketing email", "email", "untitled")):
                    title = f"Email: {parsed_subject}"

    # NOTE: Ad Strategist campaign briefs are intentionally chart-free.
    # Charts now live in the AI Report flow (campaign_analyzer.py +
    # routers/campaigns.py:_auto_generate_ai_report) which renders them
    # against actual uploaded performance metrics, not imagined budget
    # splits. If the brief still contains a [GRAPH_DATA] block here,
    # the agent's prompt is out of date — the block will pass through
    # as raw text rather than being rendered.

    if body.agent in ("content_writer", "social_manager"):
        social = _parse_social_drafts_from_text(body.content)
        if social or any(k in body.content.lower()[:500] for k in ("**twitter:**", "**linkedin:**", "**x:**", "**x/twitter:**")):
            content_type = "social_post"
        if content_type == "social_post":
            try:
                from backend.agents.social_manager_agent import (
                    _parse_posts as _sm_parse,
                    _sanitize_social_text as _sm_sanitize,
                )
                parsed_posts = _sm_parse(body.content)
                if parsed_posts:
                    body.content = json.dumps({
                        "action": "adapt_content",
                        "posts": parsed_posts,
                    })
                else:
                    cleaned = _sm_sanitize(body.content)
                    if cleaned:
                        body.content = cleaned
            except Exception:
                pass

    # ── Best-effort dedupe based on recent activity ────────────────────
    try:
        recent_window = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        recent = (
            sb.table("inbox_items")
            .select("id,content,type,status,title")
            .eq("tenant_id", tenant_id)
            .eq("agent", body.agent)
            .gte("created_at", recent_window)
            .order("created_at", desc=True)
            .limit(8)
            .execute()
        )
        rows = list(recent.data or [])

        # Strategy 1: processing placeholder — always upgrade it.
        for r in rows:
            r_title = (r.get("title") or "").lower()
            is_placeholder = (
                r.get("status") == "processing"
                or " is working on" in r_title
            )
            if not is_placeholder:
                continue
            update_data = {
                "title": title,
                "content": body.content,
                "type": content_type,
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if email_draft:
                update_data["email_draft"] = email_draft
            sb.table("inbox_items").update(update_data).eq("id", r["id"]).execute()
            logger.info(
                "[inbox-create] upgraded processing placeholder %s "
                "(agent=%s) with real agent output",
                r["id"], body.agent,
            )
            item_data = {"id": r["id"], "tenant_id": tenant_id, **update_data}
            if tenant_id:
                try:
                    await sio.emit("inbox_updated", {"action": "updated", "item": item_data}, room=tenant_id)
                except Exception:
                    pass
            return {"item": item_data, "deduped": True, "merged_placeholder": True}

        # Strategy 2: content-prefix match for double-POSTs of the same draft.
        for r in rows:
            r_content = (r.get("content") or "")[:300]
            new_prefix = (body.content or "")[:300]
            if r_content and new_prefix and len(r_content) > 50 and r_content[:100] == new_prefix[:100]:
                update_data = {
                    "title": title,
                    "content": body.content,
                    "type": content_type,
                    "status": status,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                if email_draft:
                    update_data["email_draft"] = email_draft
                sb.table("inbox_items").update(update_data).eq("id", r["id"]).execute()
                logger.info(
                    "[inbox-create] merged duplicate POST into existing row %s "
                    "(agent=%s, same content prefix)", r["id"], body.agent,
                )
                item_data = {"id": r["id"], "tenant_id": tenant_id, **update_data}
                if tenant_id:
                    try:
                        await sio.emit("inbox_updated", {"action": "updated", "item": item_data}, room=tenant_id)
                    except Exception:
                        pass
                return {"item": item_data, "deduped": True}
    except Exception as e:
        logger.debug("[inbox-create] recent-row dedupe lookup failed: %s", e)

    row = {
        "tenant_id": tenant_id,
        "title": title,
        "content": body.content,
        "type": content_type,
        "agent": body.agent,
        "priority": body.priority,
        "status": status,
    }
    if email_draft:
        row["email_draft"] = email_draft
    if body.paperclip_issue_id:
        row["paperclip_issue_id"] = body.paperclip_issue_id

    # Dedupe with the watcher's placeholder when we have an issue id
    item = None
    if body.paperclip_issue_id:
        try:
            existing = (
                sb.table("inbox_items")
                .select("id")
                .eq("tenant_id", tenant_id)
                .eq("paperclip_issue_id", body.paperclip_issue_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                placeholder_id = existing.data[0]["id"]
                update_data = {k: v for k, v in row.items() if k not in ("tenant_id",)}
                update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
                sb.table("inbox_items").update(update_data).eq("id", placeholder_id).execute()
                item = {"id": placeholder_id, **row}
                logger.info(
                    "[inbox-create] updated existing placeholder %s for paperclip_issue_id=%s",
                    placeholder_id, body.paperclip_issue_id,
                )
        except Exception as e:
            logger.warning(
                "[inbox-create] dedupe lookup failed: %s -- inserting fresh row", e,
            )

    if item is None:
        result = sb.table("inbox_items").insert(row).execute()
        item = result.data[0] if result.data else None

    # Emit real-time notification
    if item and tenant_id:
        await sio.emit("inbox_updated", {"action": "created", "item": item}, room=tenant_id)
        try:
            sb.table("notifications").insert({
                "tenant_id": tenant_id,
                "title": f"New from {body.agent}: {title}",
                "body": body.content[:200],
                "category": "inbox",
                "href": "/inbox",
            }).execute()
        except Exception:
            pass

    # Cleanup any leftover media placeholder for this tenant
    if body.agent == "media" and item:
        await _cleanup_media_placeholder(tenant_id, item.get("id"))

    # Completion log so Virtual Office Recent Activity shows "task done"
    if body.agent and item:
        try:
            from backend.orchestrator import log_agent_action as _log_agent_action
            await _log_agent_action(
                tenant_id, body.agent, "paperclip_completed",
                {"task": (item.get("title") or "")[:200], "inbox_item_id": item.get("id")},
            )
        except Exception:
            pass

    # Index the finalized row for cross-session recall
    if item:
        try:
            from backend.services.content_index import index_inbox_row
            await asyncio.to_thread(index_inbox_row, {**item, "tenant_id": tenant_id})
        except Exception:
            pass

    # Project-task mirror — Ad Strategist deliverables get a tasks row
    # so the Projects page can track the campaign as a Draft Ready /
    # In Progress / Done item with a Review button that deep-links
    # back to this inbox item. Narrow to ad_strategist for now; we'll
    # generalize once the flow proves out.
    if (
        item
        and body.agent == "ad_strategist"
        and len(body.content or "") >= 200
    ):
        project_task_row: dict | None = None
        campaign_title: str = item.get("title") or "Ad Campaign"
        project_meta: dict = {"source": "ad_strategist"}
        try:
            from backend.services.projects import (
                create_project_task,
                extract_campaign_metadata,
            )
            meta = extract_campaign_metadata(body.content or "")
            campaign_title = meta.pop("title", None) or (item.get("title") or "Ad Campaign")
            if meta:
                project_meta.update(meta)
            project_task_row = await asyncio.to_thread(
                create_project_task,
                tenant_id,
                agent="ad_strategist",
                inbox_item_id=item.get("id"),
                title=campaign_title,
                task=campaign_title,
                status="to_do",
                priority=body.priority or "medium",
                metadata=project_meta,
            )
        except Exception as e:
            logger.warning("[inbox-create] project-task mirror skipped: %s", e)

        # Campaigns mirror — same deliverable also lands in the
        # campaigns table so the Campaigns page Copy-Paste tab can
        # render it as a queued draft. Reuses the metadata dict from
        # the project-task mirror so we don't re-parse the markdown.
        # Triangle of links: inbox <-> tasks <-> campaigns.
        try:
            from backend.services.campaigns import create_campaign_from_inbox
            await asyncio.to_thread(
                create_campaign_from_inbox,
                tenant_id,
                inbox_item_id=item.get("id"),
                task_id=(project_task_row or {}).get("id"),
                title=campaign_title,
                status="draft",
                metadata=project_meta,
            )
        except Exception as e:
            logger.warning("[inbox-create] campaigns mirror skipped: %s", e)

    return {"item": item}
