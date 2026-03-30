"""Gmail API wrapper — send/read emails via Google OAuth access token."""
from __future__ import annotations

import base64
import logging
import os
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import httpx

logger = logging.getLogger("aria.gmail")

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_SEND_URL = f"{GMAIL_API_BASE}/messages/send"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _build_mime_message(to: str, subject: str, html_body: str, from_email: str) -> str:
    """Build RFC 2822 MIME message and return base64url-encoded string."""
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["From"] = from_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


async def refresh_access_token(refresh_token: str) -> str:
    """Exchange a refresh token for a fresh access token."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env to refresh Gmail tokens"
        )

    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        })
        if resp.status_code != 200:
            error_detail = resp.json().get("error_description", resp.text[:200]) if resp.text else "unknown"
            raise RuntimeError(
                f"Google token refresh failed ({resp.status_code}): {error_detail}. "
                "The user needs to reconnect Gmail in Settings > Integrations."
            )
        return resp.json()["access_token"]


async def send_email(
    access_token: str,
    to: str,
    subject: str,
    html_body: str,
    from_email: str,
) -> dict:
    """Send an email via Gmail API. Returns message_id on success."""
    raw = _build_mime_message(to, subject, html_body, from_email)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GMAIL_SEND_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            json={"raw": raw},
        )
        if resp.status_code == 401:
            return {"error": "token_expired", "status_code": 401}
        if resp.status_code >= 400:
            # Return error details instead of crashing — caller handles the failure
            try:
                detail = resp.json().get("error", {}).get("message", resp.text[:200])
            except Exception:
                detail = resp.text[:200]
            return {"error": f"gmail_api_error", "detail": detail, "status_code": resp.status_code}
        data = resp.json()
        return {
            "status_code": resp.status_code,
            "message_id": data.get("id", ""),
            "thread_id": data.get("threadId", ""),
        }


# ─── Gmail Read / List ───


def _decode_body_part(part: dict) -> str:
    """Decode a Gmail message body part from base64url."""
    body = part.get("body", {})
    data = body.get("data", "")
    if data:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    return ""


def _extract_bodies(payload: dict) -> tuple[str, str]:
    """Extract text and html bodies from a Gmail message payload.

    Returns (text_body, html_body).
    """
    text_body = ""
    html_body = ""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        text_body = _decode_body_part(payload)
    elif mime_type == "text/html":
        html_body = _decode_body_part(payload)
    elif "multipart" in mime_type:
        for sub_part in payload.get("parts", []):
            sub_mime = sub_part.get("mimeType", "")
            if sub_mime == "text/plain" and not text_body:
                text_body = _decode_body_part(sub_part)
            elif sub_mime == "text/html" and not html_body:
                html_body = _decode_body_part(sub_part)
            elif "multipart" in sub_mime:
                # Nested multipart (e.g. multipart/alternative inside multipart/mixed)
                t, h = _extract_bodies(sub_part)
                if not text_body:
                    text_body = t
                if not html_body:
                    html_body = h

    return text_body, html_body


def _get_header(headers: list[dict], name: str) -> str:
    """Get a header value from Gmail API headers list (case-insensitive)."""
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _strip_html(html: str) -> str:
    """Very lightweight HTML to plain text."""
    text = re.sub(r'<br\s*/?\s*>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    return text.strip()


async def list_messages(
    access_token: str,
    query: str = "",
    max_results: int = 20,
) -> dict:
    """List message IDs matching a query. Returns {"messages": [...], "error": ...}."""
    params: dict = {"maxResults": max_results}
    if query:
        params["q"] = query
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        if resp.status_code == 401:
            return {"error": "token_expired", "messages": []}
        if resp.status_code >= 400:
            return {"error": f"gmail_api_error ({resp.status_code})", "messages": []}
        data = resp.json()
        return {"messages": data.get("messages", [])}


async def get_message(access_token: str, message_id: str) -> dict:
    """Fetch a single full message by ID. Returns parsed message dict."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/messages/{message_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"format": "full"},
        )
        if resp.status_code == 401:
            return {"error": "token_expired"}
        if resp.status_code >= 400:
            return {"error": f"gmail_api_error ({resp.status_code})"}
        data = resp.json()
        payload = data.get("payload", {})
        headers = payload.get("headers", [])
        text_body, html_body = _extract_bodies(payload)

        # Build preview snippet
        snippet = data.get("snippet", "")
        if not snippet and text_body:
            snippet = text_body[:200]
        elif not snippet and html_body:
            snippet = _strip_html(html_body)[:200]

        return {
            "gmail_message_id": data.get("id", ""),
            "gmail_thread_id": data.get("threadId", ""),
            "subject": _get_header(headers, "Subject"),
            "from": _get_header(headers, "From"),
            "to": _get_header(headers, "To"),
            "date": _get_header(headers, "Date"),
            "in_reply_to": _get_header(headers, "In-Reply-To"),
            "references": _get_header(headers, "References"),
            "message_id_header": _get_header(headers, "Message-ID"),
            "text_body": text_body,
            "html_body": html_body,
            "preview_snippet": snippet,
            "label_ids": data.get("labelIds", []),
            "internal_date": data.get("internalDate", ""),
        }


async def get_thread(access_token: str, thread_id: str) -> dict:
    """Fetch an entire Gmail thread (all messages)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/threads/{thread_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"format": "full"},
        )
        if resp.status_code == 401:
            return {"error": "token_expired"}
        if resp.status_code >= 400:
            return {"error": f"gmail_api_error ({resp.status_code})"}
        data = resp.json()
        messages = []
        for msg in data.get("messages", []):
            payload = msg.get("payload", {})
            headers = payload.get("headers", [])
            text_body, html_body = _extract_bodies(payload)
            snippet = msg.get("snippet", "")
            messages.append({
                "gmail_message_id": msg.get("id", ""),
                "gmail_thread_id": msg.get("threadId", ""),
                "subject": _get_header(headers, "Subject"),
                "from": _get_header(headers, "From"),
                "to": _get_header(headers, "To"),
                "date": _get_header(headers, "Date"),
                "text_body": text_body,
                "html_body": html_body,
                "preview_snippet": snippet,
                "label_ids": msg.get("labelIds", []),
                "internal_date": msg.get("internalDate", ""),
            })
        return {
            "thread_id": data.get("id", ""),
            "messages": messages,
        }


async def list_history(
    access_token: str,
    start_history_id: str,
    label_id: str = "INBOX",
) -> dict:
    """List history events since a given history ID. Used for incremental sync."""
    params: dict = {
        "startHistoryId": start_history_id,
        "historyTypes": "messageAdded",
    }
    if label_id:
        params["labelId"] = label_id
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/history",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        if resp.status_code == 401:
            return {"error": "token_expired"}
        if resp.status_code == 404:
            # historyId too old — need full sync
            return {"error": "history_expired", "history": []}
        if resp.status_code >= 400:
            return {"error": f"gmail_api_error ({resp.status_code})", "history": []}
        data = resp.json()
        return {
            "history": data.get("history", []),
            "history_id": data.get("historyId", ""),
        }


async def get_profile(access_token: str) -> dict:
    """Get the Gmail user profile (for historyId and email address)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/profile",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code == 401:
            return {"error": "token_expired"}
        if resp.status_code >= 400:
            return {"error": f"gmail_api_error ({resp.status_code})"}
        data = resp.json()
        return {
            "email": data.get("emailAddress", ""),
            "history_id": data.get("historyId", ""),
            "messages_total": data.get("messagesTotal", 0),
        }
