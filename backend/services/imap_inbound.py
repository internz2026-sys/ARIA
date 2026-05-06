"""IMAP inbound — pull replies from the SMTP mailbox into ARIA threads.

The original inbound design (see `email_inbound.py`) assumes a webhook
provider (Postmark / Resend / SendGrid) parses inbound mail and POSTs
the result to `/api/email/inbound`. That requires an MX record on
`inbound.<domain>` plus a paid mail-handling vendor.

For ARIA's v1 path we send via Hostinger SMTP from `aria@hoversight.agency`
and customers reply directly to that address. No MX juggling, no third-
party provider — but we have to come and FETCH the replies via IMAP.

Flow:
  1. Background loop calls `poll_once()` every IMAP_POLL_INTERVAL seconds.
  2. We log into the SMTP user's IMAP server, search for UNSEEN messages
     in INBOX, and fetch them.
  3. For each reply we parse the In-Reply-To / References headers and
     look up the original outbound `email_messages` row (which stored its
     own Message-ID under `gmail_message_id`).
  4. From that row we know the `thread_id`, so we hand a synthetic
     NormalizedInboundEmail (with `to_token=thread_id`) to the existing
     `process_inbound_message` and let it do the rest -- insert row,
     flip thread status, emit socket events, fire notification.
  5. The message is flagged Seen on the server so we never reprocess it.

Idempotency is layered:
  - IMAP \\Seen flag means a re-poll skips messages we've handled.
  - `email_messages.gmail_message_id` UNIQUE constraint catches the
    edge case where a poll inserts but crashes before flagging Seen.

Configuration (env):
  IMAP_HOST       - default `imap.hostinger.com`
  IMAP_PORT       - default 993 (SSL)
  IMAP_USE_SSL    - default true
  IMAP_USER       - default to SMTP_USER (same mailbox)
  IMAP_PASSWORD   - default to SMTP_PASSWORD
  IMAP_MAILBOX    - default INBOX
  IMAP_POLL_INTERVAL - seconds between polls, default 60
"""
from __future__ import annotations

import asyncio
import email
import email.utils
import imaplib
import logging
import os
import re
import ssl
from datetime import datetime, timezone
from email.message import Message
from typing import Optional

from backend.services.email_inbound import (
    NormalizedInboundEmail,
    process_inbound_message,
)
from backend.services.supabase import get_db

logger = logging.getLogger("aria.services.imap_inbound")


# ── Config ────────────────────────────────────────────────────────────


def _imap_config() -> dict:
    """Read IMAP settings from env. Falls back to SMTP_* for credentials
    so the operator only has to configure one mailbox."""
    use_ssl_raw = (os.environ.get("IMAP_USE_SSL") or "true").strip().lower()
    return {
        "host": (os.environ.get("IMAP_HOST") or "imap.hostinger.com").strip(),
        "port": int((os.environ.get("IMAP_PORT") or "993").strip() or "993"),
        "use_ssl": use_ssl_raw in ("1", "true", "yes"),
        "user": (
            os.environ.get("IMAP_USER")
            or os.environ.get("SMTP_USER")
            or ""
        ).strip(),
        "password": (
            os.environ.get("IMAP_PASSWORD")
            or os.environ.get("SMTP_PASSWORD")
            or ""
        ),
        "mailbox": (os.environ.get("IMAP_MAILBOX") or "INBOX").strip() or "INBOX",
    }


def _poll_interval_seconds() -> int:
    raw = (os.environ.get("IMAP_POLL_INTERVAL") or "60").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 60
    # Clamp to a sane range — too aggressive trips Hostinger's connection
    # limit, too lazy and replies sit in the queue forever.
    return max(15, min(n, 600))


# ── Header parsing ────────────────────────────────────────────────────


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        decoded = email.header.decode_header(value)
        out: list[str] = []
        for chunk, charset in decoded:
            if isinstance(chunk, bytes):
                out.append(chunk.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(chunk)
        return "".join(out).strip()
    except Exception:
        return value.strip()


def _split_message_ids(header_value: str) -> list[str]:
    """Both In-Reply-To and References can hold one or more <Message-ID>
    tokens separated by whitespace. Returns the bracketed values without
    angle brackets, in document order."""
    if not header_value:
        return []
    return re.findall(r"<([^>]+)>", header_value)


def _extract_bodies(msg: Message) -> tuple[str, str]:
    """Pull text/plain and text/html out of a parsed email Message.

    For multipart/alternative we prefer the first text/plain part for
    `text_body` and the first text/html part for `html_body`. Attachments
    and inline images are ignored — the inbound flow only cares about
    the human-readable reply.
    """
    text_body = ""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            if ctype == "text/plain" and not text_body:
                payload = part.get_payload(decode=True) or b""
                text_body = payload.decode(
                    part.get_content_charset() or "utf-8", errors="replace",
                )
            elif ctype == "text/html" and not html_body:
                payload = part.get_payload(decode=True) or b""
                html_body = payload.decode(
                    part.get_content_charset() or "utf-8", errors="replace",
                )
    else:
        ctype = msg.get_content_type()
        payload = msg.get_payload(decode=True) or b""
        decoded = payload.decode(
            msg.get_content_charset() or "utf-8", errors="replace",
        )
        if ctype == "text/html":
            html_body = decoded
        else:
            text_body = decoded
    return text_body, html_body


def _strip_quoted_reply(text: str) -> str:
    """Best-effort trim of Gmail-style quoted reply chunks.

    Customers' reply clients append the original message under a line
    like "On <date>, <person> wrote:". We only want the new reply for
    the preview snippet -- the full body still gets stored verbatim.
    """
    if not text:
        return ""
    lines = text.splitlines()
    cut = len(lines)
    quote_lead = re.compile(
        r"^(On .+ wrote:|Le .+ a écrit\s*:|Am .+ schrieb .+:|>+\s)",
        re.IGNORECASE,
    )
    for i, ln in enumerate(lines):
        if quote_lead.match(ln.strip()):
            cut = i
            break
    return "\n".join(lines[:cut]).strip()


# ── Thread lookup by In-Reply-To / References ────────────────────────


def _find_thread_by_message_ids(message_ids: list[str]) -> Optional[str]:
    """Look up the parent `email_threads.id` by matching any of the
    given Message-IDs against rows in `email_messages.gmail_message_id`.

    The outbound SMTP path stores the Message-ID we generated locally
    in this column (it was named `gmail_message_id` historically; it's
    really the provider message id slot). When a reply arrives, its
    In-Reply-To header points back at one of those IDs.
    """
    if not message_ids:
        return None

    sb = get_db()
    # Try each id in order — the most recent reference is usually
    # In-Reply-To's first token; References fans out further back.
    for mid in message_ids:
        if not mid:
            continue
        try:
            res = (
                sb.table("email_messages")
                .select("thread_id")
                .eq("gmail_message_id", mid)
                .limit(1)
                .execute()
            )
            if res.data and res.data[0].get("thread_id"):
                return res.data[0]["thread_id"]
        except Exception as e:
            logger.debug("[imap] thread lookup failed for mid=%s: %s", mid, e)
    return None


def _find_thread_by_contact_subject(
    from_email: str, subject: str,
) -> Optional[str]:
    """Last-resort thread match: find an open thread for this contact
    whose subject matches (case-insensitive, with optional `Re:` prefix
    stripped). Avoids dropping replies on the floor when the customer's
    client doesn't preserve In-Reply-To (some webmail clients strip it
    when replying via a non-original mailbox).
    """
    contact = (from_email or "").strip().lower()
    if not contact:
        return None
    subj = re.sub(r"^\s*re\s*:\s*", "", (subject or "").strip(), flags=re.I).lower()
    if not subj:
        return None

    sb = get_db()
    try:
        res = (
            sb.table("email_threads")
            .select("id, subject")
            .ilike("contact_email", contact)
            .order("last_message_at", desc=True)
            .limit(20)
            .execute()
        )
        for row in res.data or []:
            row_subj = re.sub(
                r"^\s*re\s*:\s*", "", (row.get("subject") or "").strip(), flags=re.I,
            ).lower()
            if row_subj and row_subj == subj:
                return row["id"]
    except Exception as e:
        logger.debug("[imap] contact+subject lookup failed: %s", e)
    return None


# ── Single-message processor ──────────────────────────────────────────


def _normalize_imap_message(raw_bytes: bytes) -> tuple[Optional[NormalizedInboundEmail], list[str]]:
    """Parse raw RFC822 bytes into a NormalizedInboundEmail.

    Returns (normalized, message_ids) where message_ids is the list of
    Message-IDs from In-Reply-To + References, ordered most-recent
    first. Caller uses message_ids to find the parent thread.
    """
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception as e:
        logger.warning("[imap] failed to parse RFC822: %s", e)
        return None, []

    from_raw = _decode_header(msg.get("From"))
    addr_match = email.utils.parseaddr(from_raw)
    from_name = (addr_match[0] or "").strip()
    from_email = (addr_match[1] or "").strip()

    subject = _decode_header(msg.get("Subject"))
    in_reply_to = (msg.get("In-Reply-To") or "").strip()
    references = (msg.get("References") or "").strip()
    provider_mid = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    provider_mid = provider_mid.strip("<> ")

    # Most-specific to most-general
    mid_chain = _split_message_ids(in_reply_to) + _split_message_ids(references)

    text_body, html_body = _extract_bodies(msg)
    text_body_clean = _strip_quoted_reply(text_body)

    received_at = datetime.now(timezone.utc).isoformat()
    date_hdr = msg.get("Date")
    if date_hdr:
        try:
            parsed = email.utils.parsedate_to_datetime(date_hdr)
            if parsed:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                received_at = parsed.astimezone(timezone.utc).isoformat()
        except Exception:
            pass

    to_full = _decode_header(msg.get("To"))

    normalized: NormalizedInboundEmail = {
        "from_email": from_email,
        "from_name": from_name,
        "to_token": "",   # filled in by caller after thread lookup
        "to_full": to_full,
        "subject": subject,
        "text_body": text_body_clean or text_body,
        "html_body": html_body,
        "in_reply_to": in_reply_to,
        "provider_message_id": provider_mid,
        "received_at": received_at,
        "raw_provider_payload": {"source": "imap", "size": len(raw_bytes)},
    }
    return normalized, mid_chain


# ── Polling ──────────────────────────────────────────────────────────


def _imap_login(cfg: dict) -> imaplib.IMAP4:
    """Synchronous IMAP login. Wrapped in asyncio.to_thread by callers."""
    if cfg["use_ssl"]:
        context = ssl.create_default_context()
        client = imaplib.IMAP4_SSL(cfg["host"], cfg["port"], ssl_context=context, timeout=30)
    else:
        client = imaplib.IMAP4(cfg["host"], cfg["port"], timeout=30)
    client.login(cfg["user"], cfg["password"])
    return client


def _poll_once_sync(cfg: dict) -> tuple[int, int]:
    """Connect, process UNSEEN messages, return (processed, errors).

    Synchronous (imaplib has no asyncio variant in stdlib). Callers run
    this through asyncio.to_thread so the polling loop stays cooperative.
    Each successfully processed message is flagged \\Seen so the next
    poll skips it.
    """
    processed = 0
    errors = 0
    client: Optional[imaplib.IMAP4] = None
    try:
        client = _imap_login(cfg)
        status, _ = client.select(cfg["mailbox"], readonly=False)
        if status != "OK":
            logger.warning("[imap] mailbox select failed: %s", cfg["mailbox"])
            return 0, 1

        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            logger.warning("[imap] UNSEEN search failed")
            return 0, 1

        ids_blob = data[0] if data and data[0] else b""
        msg_ids = ids_blob.split()
        if not msg_ids:
            return 0, 0

        for mid in msg_ids:
            try:
                status, fetched = client.fetch(mid, "(RFC822)")
                if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                    errors += 1
                    continue
                raw = fetched[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    errors += 1
                    continue

                normalized, mid_chain = _normalize_imap_message(bytes(raw))
                if not normalized:
                    errors += 1
                    # Still mark Seen so we don't re-fail on it forever.
                    client.store(mid, "+FLAGS", "\\Seen")
                    continue

                # Look up the parent thread via the reply chain. Fall back
                # to (contact, subject) match if no header chain matches.
                thread_id = _find_thread_by_message_ids(mid_chain)
                if not thread_id:
                    thread_id = _find_thread_by_contact_subject(
                        normalized["from_email"], normalized["subject"],
                    )

                if not thread_id:
                    logger.info(
                        "[imap] no matching thread for from=%s subject=%r — leaving UNSEEN",
                        normalized["from_email"], normalized["subject"][:80],
                    )
                    # Don't mark Seen — future inbound work (or a manual
                    # thread re-link) might be able to claim it later.
                    errors += 1
                    continue

                normalized["to_token"] = thread_id

                # Hand off to the existing inbound persistence layer. It
                # writes email_messages, flips thread status, emits
                # socket events, fires the bell-ticker notification.
                # We need to call an async function from sync code here;
                # do it via a fresh event loop on the worker thread.
                result = asyncio.run(process_inbound_message(normalized))
                if result.get("ok") or result.get("duplicate"):
                    processed += 1
                else:
                    errors += 1
                    logger.warning(
                        "[imap] process_inbound_message failed: %s",
                        result.get("error"),
                    )

                # Flag Seen even on duplicate so we stop reprocessing.
                client.store(mid, "+FLAGS", "\\Seen")

            except Exception as e:
                errors += 1
                logger.exception("[imap] message %r failed: %s", mid, e)

        return processed, errors
    except imaplib.IMAP4.error as e:
        logger.warning("[imap] login/protocol error: %s", e)
        return 0, 1
    except (TimeoutError, ConnectionError, OSError) as e:
        logger.warning("[imap] connection error: %s", e)
        return 0, 1
    except Exception as e:
        logger.exception("[imap] unexpected poller failure: %s", e)
        return 0, 1
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass


async def poll_once() -> tuple[int, int]:
    """Async entry point used by the lifespan loop and by ad-hoc calls
    (e.g. a manual /sync trigger from the frontend). Returns
    (processed, errors). Never raises."""
    cfg = _imap_config()
    missing = [k for k in ("host", "user", "password") if not cfg[k]]
    if missing:
        logger.debug(
            "[imap] not configured (missing: %s); skipping poll",
            ",".join(missing),
        )
        return 0, 0
    return await asyncio.to_thread(_poll_once_sync, cfg)


async def imap_poll_loop() -> None:
    """Background lifespan loop. Polls every IMAP_POLL_INTERVAL seconds.

    We deliberately use a fixed interval (with jitter on the failure
    path) instead of long-polling IMAP IDLE — IDLE keeps a TCP
    connection open, and Hostinger's connection limits make idle
    connections expensive when we add more tenants. Cheap polling at
    60s scales much further before we have to revisit.
    """
    interval = _poll_interval_seconds()
    backoff = 1
    logger.info("[imap] poll loop started (interval=%ds)", interval)
    while True:
        try:
            processed, errors = await poll_once()
            if processed:
                logger.info("[imap] processed=%d errors=%d", processed, errors)
            backoff = 1
        except asyncio.CancelledError:
            logger.info("[imap] poll loop cancelled")
            return
        except Exception as e:
            logger.exception("[imap] poll iteration crashed: %s", e)
            backoff = min(backoff * 2, 8)

        await asyncio.sleep(interval * backoff)
