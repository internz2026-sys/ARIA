"""Email Sender — shared Gmail send primitives used by every email endpoint.

All three Gmail-send surfaces (general send, approve-and-send-draft,
send-reply-on-thread) previously open-coded the same access-token
refresh dance plus reply-thread context lookup plus plain-text HTML
wrapping. Consolidating here means:

- One place to reason about Google OAuth token expiry / refresh races.
- One place to evolve MIME wrapping or threading headers.
- Endpoints become thin adapters that parse the request, call into
  here, and surface the result — no more 80-line copy-pasted blocks.

These helpers are pure in the sense that they don't touch the
FastAPI request/response lifecycle beyond raising HTTPException for
the "user needs to reconnect Gmail" case (which really is an HTTP
concern). Everything else is returned in plain dicts.
"""
from __future__ import annotations

import html as _html_mod
import logging

from fastapi import HTTPException

from backend.config.loader import get_tenant_config, save_tenant_config
from backend.services.supabase import get_db

logger = logging.getLogger("aria.services.email_sender")


async def send_with_refresh(
    tenant_id: str,
    *,
    to: str,
    subject: str,
    html_body: str,
    thread_id: str = "",
    in_reply_to: str = "",
) -> dict:
    """Send an email via Gmail with automatic access-token refresh.

    Returns the raw `gmail_tool.send_email` result dict. Non-auth send
    errors (5xx, bad request, etc.) are returned verbatim so the caller
    can surface them appropriately. Raises HTTPException only when the
    Gmail connection is unrecoverably broken (no tokens, refresh
    explicitly denied) — that signals "ask the user to reconnect"
    rather than "retry later".
    """
    from backend.tools import gmail_tool

    config = get_tenant_config(tenant_id)
    access_token = config.integrations.google_access_token
    refresh_token = config.integrations.google_refresh_token

    # Proactively refresh if we have a refresh token but no access token.
    if not access_token and refresh_token:
        try:
            access_token = await gmail_tool.refresh_access_token(refresh_token)
            config.integrations.google_access_token = access_token
            save_tenant_config(config)
        except Exception:
            pass

    if not access_token:
        raise HTTPException(
            status_code=400,
            detail="Gmail not connected. Please log in with Google to grant email access.",
        )

    async def _do_send(tok: str) -> dict:
        return await gmail_tool.send_email(
            access_token=tok,
            to=to,
            subject=subject,
            html_body=html_body,
            from_email=config.owner_email,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
        )

    result = await _do_send(access_token)

    # Reactive refresh on a 401 from the send itself.
    if result.get("error") == "token_expired" and refresh_token:
        try:
            new_token = await gmail_tool.refresh_access_token(refresh_token)
            config.integrations.google_access_token = new_token
            save_tenant_config(config)
            result = await _do_send(new_token)
        except Exception as e:
            config.integrations.google_access_token = None
            if getattr(e, "is_revoked", False):
                config.integrations.google_refresh_token = None
            save_tenant_config(config)
            raise HTTPException(
                status_code=401,
                detail="Gmail token expired. Please reconnect Gmail in Settings.",
            )

    return result


async def resolve_reply_thread_context(
    tenant_id: str,
    thread_db_id: str,
    access_token: str = "",
) -> tuple[str, str]:
    """Resolve (gmail_thread_id, in_reply_to_header) for a reply target.

    Gmail threads via `threadId`; other mail clients thread via the
    In-Reply-To header. Looking the header up requires one extra Gmail
    API call per send, so we keep it best-effort — any failure just
    returns an empty string and the reply still sends (it just appears
    as a fresh message to non-Gmail recipients).
    """
    sb = get_db()
    gmail_thread_id = ""
    in_reply_to = ""

    try:
        t_row = sb.table("email_threads").select("gmail_thread_id").eq(
            "id", thread_db_id
        ).eq("tenant_id", tenant_id).single().execute()
        if t_row.data:
            gmail_thread_id = t_row.data.get("gmail_thread_id") or ""
    except Exception:
        return "", ""

    if not gmail_thread_id or not access_token:
        return gmail_thread_id, ""

    try:
        last_inbound = sb.table("email_messages").select("gmail_message_id").eq(
            "thread_id", thread_db_id
        ).eq("tenant_id", tenant_id).eq("direction", "inbound").order(
            "message_timestamp", desc=True
        ).limit(1).execute()
        if last_inbound.data:
            gmsg_id = last_inbound.data[0].get("gmail_message_id")
            if gmsg_id:
                from backend.tools import gmail_tool as _gt
                fetched = await _gt.get_message(access_token, gmsg_id)
                in_reply_to = fetched.get("message_id_header", "") or ""
    except Exception:
        pass

    return gmail_thread_id, in_reply_to


def user_text_to_html(text: str) -> str:
    """Convert plain-text user input into lightly styled HTML paragraphs.

    Blank lines separate paragraphs; single line breaks become <br>.
    Input is escaped before any markup is added — trust nothing.
    """
    escaped = _html_mod.escape(text or "")
    paragraphs = [p.replace("\n", "<br>") for p in escaped.split("\n\n") if p.strip()]
    if not paragraphs:
        return f'<p style="margin:0; line-height:1.6;">{escaped}</p>'
    return "".join(
        f'<p style="margin:0 0 12px 0; line-height:1.6;">{p}</p>' for p in paragraphs
    )
