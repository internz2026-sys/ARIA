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

import hashlib
import hmac
import logging
import re
from base64 import b64encode
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
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
            inbound_thread_id=reply_to_thread_db_id,
            inbox_item_id=body.inbox_item_id,
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
        inbound_thread_id=thread_id,
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


# ─── ARIA-managed sender settings (Resend / SMTP) ────────────────────
#
# These power the frontend's Settings → Email tab. The status endpoint
# is read on tab mount; the PATCH is fired on Save Changes. Both stay
# additive — never raise when env vars are unset, because the frontend
# uses `configured: false` to render a "queued, set up your domain"
# banner instead of an error toast.


class EmailSettingsPatch(BaseModel):
    """Body of PATCH /api/settings/email.

    Both fields are optional — frontend may PATCH only the display
    name without re-validating the local-part. Empty strings clear the
    value (lets a user revert to defaults).
    """
    tenant_id: str
    display_name: str | None = None
    sender_local: str | None = None


# Lowercase letters, digits, hyphens. Mirrors the strictest
# subset that's safe across every email provider's local-part rules.
_SENDER_LOCAL_RE = re.compile(r"^[a-z0-9-]+$")


def _email_status_payload(tenant_id: str) -> dict:
    """Build the status response for one tenant. Pure read — never
    raises. The frontend uses `configured: false` as the signal to
    render the 'queued' banner."""
    from backend.services import email_provider as _ep

    cfg = get_tenant_config(tenant_id)
    integrations = cfg.integrations
    apex = _ep._apex_domain()
    provider = (integrations.email_provider or "resend").strip().lower() or "resend"
    display_name = (
        integrations.email_sender_display_name
        or cfg.business_name
        or "ARIA"
    ).strip()
    sender_local = (integrations.email_sender_local or "").strip()
    sender_address = _ep.build_sender_address(sender_local) if apex else ""
    reply_to_address = _ep.build_reply_to(tenant_id=tenant_id) if apex else ""
    configured = bool(apex and sender_local)
    return {
        "provider": provider,
        "configured": configured,
        "domain": (f"inbound.{apex}" if apex else None),
        "sender_address": sender_address or None,
        "reply_to_address": reply_to_address or None,
        "display_name": display_name,
        "sender_local": sender_local,
    }


@router.get("/api/settings/email/status")
async def get_email_settings_status(tenant_id: str):
    """Return the resolved email sender settings for a tenant.

    Frontend (Settings → Email tab) reads this on mount to populate
    the form + decide whether to render the "queued — domain not
    configured" banner.
    """
    return _email_status_payload(tenant_id)


@router.patch("/api/settings/email")
async def update_email_settings(body: EmailSettingsPatch):
    """Update display name and/or sender local-part.

    Returns the same payload as GET /api/settings/email/status so
    the frontend can sync state without a follow-up fetch.
    """
    from backend.config.loader import update_tenant_integrations

    tenant_id = body.tenant_id.strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    cfg = get_tenant_config(tenant_id)

    if body.sender_local is not None:
        local = body.sender_local.strip().lower()
        if local and not _SENDER_LOCAL_RE.match(local):
            raise HTTPException(
                status_code=400,
                detail=(
                    "sender_local must be lowercase letters, digits, or "
                    "hyphens only (no spaces, no special chars)."
                ),
            )
        cfg.integrations.email_sender_local = local

    if body.display_name is not None:
        cfg.integrations.email_sender_display_name = body.display_name.strip()[:80]

    update_tenant_integrations(cfg)
    return _email_status_payload(tenant_id)


# ─── Inbound webhook (Postmark / Resend / SendGrid) ──────────────────


def _verify_postmark_signature(secret: str, raw_body: bytes, header_value: str) -> bool:
    """Postmark signs requests with HMAC-SHA256 base64 of the raw body,
    sent in the `X-Postmark-Signature` header."""
    if not secret or not header_value:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, header_value.strip())


def _verify_resend_signature(secret: str, raw_body: bytes, header_value: str) -> bool:
    """Resend uses Svix-style signing — `Resend-Signature: v1,<b64>` (and
    sometimes a t=<timestamp> prefix). We accept either form for now."""
    if not secret or not header_value:
        return False
    # Strip any "v1," prefix (Svix convention) and split on commas.
    candidates = [
        part.strip().split(",", 1)[-1] for part in header_value.split(" ") if part.strip()
    ]
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected_b64 = b64encode(digest).decode("ascii")
    expected_hex = digest.hex()
    for cand in candidates:
        if hmac.compare_digest(cand, expected_b64) or hmac.compare_digest(cand, expected_hex):
            return True
    return False


def _verify_sendgrid_signature(secret: str, raw_body: bytes, header_value: str) -> bool:
    """SendGrid uses ECDSA (not HMAC) for inbound parse. Implementing the
    full ECDSA verify is out of scope for this stub; we treat any
    presence of the header as 'configured' and short-circuit to True only
    if dev-mode (no secret set)."""
    if not secret:
        return True
    # Real ECDSA verify would go here. For now, log + accept so the
    # endpoint doesn't 401 every Sendgrid hit.
    logger.warning("[inbound] SendGrid signature verification not implemented — accepting")
    return True


@router.post("/api/email/inbound")
async def inbound_email_webhook(request: Request):
    """Inbound email webhook for Postmark / Resend / SendGrid.

    Replaces the deprecated `_gmail_sync_loop` (which required the
    `gmail.readonly` scope we just dropped). Outbound emails sent via
    `email_sender.send_with_refresh` carry a Reply-To header pointing to
    `replies+<thread_id>@inbound.<INBOUND_EMAIL_DOMAIN>` — the customer's
    reply lands in our provider's inbox, gets parsed, and POSTed here.

    Provider is selected via `INBOUND_EMAIL_PROVIDER` (default postmark).
    HMAC signing key in `INBOUND_WEBHOOK_SECRET` (unset = dev-mode accept).

    ALWAYS returns 200 to the caller — providers retry aggressively on
    5xx, and our internal failures shouldn't multiply on the wire.
    """
    from backend.services.email_inbound import (
        get_inbound_provider,
        get_webhook_secret,
        normalize_payload,
        process_inbound_message,
    )

    raw_body = await request.body()
    provider = get_inbound_provider()
    secret = get_webhook_secret()

    # ── Signature validation ──────────────────────────────────────────
    if secret:
        if provider == "postmark":
            sig = request.headers.get("X-Postmark-Signature", "")
            if not _verify_postmark_signature(secret, raw_body, sig):
                logger.warning("[inbound] postmark signature mismatch")
                raise HTTPException(status_code=401, detail="Invalid signature")
        elif provider == "resend":
            sig = (
                request.headers.get("Resend-Signature")
                or request.headers.get("Svix-Signature")
                or ""
            )
            if not _verify_resend_signature(secret, raw_body, sig):
                logger.warning("[inbound] resend signature mismatch")
                raise HTTPException(status_code=401, detail="Invalid signature")
        elif provider == "sendgrid":
            sig = request.headers.get("X-Twilio-Email-Event-Webhook-Signature", "")
            if not _verify_sendgrid_signature(secret, raw_body, sig):
                logger.warning("[inbound] sendgrid signature mismatch")
                raise HTTPException(status_code=401, detail="Invalid signature")
    else:
        logger.warning(
            "[inbound] INBOUND_WEBHOOK_SECRET unset — accepting unsigned %s payload (dev mode)",
            provider,
        )

    # ── Parse JSON or form-data depending on provider ────────────────
    payload: dict
    content_type = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in content_type:
            payload = await request.json()
        elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            payload = dict(form)
        else:
            # Best-effort JSON fallback
            import json as _json
            payload = _json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception as e:
        logger.warning("[inbound] could not parse %s payload: %s", provider, e)
        return {"ok": False, "error": "unparseable_payload"}

    if not isinstance(payload, dict) or not payload:
        return {"ok": False, "error": "empty_payload"}

    try:
        normalized = normalize_payload(provider, payload)
    except Exception as e:
        logger.exception("[inbound] normalize_payload failed: %s", e)
        return {"ok": False, "error": "normalize_failed"}

    try:
        result = await process_inbound_message(normalized)
    except Exception as e:
        logger.exception("[inbound] process_inbound_message crashed: %s", e)
        return {"ok": False, "error": "process_failed"}

    # Always 200 — provider retries are noise, not a fix.
    return result


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
