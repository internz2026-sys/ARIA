"""Gmail Inbound Reply Sync — polls for replies to ARIA-managed email threads.

MVP: Polling-based. For each tenant with Gmail connected, checks for new
inbound messages in known threads. Designed so Gmail push/watch can replace
the polling layer later without touching the import logic.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from backend.config.loader import get_active_tenants, get_tenant_config, save_tenant_config
from backend.tools import gmail_tool

logger = logging.getLogger("aria.gmail_sync")


def _get_supabase():
    from backend.config.loader import _get_supabase
    return _get_supabase()


def _extract_email_address(raw: str) -> str:
    """Extract bare email from 'Name <email@example.com>' format."""
    m = re.search(r'<([^>]+)>', raw)
    if m:
        return m.group(1).lower()
    return raw.strip().lower()


async def _ensure_access_token(config) -> str | None:
    """Get a valid access token, refreshing if needed. Returns None if disconnected."""
    token = config.integrations.google_access_token
    if not token:
        return None

    # Test token by fetching profile
    profile = await gmail_tool.get_profile(token)
    if profile.get("error") == "token_expired":
        refresh = config.integrations.google_refresh_token
        if not refresh:
            logger.warning("Gmail token expired and no refresh token for tenant %s", config.tenant_id)
            config.integrations.google_access_token = None
            save_tenant_config(config)
            return None
        try:
            token = await gmail_tool.refresh_access_token(refresh)
            config.integrations.google_access_token = token
            save_tenant_config(config)
        except Exception as e:
            logger.warning("Gmail token refresh failed for tenant %s: %s", config.tenant_id, e)
            config.integrations.google_access_token = None
            # Only clear refresh_token if Google explicitly revoked it
            if getattr(e, "is_revoked", False):
                config.integrations.google_refresh_token = None
                logger.warning("Google revoked refresh token for tenant %s — user must reconnect", config.tenant_id)
            save_tenant_config(config)
            return None
    elif profile.get("error"):
        logger.warning("Gmail API error for tenant %s: %s", config.tenant_id, profile["error"])
        return None

    return token


def _get_known_thread_ids(tenant_id: str) -> list[str]:
    """Get all gmail_thread_ids we're tracking for a tenant."""
    sb = _get_supabase()
    result = sb.table("email_threads").select("gmail_thread_id").eq(
        "tenant_id", str(tenant_id)
    ).not_.is_("gmail_thread_id", "null").execute()
    return [r["gmail_thread_id"] for r in (result.data or []) if r.get("gmail_thread_id")]


def _get_known_message_ids(tenant_id: str) -> set[str]:
    """Get all gmail_message_ids already imported for a tenant (for dedup)."""
    sb = _get_supabase()
    result = sb.table("email_messages").select("gmail_message_id").eq(
        "tenant_id", str(tenant_id)
    ).not_.is_("gmail_message_id", "null").execute()
    return {r["gmail_message_id"] for r in (result.data or []) if r.get("gmail_message_id")}


def _find_thread_by_gmail_id(tenant_id: str, gmail_thread_id: str) -> dict | None:
    """Look up an email_threads row by gmail_thread_id."""
    sb = _get_supabase()
    result = sb.table("email_threads").select("*").eq(
        "tenant_id", str(tenant_id)
    ).eq("gmail_thread_id", gmail_thread_id).limit(1).execute()
    return result.data[0] if result.data else None


def _save_inbound_message(
    thread_id: str,
    tenant_id: str,
    msg: dict,
) -> dict | None:
    """Save a single inbound email message to the email_messages table."""
    sb = _get_supabase()
    # Build preview snippet
    snippet = msg.get("preview_snippet", "")
    if not snippet and msg.get("text_body"):
        snippet = msg["text_body"][:200]
    elif not snippet and msg.get("html_body"):
        snippet = gmail_tool._strip_html(msg["html_body"])[:200]

    # Parse internal_date (milliseconds since epoch) to timestamp
    internal_date = msg.get("internal_date", "")
    if internal_date:
        try:
            ts = datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc).isoformat()
        except (ValueError, TypeError):
            ts = datetime.now(timezone.utc).isoformat()
    else:
        ts = datetime.now(timezone.utc).isoformat()

    row = {
        "thread_id": thread_id,
        "tenant_id": str(tenant_id),
        "gmail_message_id": msg.get("gmail_message_id"),
        "direction": "inbound",
        "sender": msg.get("from", ""),
        "recipients": msg.get("to", ""),
        "subject": msg.get("subject", ""),
        "text_body": msg.get("text_body", ""),
        "html_body": msg.get("html_body", ""),
        "preview_snippet": snippet,
        "message_timestamp": ts,
        "approval_status": "none",
    }
    try:
        result = sb.table("email_messages").insert(row).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        # Likely duplicate (unique constraint on gmail_message_id)
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            logger.debug("Skipping duplicate message %s", msg.get("gmail_message_id"))
            return None
        logger.error("Failed to save inbound message: %s", e)
        return None


def _update_thread_status(thread_id: str, status: str = "needs_review"):
    """Update thread status and last_message_at."""
    sb = _get_supabase()
    sb.table("email_threads").update({
        "status": status,
        "last_message_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", thread_id).execute()


def _create_inbox_item_for_reply(
    tenant_id: str,
    thread: dict,
    msg: dict,
) -> dict | None:
    """Create an inbox item for a new inbound reply so it appears in the user's inbox."""
    sb = _get_supabase()
    sender = _extract_email_address(msg.get("from", ""))
    subject = msg.get("subject", "Reply")
    snippet = msg.get("preview_snippet", "")[:200]

    row = {
        "tenant_id": str(tenant_id),
        "agent": "email_marketer",
        "type": "email_reply",
        "title": f"Reply from {sender}: {subject}",
        "content": snippet,
        "status": "needs_review",
        "priority": "high",
    }
    try:
        result = sb.table("inbox_items").insert(row).execute()
        saved = result.data[0] if result.data else None
        if saved:
            logger.info("Created inbox item for reply from %s (id=%s)", sender, saved.get("id"))
        return saved
    except Exception as e:
        logger.error("Failed to create inbox item for reply: %s", e)
        return None


async def sync_tenant_replies(tenant_id: str) -> dict:
    """Sync inbound replies for a single tenant.

    Returns {"imported": int, "threads_checked": int, "error": str | None}.
    """
    config = get_tenant_config(tenant_id)
    token = await _ensure_access_token(config)
    if not token:
        return {"imported": 0, "threads_checked": 0, "error": "gmail_not_connected"}

    # Get all thread IDs we're tracking
    thread_ids = _get_known_thread_ids(tenant_id)
    if not thread_ids:
        return {"imported": 0, "threads_checked": 0, "error": None}

    known_msg_ids = _get_known_message_ids(tenant_id)
    imported = 0
    new_replies: list[dict] = []  # Collect for real-time notifications

    for gmail_tid in thread_ids:
        try:
            thread_data = await gmail_tool.get_thread(token, gmail_tid)
            if thread_data.get("error"):
                if thread_data["error"] == "token_expired":
                    return {"imported": imported, "threads_checked": 0, "error": "token_expired", "new_replies": new_replies}
                logger.warning("Failed to fetch thread %s: %s", gmail_tid, thread_data["error"])
                continue

            # Find our DB thread record
            db_thread = _find_thread_by_gmail_id(tenant_id, gmail_tid)
            if not db_thread:
                continue

            owner_email = config.owner_email.lower()

            for msg in thread_data.get("messages", []):
                msg_id = msg.get("gmail_message_id", "")
                if not msg_id or msg_id in known_msg_ids:
                    continue

                # Determine direction: if sender matches tenant owner, it's outbound
                sender_email = _extract_email_address(msg.get("from", ""))
                if sender_email == owner_email:
                    # Outbound message we didn't track — skip for now
                    # (we save outbound via approve-send flow)
                    continue

                # Inbound reply
                saved = _save_inbound_message(
                    thread_id=db_thread["id"],
                    tenant_id=tenant_id,
                    msg=msg,
                )
                if saved:
                    imported += 1
                    known_msg_ids.add(msg_id)
                    _update_thread_status(db_thread["id"], "needs_review")
                    inbox_item = _create_inbox_item_for_reply(tenant_id, db_thread, msg)
                    new_replies.append({
                        "thread_id": db_thread["id"],
                        "sender": sender_email,
                        "subject": msg.get("subject", ""),
                        "snippet": msg.get("preview_snippet", "")[:200],
                        "inbox_item": inbox_item,
                    })
                    logger.info(
                        "Imported inbound reply from %s in thread %s",
                        sender_email, gmail_tid,
                    )

        except Exception as e:
            logger.error("Error syncing thread %s for tenant %s: %s", gmail_tid, tenant_id, e)

    return {"imported": imported, "threads_checked": len(thread_ids), "error": None, "new_replies": new_replies}


async def sync_all_tenants() -> list[dict]:
    """Run sync for all active tenants with Gmail connected. Called by cron."""
    tenants = get_active_tenants()
    results = []

    for tenant in tenants:
        if not tenant.integrations.google_access_token and not tenant.integrations.google_refresh_token:
            continue
        try:
            result = await sync_tenant_replies(str(tenant.tenant_id))
            result["tenant_id"] = str(tenant.tenant_id)
            results.append(result)
            if result.get("imported", 0) > 0:
                logger.info(
                    "Tenant %s: imported %d inbound replies from %d threads",
                    tenant.tenant_id, result["imported"], result["threads_checked"],
                )
        except Exception as e:
            logger.error("Sync failed for tenant %s: %s", tenant.tenant_id, e)
            results.append({"tenant_id": str(tenant.tenant_id), "error": str(e)})

    return results
