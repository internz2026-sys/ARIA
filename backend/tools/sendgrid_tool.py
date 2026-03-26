"""SendGrid API wrapper — email delivery + tracking."""
from __future__ import annotations

import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content


def _client() -> SendGridAPIClient:
    return SendGridAPIClient(os.environ.get("SENDGRID_API_KEY", ""))


async def send_email(to: str, subject: str, html_body: str, from_email: str | None = None) -> dict:
    """Send an email via SendGrid."""
    message = Mail(
        from_email=Email(from_email or os.environ.get("SENDGRID_FROM_EMAIL", "")),
        to_emails=To(to),
        subject=subject,
        html_content=Content("text/html", html_body),
    )
    response = _client().send(message)
    return {"status_code": response.status_code, "message_id": response.headers.get("X-Message-Id", "")}


async def send_template_email(to: str, template_id: str, dynamic_data: dict) -> dict:
    """Send a templated email with dynamic data."""
    message = Mail(from_email=Email(os.environ.get("SENDGRID_FROM_EMAIL", "")), to_emails=To(to))
    message.template_id = template_id
    message.dynamic_template_data = dynamic_data
    response = _client().send(message)
    return {"status_code": response.status_code, "message_id": response.headers.get("X-Message-Id", "")}


async def get_email_stats(message_id: str) -> dict:
    """Get open/click stats for a sent email."""
    response = _client().client.messages._(message_id).get()
    return response.to_dict if hasattr(response, "to_dict") else {"status": "retrieved"}
