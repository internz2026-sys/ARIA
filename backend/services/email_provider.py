"""Email Provider abstraction — single send() seam for outbound mail.

Two channels:
  - SMTP    — Hostinger mail hosting on the project's domain. Default.
              Configured via SMTP_HOST / SMTP_PORT / SMTP_USER /
              SMTP_PASSWORD / SMTP_FROM_EMAIL / SMTP_FROM_NAME env vars.
              Synchronous smtplib wrapped in asyncio.to_thread so we
              don't block the event loop.
  - Gmail   — Per-user OAuth (gmail.send scope). Used when the tenant
              has connected their own Google account and wants outbound
              sent from their personal Gmail address.

Resend was removed 2026-05-06 per project decision (we already have
mail hosting included with Hostinger; SMTP via that mailbox is free,
sufficient for the volumes ARIA expects, and avoids another vendor).

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

If SMTP credentials are missing, send_email returns provider="none"
with success=False so callers can surface "configure email" instead
of silently swallowing the send.
"""
from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from typing import Optional, TypedDict

logger = logging.getLogger("aria.services.email_provider")


class EmailSendResult(TypedDict):
    """Common return shape across providers.

    `provider` is "smtp" | "gmail" | "none" — callers can branch on
    "none" to surface "configure email to enable sending" to the user
    instead of a hard failure.
    """
    success: bool
    message_id: Optional[str]
    thread_id: Optional[str]   # only Gmail returns this (threadId from Gmail API)
    provider: str
    error: Optional[str]


# ── Helpers ────────────────────────────────────────────────────────────


def _provider_choice() -> str:
    """Read EMAIL_PROVIDER env var and normalize. Default is "smtp"."""
    raw = (os.environ.get("EMAIL_PROVIDER") or "smtp").strip().lower()
    if raw not in {"smtp", "gmail", "auto"}:
        logger.warning(
            "EMAIL_PROVIDER=%r is not one of smtp/gmail/auto; defaulting to smtp",
            raw,
        )
        return "smtp"
    return raw


def _inbound_domain() -> str:
    return (os.environ.get("INBOUND_EMAIL_DOMAIN") or "").strip()


def _smtp_config() -> dict:
    """Return SMTP credentials + sender info from env. Empty strings on
    missing keys so callers can short-circuit with provider='none'."""
    use_ssl_raw = (os.environ.get("SMTP_USE_SSL") or "true").strip().lower()
    return {
        "host": (os.environ.get("SMTP_HOST") or "").strip(),
        "port": int((os.environ.get("SMTP_PORT") or "465").strip() or "465"),
        "use_ssl": use_ssl_raw in ("1", "true", "yes"),
        "user": (os.environ.get("SMTP_USER") or "").strip(),
        "password": os.environ.get("SMTP_PASSWORD") or "",
        "from_email": (os.environ.get("SMTP_FROM_EMAIL") or os.environ.get("SMTP_USER") or "").strip(),
        "from_name": (os.environ.get("SMTP_FROM_NAME") or "ARIA").strip(),
    }


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


# ── SMTP send (Hostinger or any standard SMTP server) ─────────────────


def _build_mime_message(
    *,
    from_header: str,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    in_reply_to: str,
    references: str,
    reply_to: str,
    sender_email: str,
) -> tuple[MIMEMultipart, str]:
    """Build a multipart/alternative MIME message ready for SMTP send.

    Returns (msg, message_id). The message_id is generated locally so
    we can return it to the caller for thread tracking — Hostinger's
    SMTP doesn't echo back its own ID the way an HTTP API would.
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = from_header
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    elif references:
        msg["References"] = references

    # RFC-compliant Message-ID — domain part should match the sender's
    # domain so spam filters don't downrank.
    sender_domain = sender_email.split("@", 1)[-1] if "@" in sender_email else "aria.local"
    message_id = make_msgid(domain=sender_domain)
    msg["Message-ID"] = message_id

    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    return msg, message_id


def _smtp_send_sync(
    *,
    cfg: dict,
    msg: MIMEMultipart,
    to: str,
) -> Optional[str]:
    """Synchronous smtplib send. Called via asyncio.to_thread so the
    blocking I/O doesn't stall the event loop. Raises on failure;
    caller wraps in try/except to translate to EmailSendResult."""
    context = ssl.create_default_context()
    if cfg["use_ssl"]:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=context, timeout=30) as server:
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg, from_addr=cfg["from_email"], to_addrs=[to])
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
            server.starttls(context=context)
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg, from_addr=cfg["from_email"], to_addrs=[to])
    return None


async def _send_via_smtp(
    *,
    display_name: str,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    in_reply_to: str,
    references: str,
    reply_to: str,
) -> EmailSendResult:
    """Send via the configured SMTP server. Never raises."""
    cfg = _smtp_config()

    # Hard-fail (not silent-noop) when SMTP isn't configured. The
    # previous Resend path silent-noop'd which masked broken setups
    # for hours/days; on SMTP we want a clear 'configure your email'
    # signal so the operator notices.
    missing = [k for k in ("host", "user", "password", "from_email") if not cfg[k]]
    if missing:
        logger.warning(
            "[email_provider] SMTP not configured (missing: %s); send to %s skipped",
            ",".join(missing), to,
        )
        return {
            "success": False,
            "message_id": None,
            "thread_id": None,
            "provider": "none",
            "error": f"SMTP not configured (missing env: {', '.join('SMTP_' + m.upper() for m in missing)})",
        }

    # From header always uses the SMTP-authenticated mailbox as the
    # actual sender. Hostinger (and most SMTP servers) reject sends
    # where the From mismatches the auth identity. Display name is
    # tenant-customizable; the email address is fixed to the mailbox
    # we authenticate as.
    from_header = formataddr((display_name or cfg["from_name"], cfg["from_email"]))

    msg, message_id = _build_mime_message(
        from_header=from_header,
        to=to,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        in_reply_to=in_reply_to,
        references=references,
        reply_to=reply_to,
        sender_email=cfg["from_email"],
    )

    try:
        await asyncio.to_thread(_smtp_send_sync, cfg=cfg, msg=msg, to=to)
    except smtplib.SMTPAuthenticationError as e:
        logger.warning("[email_provider] SMTP auth failed: %s", e)
        return {
            "success": False, "message_id": None, "thread_id": None,
            "provider": "smtp",
            "error": "SMTP authentication failed — check SMTP_USER and SMTP_PASSWORD",
        }
    except smtplib.SMTPRecipientsRefused as e:
        logger.warning("[email_provider] SMTP recipient refused: %s", e)
        return {
            "success": False, "message_id": None, "thread_id": None,
            "provider": "smtp",
            "error": f"Recipient refused: {to}",
        }
    except smtplib.SMTPException as e:
        logger.warning("[email_provider] SMTP error: %s", e)
        return {
            "success": False, "message_id": None, "thread_id": None,
            "provider": "smtp",
            "error": f"smtp_error: {type(e).__name__}: {e}",
        }
    except (TimeoutError, ConnectionError, OSError) as e:
        logger.warning("[email_provider] SMTP connection failed: %s", e)
        return {
            "success": False, "message_id": None, "thread_id": None,
            "provider": "smtp",
            "error": f"smtp_connection_error: {type(e).__name__}: {e}",
        }
    except Exception as e:
        logger.exception("[email_provider] unexpected SMTP send failure: %s", e)
        return {
            "success": False, "message_id": None, "thread_id": None,
            "provider": "smtp",
            "error": f"smtp_unexpected: {type(e).__name__}: {e}",
        }

    return {
        "success": True,
        "message_id": message_id,
        "thread_id": None,            # SMTP has no threadId concept
        "provider": "smtp",
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
      2. EMAIL_PROVIDER env var               (default "smtp")
      3. "auto"                                (uses Gmail if tokens exist, else SMTP)

    `inbound_thread_id` and `inbox_item_id` feed the Reply-To token
    builder so customer replies route back through the inbound webhook
    and re-enter the right thread. When neither is supplied, we fall
    back to a tenant-level token so the inbound parser can still find
    the right tenant.
    """
    from backend.config.loader import get_tenant_config

    cfg = get_tenant_config(tenant_id)

    # Pick provider. The legacy 'resend' value lingers in some tenant
    # configs from before the SMTP migration — treat it as a transparent
    # alias for the new default (SMTP) so existing tenants keep sending
    # without a manual config flip.
    per_tenant = (cfg.integrations.email_provider or "").strip().lower()
    if per_tenant == "resend":
        per_tenant = "smtp"
    provider = per_tenant or _provider_choice()

    if provider == "auto":
        # Smart fallback: prefer Gmail when the tenant has tokens, else SMTP
        if cfg.integrations.google_access_token or cfg.integrations.google_refresh_token:
            provider = "gmail"
        else:
            provider = "smtp"

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

    # ── SMTP path ──
    display_name = (from_display_name or "").strip() or (
        cfg.integrations.email_sender_display_name or cfg.business_name or "ARIA"
    )

    reply_to = build_reply_to(
        inbound_thread_id=inbound_thread_id,
        tenant_id=str(cfg.tenant_id) if not inbound_thread_id else "",
        inbox_item_id=inbox_item_id if not inbound_thread_id else "",
    )

    # Plain-text companion: callers usually pass HTML only. Stripping
    # tags here keeps every send multipart so spam filters don't
    # downrank us for HTML-only mail.
    if not text_body and html_body:
        import re as _re
        text_body = _re.sub(r"<[^>]+>", "", html_body).strip()

    return await _send_via_smtp(
        display_name=display_name,
        to=to,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        in_reply_to=in_reply_to,
        references=references,
        reply_to=reply_to,
    )
