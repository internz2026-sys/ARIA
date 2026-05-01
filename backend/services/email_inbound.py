"""Email Inbound — webhook normalization + thread/message persistence.

Replaces the deprecated `_gmail_sync_loop` (which required `gmail.readonly`
scope) with a provider-agnostic inbound webhook receiver.

Flow:
  1. Outbound emails (sent via `email_sender.send_with_refresh`) carry a
     `Reply-To: replies+<token>@inbound.<INBOUND_EMAIL_DOMAIN>` header.
     The token is the `email_threads.id` (uuid).
  2. Customer hits Reply. Their MTA delivers the message to Postmark /
     Resend / SendGrid (whichever is configured), which POSTs the parsed
     payload to `/api/email/inbound`.
  3. The router calls `process_inbound_message(...)` here. We parse the
     token, look up the matching `email_threads` row, insert an
     `email_messages` row with `direction='inbound'`, flip the thread
     back to `open`, emit `inbox_updated`, fire a `notification`.

Idempotency is provided by the existing UNIQUE INDEX on
`email_messages.gmail_message_id` (re-purposed as a generic provider
message id slot — a single column, since we'll only ever have ONE
inbound provider active per tenant).

Provider-specific normalizers live in this module so the router stays
thin: it does signature validation + routing, and never touches the raw
provider payload shape.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, TypedDict

from backend.services.supabase import get_db

logger = logging.getLogger("aria.services.email_inbound")


# ── Normalized inbound email shape ───────────────────────────────────────


class NormalizedInboundEmail(TypedDict):
    """Common shape every provider's payload is mapped onto before
    `process_inbound_message` consumes it. Keeps the persistence path
    provider-agnostic."""
    from_email: str
    from_name: str
    to_token: str            # the "<token>" from replies+<token>@...
    to_full: str             # the full reconstructed inbound address
    subject: str
    text_body: str
    html_body: str
    in_reply_to: str         # value of the In-Reply-To header (if present)
    provider_message_id: str # used for idempotent inserts
    received_at: str         # iso timestamp
    raw_provider_payload: dict[str, Any]


# ── Token parsing ────────────────────────────────────────────────────────


# A bare uuid v4-ish (also matches v1/v3/v5 — we don't enforce variant bits)
_UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)


def extract_to_token(to_address: str) -> str:
    """Extract the `<token>` portion from `replies+<token>@inbound.<domain>`.

    Tolerates a few real-world variants:
      - "replies+<uuid>@inbound.example.com" (canonical)
      - "Replies <replies+<uuid>@inbound.example.com>" (named address)
      - "<tenant_short>_<uuid>" (future tenant-prefix form — we strip the
        prefix and return the trailing uuid)

    Returns "" when no token can be parsed.
    """
    if not to_address:
        return ""

    addr = to_address.strip()
    # If wrapped in "Name <foo@bar>" format, peel off the angle-bracket part.
    angle = re.search(r"<([^>]+)>", addr)
    if angle:
        addr = angle.group(1).strip()

    # Pull the local-part (before "@") and split off any "replies+" prefix.
    local = addr.split("@", 1)[0]
    if "+" in local:
        local = local.split("+", 1)[1]
    # Otherwise the whole local-part is the token (some providers strip the
    # plus-extension during forwarding).

    # If the token is `<tenant_short>_<uuid>`, the embedded uuid is what we
    # need. The bare-uuid regex below picks it out either way.
    m = _UUID_RE.search(local)
    if m:
        return m.group(1)

    # Fallback: the whole local-part might be the token (non-uuid scheme).
    return local or ""


# ── Provider-specific normalizers ────────────────────────────────────────


def normalize_postmark_payload(payload: dict[str, Any]) -> NormalizedInboundEmail:
    """Map a Postmark inbound webhook payload onto NormalizedInboundEmail.

    Postmark's documented shape is the most stable of the three providers
    we target — `From`, `To`, `Subject`, `TextBody`, `HtmlBody`,
    `StrippedTextReply`, `Headers` (list of {Name, Value}), and `MessageID`.
    """
    headers_list = payload.get("Headers") or []
    headers_map = {
        (h.get("Name") or "").lower(): (h.get("Value") or "")
        for h in headers_list if isinstance(h, dict)
    }
    in_reply_to = headers_map.get("in-reply-to", "") or headers_map.get("references", "")

    to_addr = payload.get("To") or payload.get("OriginalRecipient") or ""
    token = extract_to_token(to_addr)

    # Prefer Postmark's stripped reply (signature + quoted thread already
    # removed) when present — yields a clean text_body for `preview_snippet`.
    text_body = payload.get("StrippedTextReply") or payload.get("TextBody") or ""

    received_at = payload.get("Date") or datetime.now(timezone.utc).isoformat()

    return NormalizedInboundEmail(
        from_email=payload.get("From") or "",
        from_name=payload.get("FromName") or "",
        to_token=token,
        to_full=to_addr,
        subject=payload.get("Subject") or "",
        text_body=text_body,
        html_body=payload.get("HtmlBody") or "",
        in_reply_to=in_reply_to,
        provider_message_id=payload.get("MessageID") or "",
        received_at=received_at,
        raw_provider_payload=payload,
    )


def normalize_resend_payload(payload: dict[str, Any]) -> NormalizedInboundEmail:
    """Resend inbound webhook stub. Resend's inbound API is still beta;
    fields below match their current schema. Treat as best-effort until
    we light up Resend in production."""
    data = payload.get("data") or payload
    from_field = data.get("from") or ""
    # Resend's `from` is typically "Name <email>"
    fm = re.match(r"\s*(.*?)\s*<([^>]+)>\s*$", from_field)
    from_name = fm.group(1) if fm else ""
    from_email = fm.group(2) if fm else from_field

    to_list = data.get("to") or []
    to_addr = to_list[0] if isinstance(to_list, list) and to_list else ""
    token = extract_to_token(to_addr)

    headers = data.get("headers") or {}
    in_reply_to = ""
    if isinstance(headers, dict):
        in_reply_to = headers.get("in-reply-to") or headers.get("In-Reply-To") or ""

    return NormalizedInboundEmail(
        from_email=from_email,
        from_name=from_name,
        to_token=token,
        to_full=to_addr,
        subject=data.get("subject") or "",
        text_body=data.get("text") or "",
        html_body=data.get("html") or "",
        in_reply_to=in_reply_to,
        provider_message_id=data.get("email_id") or data.get("id") or "",
        received_at=data.get("created_at") or datetime.now(timezone.utc).isoformat(),
        raw_provider_payload=payload,
    )


def normalize_sendgrid_payload(payload: dict[str, Any]) -> NormalizedInboundEmail:
    """SendGrid Inbound Parse stub. SendGrid posts as multipart/form-data
    not JSON — the router is responsible for converting form fields to a
    dict before calling this. Treat as best-effort until we light it up."""
    to_addr = payload.get("to") or ""
    token = extract_to_token(to_addr)

    return NormalizedInboundEmail(
        from_email=payload.get("from") or "",
        from_name="",
        to_token=token,
        to_full=to_addr,
        subject=payload.get("subject") or "",
        text_body=payload.get("text") or "",
        html_body=payload.get("html") or "",
        in_reply_to=payload.get("In-Reply-To") or "",
        provider_message_id=payload.get("message-id") or payload.get("Message-ID") or "",
        received_at=datetime.now(timezone.utc).isoformat(),
        raw_provider_payload=payload,
    )


def normalize_payload(provider: str, payload: dict[str, Any]) -> NormalizedInboundEmail:
    """Dispatch to the correct normalizer based on the configured provider.
    Falls back to Postmark since that's the most fully-implemented shape."""
    p = (provider or "postmark").strip().lower()
    if p == "resend":
        return normalize_resend_payload(payload)
    if p == "sendgrid":
        return normalize_sendgrid_payload(payload)
    return normalize_postmark_payload(payload)


# ── Persistence ──────────────────────────────────────────────────────────


async def process_inbound_message(msg: NormalizedInboundEmail) -> dict[str, Any]:
    """Find the parent thread, insert the inbound message, flip thread
    status, emit live UI updates, fire a notification.

    Always returns a dict — never raises. The webhook caller (Postmark
    et al.) MUST receive 200 even when we can't process the message,
    or we get retried indefinitely.
    """
    token = (msg.get("to_token") or "").strip()
    if not token:
        logger.warning("[inbound] no token in to_address=%r", msg.get("to_full"))
        return {"ok": False, "thread_id": None, "message_id": None,
                "error": "no_token_in_address"}

    sb = get_db()

    # ── 1. Look up the parent thread by id ─────────────────────────────
    thread_row = None
    try:
        result = (
            sb.table("email_threads")
            .select("*")
            .eq("id", token)
            .limit(1)
            .execute()
        )
        if result.data:
            thread_row = result.data[0]
    except Exception as e:
        # Common case: token isn't a valid uuid → Postgres rejects the
        # `id = '<garbage>'` filter. Log and bail with thread_not_found.
        logger.warning("[inbound] thread lookup failed token=%r err=%s", token, e)

    if not thread_row:
        logger.warning("[inbound] thread_not_found token=%r from=%r",
                       token, msg.get("from_email"))
        return {"ok": False, "thread_id": None, "message_id": None,
                "error": "thread_not_found"}

    thread_id = thread_row["id"]
    tenant_id = thread_row["tenant_id"]

    # ── 2. Idempotency — drop duplicate provider message_ids ───────────
    provider_mid = (msg.get("provider_message_id") or "").strip()
    if provider_mid:
        try:
            existing = (
                sb.table("email_messages")
                .select("id")
                .eq("gmail_message_id", provider_mid)
                .limit(1)
                .execute()
            )
            if existing.data:
                logger.info("[inbound] duplicate provider_message_id=%s — skipping",
                            provider_mid)
                return {"ok": True, "thread_id": thread_id,
                        "message_id": existing.data[0]["id"],
                        "error": None, "duplicate": True}
        except Exception as e:
            logger.debug("[inbound] dedup lookup failed (continuing): %s", e)

    # ── 3. Build + insert the email_messages row ────────────────────────
    now_iso = datetime.now(timezone.utc).isoformat()
    received_at = msg.get("received_at") or now_iso

    from_email = msg.get("from_email", "")
    from_name = msg.get("from_name", "")
    sender_str = f"{from_name} <{from_email}>" if from_name and from_email else from_email

    text_body = msg.get("text_body", "") or ""
    html_body = msg.get("html_body", "") or ""
    preview_snippet = (text_body or re.sub(r"<[^>]+>", "", html_body))[:280].strip()

    subject = msg.get("subject") or thread_row.get("subject") or ""

    insert_row: dict[str, Any] = {
        "thread_id": thread_id,
        "tenant_id": tenant_id,
        "gmail_message_id": provider_mid or None,
        "direction": "inbound",
        "sender": sender_str or from_email,
        "recipients": msg.get("to_full") or "",
        "subject": subject,
        "text_body": text_body,
        "html_body": html_body,
        "preview_snippet": preview_snippet,
        "message_timestamp": received_at,
        "approval_status": "none",
    }

    new_message_id: str | None = None
    try:
        ins = sb.table("email_messages").insert(insert_row).execute()
        if ins.data:
            new_message_id = ins.data[0]["id"]
    except Exception as e:
        # If the unique-index on gmail_message_id triggers, treat it as a
        # successful dedupe rather than a hard failure.
        msg_str = str(e).lower()
        if "duplicate" in msg_str or "unique" in msg_str:
            logger.info("[inbound] insert hit unique constraint — treating as duplicate: %s", e)
            return {"ok": True, "thread_id": thread_id, "message_id": None,
                    "error": None, "duplicate": True}
        logger.error("[inbound] insert email_messages failed: %s", e)
        return {"ok": False, "thread_id": thread_id, "message_id": None,
                "error": f"insert_failed: {e}"}

    # ── 4. Flip thread status + bump last_message_at ────────────────────
    try:
        new_status = thread_row.get("status") or "open"
        # Re-open closed threads and bring awaiting_reply / replied back to
        # "open" so the UI surfaces the new inbound for the user.
        if new_status in ("closed", "awaiting_reply", "replied"):
            new_status = "open"
        sb.table("email_threads").update({
            "status": new_status,
            "last_message_at": received_at,
            "updated_at": now_iso,
        }).eq("id", thread_id).execute()
    except Exception as e:
        logger.warning("[inbound] thread status update failed (non-fatal): %s", e)

    # ── 5. Emit Socket.IO updates so live UIs refresh ───────────────────
    try:
        from backend.services.realtime import sio
        await sio.emit("inbox_updated", {
            "action": "new_reply",
            "thread_id": thread_id,
            "message": {
                "id": new_message_id,
                "thread_id": thread_id,
                "tenant_id": tenant_id,
                "direction": "inbound",
                "sender": sender_str,
                "subject": subject,
                "preview_snippet": preview_snippet,
                "message_timestamp": received_at,
            },
            "thread": {
                "id": thread_id,
                "status": "open",
                "last_message_at": received_at,
                "contact_email": thread_row.get("contact_email", ""),
                "subject": thread_row.get("subject", ""),
            },
        }, room=tenant_id)
        await sio.emit("email_thread_updated", {
            "thread_id": thread_id,
            "status": "open",
            "last_message_at": received_at,
        }, room=tenant_id)
    except Exception as e:
        logger.debug("[inbound] socket emit failed (non-fatal): %s", e)

    # ── 6. Bell-ticker notification (best-effort) ───────────────────────
    try:
        from backend.server import _notify
        contact = thread_row.get("contact_email") or from_email or "Unknown"
        snippet_short = (preview_snippet or subject)[:140]
        await _notify(
            tenant_id,
            "email_reply",
            f"New reply from {contact}",
            body=snippet_short,
            href=f"/conversations?id={thread_id}",
            category="conversation",
            priority="normal",
            resource_type="email_thread",
            resource_id=thread_id,
        )
    except Exception as e:
        logger.debug("[inbound] notify failed (non-fatal): %s", e)

    return {"ok": True, "thread_id": thread_id,
            "message_id": new_message_id, "error": None}


# ── HMAC signature helpers (router uses these) ───────────────────────────


def get_inbound_provider() -> str:
    """Provider currently configured for inbound mail. Default postmark."""
    return (os.environ.get("INBOUND_EMAIL_PROVIDER") or "postmark").strip().lower()


def get_webhook_secret() -> str:
    """Shared HMAC secret for inbound webhook validation. Empty string
    means dev-mode (accept all)."""
    return (os.environ.get("INBOUND_WEBHOOK_SECRET") or "").strip()
