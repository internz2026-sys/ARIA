"""Email Router — every /api/email/... endpoint lives here.

Handlers are thin adapters that:
  - validate the request (Pydantic models below)
  - call into the service modules (email_sender, email_parser, email_template)
  - touch Supabase directly only for inbox / thread row updates specific
    to the email feature

Server-level state (Socket.IO, notification helpers, confirmation gate)
is lazy-imported from backend.server INSIDE each handler that needs it
so this module can be imported at FastAPI setup without triggering a
circular import on backend.server.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config.loader import get_tenant_config
from backend.services.email_sender import (
    resolve_reply_thread_context,
    send_with_refresh,
    user_text_to_html,
)
from backend.services.supabase import get_db

logger = logging.getLogger("aria.routers.email")

router = APIRouter(tags=["Email"])


# ─── Pydantic request models ──────────────────────────────────────────


class GmailSendRequest(BaseModel):
    to: str
    subject: str
    html_body: str


class EmailApproveRequest(BaseModel):
    inbox_item_id: str


class UpdateDraftRequest(BaseModel):
    inbox_item_id: str
    to: str = ""
    subject: str = ""
    html_body: str = ""


class CancelDraftRequest(BaseModel):
    inbox_item_id: str
    # Optional reason the user rejected this draft. Written to
    # inbox_items.cancel_reason (column added by create_style_memory.sql)
    # and replayed into future agent prompts via summarize_cancel_reasons.
    reason: str = ""


class DraftReplyRequest(BaseModel):
    thread_id: str
    custom_instructions: str = ""


class SendReplyRequest(BaseModel):
    body: str
    subject: str = ""


# ─── Generic send ─────────────────────────────────────────────────────


@router.post("/api/email/{tenant_id}/send")
async def send_gmail_email(tenant_id: str, body: GmailSendRequest, confirmed: bool = False):
    """Send an email via the user's authenticated Gmail account.

    Requires confirmed=true — human must explicitly approve before sending.
    """
    # Confirmation gate lives in server.py for now (used by many routes).
    from backend.server import _require_confirmation

    gate = _require_confirmation(
        "send_email", confirmed, f"Send email to {body.to}?\n\nSubject: {body.subject}"
    )
    if gate:
        return gate

    result = await send_with_refresh(
        tenant_id,
        to=body.to,
        subject=body.subject,
        html_body=body.html_body,
    )
    if result.get("error"):
        detail = result.get("detail", "Gmail API error")
        raise HTTPException(status_code=result.get("status_code", 401), detail=detail)

    return {"status": "sent", "message_id": result.get("message_id", "")}


# ─── Approve-and-send pending draft ───────────────────────────────────


@router.post("/api/email/{tenant_id}/approve-send")
async def approve_and_send_email(tenant_id: str, body: EmailApproveRequest):
    """Approve a pending email draft and send it via Gmail.

    Only sends drafts in 'draft_pending_approval' or 'failed' status.
    Updates the inbox item through the lifecycle:
    draft_pending_approval → sending → sent / failed.
    """
    from backend.server import sio, _notify

    sb = get_db()

    item_result = (
        sb.table("inbox_items").select("*").eq("id", body.inbox_item_id).single().execute()
    )
    item = item_result.data
    if not item:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    if item.get("status") not in ("draft_pending_approval", "failed"):
        raise HTTPException(
            status_code=400,
            detail=f"Item is not a pending draft (status: {item.get('status')})",
        )
    if item.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")

    meta = item.get("email_draft") or {}
    to = meta.get("to", "")
    subject = meta.get("subject", "")
    html_body = meta.get("html_body", "")

    if not to or not subject or not html_body:
        raise HTTPException(
            status_code=400,
            detail="Email draft is missing required fields (to, subject, or body)",
        )

    sb.table("inbox_items").update({
        "status": "sending",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).execute()

    def _mark_failed() -> None:
        sb.table("inbox_items").update({
            "status": "failed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", body.inbox_item_id).execute()

    config = get_tenant_config(tenant_id)
    reply_thread_id = ""
    reply_in_reply_to = ""
    reply_to_thread_db_id = meta.get("reply_to_thread_id", "")
    if reply_to_thread_db_id:
        reply_thread_id, reply_in_reply_to = await resolve_reply_thread_context(
            tenant_id, reply_to_thread_db_id,
            access_token=config.integrations.google_access_token or "",
        )

    try:
        result = await send_with_refresh(
            tenant_id,
            to=to,
            subject=subject,
            html_body=html_body,
            thread_id=reply_thread_id,
            in_reply_to=reply_in_reply_to,
        )
    except HTTPException:
        _mark_failed()
        raise

    if result.get("error"):
        _mark_failed()
        raise HTTPException(status_code=500, detail=f"Email send failed: {result['error']}")

    sb.table("inbox_items").update({
        "status": "sent",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).execute()

    # ── Thread tracking: persist outbound message for future reply matching ──
    gmail_message_id = result.get("message_id", "")
    gmail_thread_id = result.get("thread_id", "")
    thread_db_id = None
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        if gmail_thread_id:
            existing = (
                sb.table("email_threads")
                .select("id")
                .eq("tenant_id", tenant_id)
                .eq("gmail_thread_id", gmail_thread_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                thread_db_id = existing.data[0]["id"]
                sb.table("email_threads").update({
                    "last_message_at": now_iso,
                    "status": "awaiting_reply",
                    "updated_at": now_iso,
                }).eq("id", thread_db_id).execute()

        if not thread_db_id:
            thread_row = {
                "tenant_id": tenant_id,
                "gmail_thread_id": gmail_thread_id or None,
                "contact_email": to,
                "subject": subject,
                "status": "awaiting_reply",
                "last_message_at": now_iso,
                "inbox_item_id": body.inbox_item_id,
            }
            t_result = sb.table("email_threads").insert(thread_row).execute()
            if t_result.data:
                thread_db_id = t_result.data[0]["id"]

        if thread_db_id:
            text_body = meta.get("text_body", "")
            preview = meta.get("preview_snippet", "")
            sb.table("email_messages").insert({
                "thread_id": thread_db_id,
                "tenant_id": tenant_id,
                "gmail_message_id": gmail_message_id or None,
                "direction": "outbound",
                "sender": config.owner_email,
                "recipients": to,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body,
                "preview_snippet": preview,
                "message_timestamp": now_iso,
                "approval_status": "sent",
            }).execute()
    except Exception as e:
        logger.warning("Thread tracking failed (email still sent): %s", e)

    await sio.emit("inbox_item_updated", {
        "id": body.inbox_item_id,
        "status": "sent",
    }, room=tenant_id)
    await sio.emit("email_thread_updated", {
        "thread_id": gmail_thread_id,
        "status": "awaiting_reply",
    }, room=tenant_id)
    await _notify(
        tenant_id, "email_sent", f"Email sent to {to}",
        body=subject, href="/conversations",
        category="status", priority="normal",
    )

    return {"status": "sent", "message_id": gmail_message_id, "thread_id": gmail_thread_id}


# ─── Draft CRUD (update / cancel) ─────────────────────────────────────


@router.post("/api/email/{tenant_id}/update-draft")
async def update_email_draft(tenant_id: str, body: UpdateDraftRequest):
    """Update an email draft's to, subject, or body before sending."""
    sb = get_db()

    item_result = (
        sb.table("inbox_items").select("*").eq("id", body.inbox_item_id).single().execute()
    )
    item = item_result.data
    if not item:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    if item.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    if item.get("status") not in ("draft_pending_approval", "failed"):
        raise HTTPException(status_code=400, detail="Draft is not editable")

    draft = item.get("email_draft") or {}
    if body.to:
        draft["to"] = body.to
    if body.subject:
        draft["subject"] = body.subject
    if body.html_body:
        draft["html_body"] = body.html_body
        text = re.sub(r"<[^>]+>", "", body.html_body).strip()
        draft["text_body"] = text
        draft["preview_snippet"] = text[:200]

    sb.table("inbox_items").update({
        "email_draft": draft,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).execute()

    return {"ok": True, "email_draft": draft}


@router.post("/api/email/{tenant_id}/cancel-draft")
async def cancel_email_draft(tenant_id: str, body: CancelDraftRequest):
    """Cancel a pending email draft, optionally capturing a reason."""
    sb = get_db()
    updates: dict = {
        "status": "cancelled",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    reason = (body.reason or "").strip()
    if reason:
        updates["cancel_reason"] = reason[:500]
    try:
        sb.table("inbox_items").update(updates).eq(
            "id", body.inbox_item_id
        ).eq("tenant_id", tenant_id).execute()
    except Exception:
        # Column might not exist yet (migration not applied). Retry
        # without the reason so the cancel itself still succeeds.
        sb.table("inbox_items").update({
            "status": "cancelled",
            "updated_at": updates["updated_at"],
        }).eq("id", body.inbox_item_id).eq("tenant_id", tenant_id).execute()
    return {"ok": True}


# ─── Threads list / get / mark-read ───────────────────────────────────


@router.get("/api/email/{tenant_id}/threads")
async def list_email_threads(tenant_id: str, status: str = ""):
    """List email conversation threads for a tenant."""
    sb = get_db()
    query = sb.table("email_threads").select("*").eq("tenant_id", tenant_id)
    if status:
        query = query.eq("status", status)
    result = query.order("last_message_at", desc=True).execute()
    return {"threads": result.data or []}


@router.get("/api/email/{tenant_id}/threads/{thread_id}")
async def get_email_thread(tenant_id: str, thread_id: str):
    """Get a single thread with all its messages."""
    sb = get_db()
    thread_result = (
        sb.table("email_threads")
        .select("*")
        .eq("id", thread_id)
        .eq("tenant_id", tenant_id)
        .single()
        .execute()
    )
    if not thread_result.data:
        raise HTTPException(status_code=404, detail="Thread not found")

    messages_result = (
        sb.table("email_messages")
        .select("*")
        .eq("thread_id", thread_id)
        .order("message_timestamp", desc=False)
        .execute()
    )

    return {
        "thread": thread_result.data,
        "messages": messages_result.data or [],
    }


@router.post("/api/email/{tenant_id}/threads/{thread_id}/mark-read")
async def mark_thread_read(tenant_id: str, thread_id: str):
    """Mark a thread as read (status → open)."""
    sb = get_db()
    sb.table("email_threads").update({
        "status": "open",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", thread_id).eq("tenant_id", tenant_id).execute()
    return {"ok": True}


# ─── Agent-drafted reply on a thread ─────────────────────────────────


@router.post("/api/email/{tenant_id}/draft-reply")
async def generate_draft_reply(tenant_id: str, body: DraftReplyRequest):
    """Generate a suggested reply draft for an email thread.

    Uses the email marketer agent to draft a contextual reply based on
    the thread history. The draft is saved as draft_pending_approval —
    never sent.
    """
    from backend.tools.claude_cli import call_claude, MODEL_HAIKU

    sb = get_db()

    thread_result = (
        sb.table("email_threads")
        .select("*")
        .eq("id", body.thread_id)
        .eq("tenant_id", tenant_id)
        .single()
        .execute()
    )
    if not thread_result.data:
        raise HTTPException(status_code=404, detail="Thread not found")
    thread = thread_result.data

    messages_result = (
        sb.table("email_messages")
        .select("*")
        .eq("thread_id", body.thread_id)
        .order("message_timestamp", desc=False)
        .execute()
    )
    messages = messages_result.data or []

    if not messages:
        raise HTTPException(status_code=400, detail="No messages in this thread to reply to")

    config = get_tenant_config(tenant_id)
    conversation = ""
    for msg in messages:
        direction = "SENT" if msg["direction"] == "outbound" else "RECEIVED"
        sender = msg.get("sender", "")
        body_text = msg.get("text_body", "") or msg.get("preview_snippet", "")
        conversation += (
            f"\n[{direction}] From: {sender}\nSubject: {msg.get('subject', '')}\n{body_text}\n---\n"
        )

    latest_inbound = None
    for msg in reversed(messages):
        if msg["direction"] == "inbound":
            latest_inbound = msg
            break
    if not latest_inbound:
        raise HTTPException(status_code=400, detail="No inbound message to reply to")

    instructions = body.custom_instructions or "Write a helpful, professional reply."

    system_prompt = f"""You are the Email Marketer for {config.business_name}.
Brand voice: {config.brand_voice.tone}
Business: {config.description}

Write a reply email based on the conversation thread below.
{instructions}

Output format:
SUBJECT: Re: <original subject>
---
<email body in HTML>

Keep it professional, concise, and on-brand. Do not include placeholder text."""

    user_prompt = (
        f"Thread conversation:\n{conversation}\n\n"
        "Draft a reply to the latest inbound message."
    )

    raw = await call_claude(system_prompt, user_prompt, max_tokens=1500, model=MODEL_HAIKU)

    subject_match = re.match(
        r"(?:SUBJECT:\s*)(.+?)(?:\n---\n|\n\n)(.*)",
        raw, re.DOTALL | re.IGNORECASE,
    )
    if subject_match:
        reply_subject = subject_match.group(1).strip()
        reply_body = subject_match.group(2).strip()
    else:
        reply_subject = f"Re: {thread.get('subject', '')}"
        reply_body = raw.strip()

    from backend.agents.email_marketer_agent import _wrap_html
    html_body = _wrap_html(reply_body)
    text_body = re.sub(r"<[^>]+>", "", reply_body).strip()
    preview_snippet = text_body[:200]

    now_iso = datetime.now(timezone.utc).isoformat()
    draft_row = {
        "thread_id": body.thread_id,
        "tenant_id": tenant_id,
        "direction": "outbound",
        "sender": config.owner_email,
        "recipients": thread.get("contact_email", ""),
        "subject": reply_subject,
        "text_body": text_body,
        "html_body": html_body,
        "preview_snippet": preview_snippet,
        "message_timestamp": now_iso,
        "approval_status": "draft_pending_approval",
    }
    msg_result = sb.table("email_messages").insert(draft_row).execute()
    draft_msg = msg_result.data[0] if msg_result.data else {}

    inbox_row = {
        "tenant_id": tenant_id,
        "agent": "email_marketer",
        "type": "email_sequence",
        "title": f"Draft Reply: {reply_subject}",
        "content": preview_snippet,
        "status": "draft_pending_approval",
        "priority": "high",
        "email_draft": {
            "to": thread.get("contact_email", ""),
            "subject": reply_subject,
            "html_body": html_body,
            "text_body": text_body,
            "preview_snippet": preview_snippet,
            "status": "draft_pending_approval",
            "reply_to_thread_id": body.thread_id,
            "reply_to_message_id": draft_msg.get("id", ""),
        },
    }
    inbox_result = sb.table("inbox_items").insert(inbox_row).execute()
    inbox_item = inbox_result.data[0] if inbox_result.data else {}

    sb.table("email_threads").update({
        "status": "replied",
        "updated_at": now_iso,
    }).eq("id", body.thread_id).execute()

    return {
        "draft": {
            "message_id": draft_msg.get("id", ""),
            "inbox_item_id": inbox_item.get("id", ""),
            "to": thread.get("contact_email", ""),
            "subject": reply_subject,
            "preview_snippet": preview_snippet,
            "status": "draft_pending_approval",
        },
    }


# ─── Direct user-authored reply on a thread ──────────────────────────


@router.post("/api/email/{tenant_id}/threads/{thread_id}/send-reply")
async def send_thread_reply(tenant_id: str, thread_id: str, body: SendReplyRequest):
    """Send a user-authored reply directly on an existing email thread.

    Unlike /draft-reply (which drafts via the agent and goes through
    inbox approval), this sends immediately with the user's own text.
    Gmail threadId + In-Reply-To are preserved so the reply stays in
    the same conversation in Gmail and in other mail clients.
    """
    from backend.agents.email_marketer_agent import _wrap_html
    from backend.server import sio

    sb = get_db()

    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Reply body cannot be empty")

    thread_result = (
        sb.table("email_threads")
        .select("*")
        .eq("id", thread_id)
        .eq("tenant_id", tenant_id)
        .single()
        .execute()
    )
    if not thread_result.data:
        raise HTTPException(status_code=404, detail="Thread not found")
    thread = thread_result.data
    contact_email = thread.get("contact_email", "")
    if not contact_email:
        raise HTTPException(
            status_code=400,
            detail="Thread has no contact email to reply to",
        )

    original_subject = thread.get("subject", "") or ""
    subject = (body.subject or "").strip() or (
        original_subject
        if original_subject.lower().startswith("re:")
        else f"Re: {original_subject}" if original_subject else "Re:"
    )

    html_body = _wrap_html(user_text_to_html(text))

    config = get_tenant_config(tenant_id)
    gmail_thread_id, in_reply_to = await resolve_reply_thread_context(
        tenant_id, thread_id,
        access_token=config.integrations.google_access_token or "",
    )

    result = await send_with_refresh(
        tenant_id,
        to=contact_email,
        subject=subject,
        html_body=html_body,
        thread_id=gmail_thread_id,
        in_reply_to=in_reply_to,
    )

    if result.get("error"):
        detail = result.get("detail") or result.get("error") or "Gmail send failed"
        raise HTTPException(status_code=500, detail=f"Email send failed: {detail}")

    now_iso = datetime.now(timezone.utc).isoformat()
    sb.table("email_messages").insert({
        "thread_id": thread_id,
        "tenant_id": tenant_id,
        "gmail_message_id": result.get("message_id") or None,
        "direction": "outbound",
        "sender": config.owner_email,
        "recipients": contact_email,
        "subject": subject,
        "text_body": text,
        "html_body": html_body,
        "preview_snippet": text[:200],
        "message_timestamp": now_iso,
        "approval_status": "sent",
    }).execute()

    sb.table("email_threads").update({
        "status": "awaiting_reply",
        "last_message_at": now_iso,
        "gmail_thread_id": gmail_thread_id or result.get("thread_id") or None,
        "updated_at": now_iso,
    }).eq("id", thread_id).execute()

    await sio.emit("email_thread_updated", {
        "thread_id": thread_id,
        "gmail_thread_id": gmail_thread_id or result.get("thread_id", ""),
        "status": "awaiting_reply",
    }, room=tenant_id)

    return {
        "status": "sent",
        "message_id": result.get("message_id", ""),
        "thread_id": thread_id,
        "gmail_thread_id": gmail_thread_id or result.get("thread_id", ""),
    }


# ─── Gmail sync (manual trigger + sync-all for cron) ─────────────────


@router.post("/api/email/{tenant_id}/sync")
async def trigger_email_sync(tenant_id: str):
    """Manually trigger Gmail inbound reply sync for a tenant."""
    from backend.server import _emit_sync_events
    from backend.tools.gmail_sync import sync_tenant_replies

    result = await sync_tenant_replies(tenant_id)
    await _emit_sync_events(tenant_id, result)
    return result


@router.post("/api/email/sync-all")
async def trigger_sync_all():
    """Trigger Gmail sync for all active tenants. Called by cron."""
    from backend.server import _emit_sync_events
    from backend.tools.gmail_sync import sync_all_tenants

    results = await sync_all_tenants()
    for r in results:
        tid = r.get("tenant_id", "")
        if tid:
            await _emit_sync_events(tid, r)
    return {"tenants_synced": len(results), "results": results}
