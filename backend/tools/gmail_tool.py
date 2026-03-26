"""Gmail API wrapper — send emails via Google OAuth access token."""
from __future__ import annotations

import base64
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import httpx

GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
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
    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        })
        resp.raise_for_status()
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
        resp.raise_for_status()
        data = resp.json()
        return {"status_code": resp.status_code, "message_id": data.get("id", "")}
