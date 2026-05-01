"""Email Provider abstraction — single send() seam for outbound mail.

Why this exists:
    The old path was direct `gmail_tool.send_email(...)` calls scattered
    through email_marketer_agent, email_sender, scheduler, etc. That
    locked us into Gmail OAuth as the sole sending channel — which is
    a 2-6 week wait on Google verification + requires a real domain +
    breaks against school-account workspace policies.

    This module replaces those direct calls with a provider-agnostic
    `send_email(...)` that picks the channel based on:
      1. The EMAIL_PROVIDER env var ("resend" | "gmail" | "auto").
      2. Whether the tenant has Gmail OAuth tokens (only relevant for
         "auto" mode — smart fallback during the migration period).

    The default is Resend (HTTP API, sends immediately once a domain is
    verified). Gmail OAuth still works when EMAIL_PROVIDER=gmail or the
    tenant explicitly opts into it.

Reply-to convention (parsed by the inbound webhook in backend/services/
email_inbound.py):

    Format: replies+<TOKEN>@inbound.<INBOUND_EMAIL_DOMAIN>

    TOKEN priority — pick the first that's available:
        1. <thread_id>          (UUID of email_threads row)
        2. t.<tenant_id>        (UUID of tenant, prefixed "t.")
        3. i.<inbox_item_id>    (UUID of inbox_items row, prefixed "i.")

    The thread_id form is preferred because email_threads carries
    tenant_id + contact_email + gmail_thread_id already. The
    "t." / "i." prefixes are sentinels so the inbound parser can
    disambiguate which lookup to perform without a schema-aware regex.

If RESEND_API_KEY or INBOUND_EMAIL_DOMAIN is unset, send_email logs a
warning and returns a stub success result with provider="none". Callers
treat that as "queued until domain configured" — same UX the frontend's
Settings → Email tab already plans to render via the /api/settings/
email/status endpoint.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, TypedDict

import httpx

logger = logging.getLogger("aria.services.email_provider")

RESEND_API_URL = "https://api.resend.com/emails"


class EmailSendResult(TypedDict):
    """Common return shape across providers.

    `provider` is "resend" | "gmail" | "none" — callers can branch on
    "none" to surface "queued, configure domain to enable sending" to
    the user instead of a hard failure.
    """
    success: bool
    message_id: Optional[str]
    thread_id: Optional[str]   # only Gmail returns this (threadId from Gmail API)
    provider: str
    error: Optional[str]


# ── Helpers ────────────────────────────────────────────────────────────


def _provider_choice() -> str:
    """Read EMAIL_PROVIDER env var and normalize. Default is "resend"."""
    raw = (os.environ.get("EMAIL_PROVIDER") or "resend").strip().lower()
    if raw not in {"resend", "gmail", "auto"}:
        logger.warning(
            "EMAIL_PROVIDER=%r is not one of resend/gmail/auto; defaulting to resend",
            raw,
        )
        return "resend"
    return raw


def _inbound_domain() -> str:
    return (os.environ.get("INBOUND_EMAIL_DOMAIN") or "").strip()


def _resend_key() -> str:
    return (os.environ.get("RESEND_API_KEY") or "").strip()


def _strip_subdomain_prefix(domain: str, prefix: str) -> str:
    """If domain already starts with `prefix.`, strip it so callers can
    safely re-prefix without doubling up.

    This is what lets the same INBOUND_EMAIL_DOMAIN env var work whether
    the operator set it to the apex (`tryaria.com`) or to the inbound
    subdomain (`inbound.tryaria.com`, the historical value used by
    gmail_tool). We always normalize to the apex internally, then
    prepend "send." or "inbound." as the role demands.
    """
    if domain.startswith(prefix + "."):
        return domain[len(prefix) + 1:]
    return domain


def _apex_domain() -> str:
    """Return the apex (no leading 'send.' / 'inbound.') domain.

    Accepts either form of INBOUND_EMAIL_DOMAIN — a fresh deploy can
    set it to `tryaria.com` and an older deploy that had it as
    `inbound.tryaria.com` keeps working.
    """
    raw = _inbound_domain()
    if not raw:
        return ""
    raw = _strip_subdomain_prefix(raw, "inbound")
    raw = _strip_subdomain_prefix(raw, "send")
    return raw


def build_reply_to(
    *,
    inbound_thread_id: str = "",
    tenant_id: str = "",
    inbox_item_id: str = "",
) -> str:
    """Build the Reply-To address for a given send.

    Format:
        replies+<TOKEN>@inbound.<APEX_DOMAIN>

    Returns "" when INBOUND_EMAIL_DOMAIN is unset OR no token can be
    resolved — caller should then omit the Reply-To header so replies
    fall back to the From address (Resend handles its own bounce
    routing for that case).

    Token priority: thread_id > tenant_id > inbox_item_id. See module
    docstring for the lookup convention the inbound parser uses.
    """
    apex = _apex_domain()
    if not apex:
        return ""
    if inbound_thread_id:
        token = inbound_thread_id
    elif tenant_id:
        token = f"t.{tenant_id}"
    elif inbox_item_id:
        token = f"i.{inbox_item_id}"
    else:
        return ""
    return f"replies+{token}@inbound.{apex}"


def build_sender_address(local: str) -> str:
    """Compose the full ARIA-managed sender address.

    Format:
        <local>@send.<APEX_DOMAIN>

    Returns "" when either piece is missing — caller surfaces that as
    "not configured".

    The dedicated "send." subdomain lets DKIM/SPF for outbound be
    scoped separately from the "inbound." MX record.
    """
    apex = _apex_domain()
    local = (local or "").strip()
    if not apex or not local:
        return ""
    return f"{local}@send.{apex}"


def build_from_header(display_name: str, sender_address: str) -> str:
    """RFC 5322 From line: '"Display Name" <local@send.domain>'.

    Display name is wrapped in quotes only when it contains characters
    that would otherwise need escaping. Plain ASCII names render fine
    bare, but commas / semicolons / quotes break parsers if not quoted.
    """
    addr = (sender_address or "").strip()
    name = (display_name or "").strip()
    if not addr:
        return ""
    if not name:
        return addr
    # Quote always — safest path; RFC 5322 allows it unconditionally.
    safe_name = name.replace('"', '').replace('\\', '')
    return f'"{safe_name}" <{addr}>'


# ── Tenant config bridge ───────────────────────────────────────────────


def get_tenant_email_settings(tenant_id: str) -> dict:
    """Return the resolved email settings for a tenant.

    Pulls from `tenant.integrations.email_*` fields, falling back to
    sensible defaults when the user hasn't filled them in yet. Returns
    a plain dict so callers don't depend on the Pydantic model.
    """
    from backend.config.loader import get_tenant_config

    cfg = get_tenant_config(tenant_id)
    integrations = cfg.integrations
    display_name = (integrations.email_sender_display_name or cfg.business_name or "ARIA").strip()
    local = (integrations.email_sender_local or "").strip()
    provider = (integrations.email_provider or "").strip().lower() or _provider_choice()
    return {
        "display_name": display_name,
        "sender_local": local,
        "provider": provider,
        "domain": _inbound_domain(),
    }


# ── Resend HTTP send ───────────────────────────────────────────────────


async def _send_via_resend(
    *,
    from_header: str,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    in_reply_to: str,
    references: str,
    reply_to: str,
) -> EmailSendResult:
    """POST to Resend's /emails endpoint. Never raises."""
    api_key = _resend_key()
    if not api_key:
        logger.warning(
            "[email_provider] RESEND_API_KEY unset — send to %s noop'd; "
            "configure the env var to enable actual delivery.", to,
        )
        return {
            "success": True,            # not a hard error — surface to UI as "queued"
            "message_id": None,
            "thread_id": None,
            "provider": "none",
            "error": "RESEND_API_KEY not configured",
        }
    if not from_header:
        return {
            "success": False,
            "message_id": None,
            "thread_id": None,
            "provider": "resend",
            "error": "Sender address not configured (set INBOUND_EMAIL_DOMAIN + tenant email_sender_local)",
        }

    headers: dict[str, str] = {}
    if reply_to:
        headers["Reply-To"] = reply_to
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
        headers["References"] = references or in_reply_to
    elif references:
        headers["References"] = references

    payload: dict = {
        "from": from_header,
        "to": [to] if isinstance(to, str) else list(to),
        "subject": subject,
        "html": html_body,
    }
    if text_body:
        payload["text"] = text_body
    if headers:
        payload["headers"] = headers

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                RESEND_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    except Exception as e:
        logger.warning("[email_provider] Resend HTTP error: %s", e)
        return {
            "success": False,
            "message_id": None,
            "thread_id": None,
            "provider": "resend",
            "error": f"resend_http_error: {e}",
        }

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message") or resp.text[:200]
        except Exception:
            detail = resp.text[:200]
        logger.warning(
            "[email_provider] Resend send failed (%s): %s", resp.status_code, detail,
        )
        return {
            "success": False,
            "message_id": None,
            "thread_id": None,
            "provider": "resend",
            "error": f"resend_api_error ({resp.status_code}): {detail}",
        }

    try:
        data = resp.json()
    except Exception:
        data = {}
    return {
        "success": True,
        "message_id": data.get("id") or None,
        "thread_id": None,            # Resend has no threadId concept
        "provider": "resend",
        "error": None,
    }


# ── Gmail send (delegates to existing tool) ────────────────────────────


async def _send_via_gmail(
    tenant_id: str,
    *,
    to: str,
    subject: str,
    html_body: str,
    in_reply_to: str,
    references: str,
    reply_to_thread_id: str = "",
) -> EmailSendResult:
    """Send through the existing send_with_refresh helper.

    This intentionally calls into backend.services.email_sender so the
    Gmail token refresh dance (which now also includes the inbound
    Reply-To injection inside _build_mime_message) stays in one place.
    """
    from backend.services.email_sender import send_with_refresh

    try:
        result = await send_with_refresh(
            tenant_id,
            to=to,
            subject=subject,
            html_body=html_body,
            thread_id=reply_to_thread_id,
            in_reply_to=in_reply_to,
        )
    except Exception as e:
        # send_with_refresh raises HTTPException for "user must reconnect
        # Gmail" — translate to a non-raising provider failure here so
        # callers can decide whether to escalate or fall back.
        logger.warning("[email_provider] Gmail send raised: %s", e)
        return {
            "success": False,
            "message_id": None,
            "thread_id": None,
            "provider": "gmail",
            "error": str(e),
        }

    if result.get("error"):
        return {
            "success": False,
            "message_id": result.get("message_id") or None,
            "thread_id": result.get("thread_id") or None,
            "provider": "gmail",
            "error": result.get("error") or result.get("detail") or "gmail_error",
        }

    return {
        "success": True,
        "message_id": result.get("message_id") or None,
        "thread_id": result.get("thread_id") or None,
        "provider": "gmail",
        "error": None,
    }


# ── Public API ─────────────────────────────────────────────────────────


async def send_email(
    tenant_id: str,
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str = "",
    in_reply_to: str = "",
    references: str = "",
    inbound_thread_id: str = "",
    inbox_item_id: str = "",
    from_display_name: Optional[str] = None,
    reply_to_gmail_thread_id: str = "",   # only used when routing via Gmail
) -> EmailSendResult:
    """Send an email through the configured provider.

    Resolution order:
      1. tenant.integrations.email_provider  (explicit per-tenant override)
      2. EMAIL_PROVIDER env var               (default "resend")
      3. "auto"                                (uses Gmail if tokens exist)

    `inbound_thread_id` and `inbox_item_id` feed the Reply-To token
    builder so customer replies route back through the inbound webhook
    and re-enter the right thread. When neither is supplied, we fall
    back to a tenant-level token so the inbound parser can still find
    the right tenant.
    """
    from backend.config.loader import get_tenant_config

    cfg = get_tenant_config(tenant_id)

    # Pick provider
    per_tenant = (cfg.integrations.email_provider or "").strip().lower()
    provider = per_tenant or _provider_choice()

    if provider == "auto":
        # Smart fallback: prefer Gmail when the tenant has tokens, else Resend
        if cfg.integrations.google_access_token or cfg.integrations.google_refresh_token:
            provider = "gmail"
        else:
            provider = "resend"

    if provider == "gmail":
        return await _send_via_gmail(
            tenant_id,
            to=to,
            subject=subject,
            html_body=html_body,
            in_reply_to=in_reply_to,
            references=references,
            reply_to_thread_id=reply_to_gmail_thread_id,
        )

    # ── Resend path ──
    display_name = (from_display_name or "").strip() or (
        cfg.integrations.email_sender_display_name or cfg.business_name or "ARIA"
    )
    sender_addr = build_sender_address(cfg.integrations.email_sender_local)
    from_header = build_from_header(display_name, sender_addr)

    reply_to = build_reply_to(
        inbound_thread_id=inbound_thread_id,
        tenant_id=str(cfg.tenant_id) if not inbound_thread_id else "",
        inbox_item_id=inbox_item_id if not inbound_thread_id else "",
    )

    # Plain-text companion: callers usually pass HTML only. Stripping
    # tags here keeps every Resend send multipart so spam filters don't
    # downrank us for HTML-only mail.
    if not text_body and html_body:
        import re as _re
        text_body = _re.sub(r"<[^>]+>", "", html_body).strip()

    return await _send_via_resend(
        from_header=from_header,
        to=to,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        in_reply_to=in_reply_to,
        references=references,
        reply_to=reply_to,
    )
