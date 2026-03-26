"""Email Marketer Agent — produces email campaigns and sends via Gmail."""
from __future__ import annotations

import json
import logging
import re

from backend.agents.base import BaseAgent

logger = logging.getLogger("aria.email_marketer")

_agent = None

# Regex to find email addresses in task descriptions
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


class EmailMarketerAgent(BaseAgent):
    AGENT_NAME = "email_marketer"
    CONTEXT_KEY = "action"  # tasks come in via context={"action": "..."}
    DEFAULT_CONTEXT = "draft a newsletter"

    def build_system_prompt(self, config, action: str) -> str:
        # Detect if this is a send task with a recipient
        recipient = _extract_recipient(action)
        send_note = ""
        if recipient:
            send_note = f"""

IMPORTANT: This email will be SENT to {recipient} from {config.owner_email} after you draft it.
Write a complete, professional, ready-to-send email. Include a clear subject line.

Structure your response as:
SUBJECT: <the subject line>
---
<the full HTML email body>

Make the HTML body professional with proper formatting. Do NOT include placeholder text."""

        return f"""You are the Email Marketer for {config.business_name}, an AI marketing agent
that creates complete email campaigns for developer-focused products.

{self.business_context(config)}
Positioning: {config.gtm_playbook.positioning}

Campaign types:
1. welcome_sequence — 3-5 email series for new signups (intro, value, activation)
2. newsletter — weekly/biweekly with product updates, content roundup, community highlights
3. launch_sequence — pre-launch teaser, launch day announcement, social proof follow-up, final reminder
4. re_engagement — win-back emails for inactive users
5. product_update — feature announcement with clear benefit framing

For each email provide:
- Subject line (2-3 A/B variants)
- Preview text
- Email body (plain text + HTML-ready format)
- Recommended send time and day
- Segmentation notes (if applicable)

Keep emails concise, value-driven, one clear CTA per email.
{send_note}"""

    def build_user_message(self, action: str, context: dict | None) -> str:
        return action or "Draft a newsletter campaign."


def _get():
    global _agent
    if _agent is None:
        _agent = EmailMarketerAgent()
    return _agent


AGENT_NAME = EmailMarketerAgent.AGENT_NAME


def _extract_recipient(task: str) -> str | None:
    """Pull the first email address from a task description."""
    match = _EMAIL_RE.search(task or "")
    return match.group(0) if match else None


def _extract_subject_and_body(content: str) -> tuple[str, str]:
    """Parse SUBJECT: ... --- <body> format from agent output.

    Falls back to using the first line as subject and rest as body.
    """
    # Try SUBJECT: ... --- <body> format
    m = re.match(r"(?:SUBJECT:\s*)(.+?)(?:\n---\n|\n\n)(.*)", content, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Try "Subject:" anywhere in the text
    m = re.search(r"(?:Subject:\s*)(.+?)(?:\n)", content, re.IGNORECASE)
    if m:
        subject = m.group(1).strip().strip('"').strip("*")
        # Body is everything after the subject line
        body_start = m.end()
        body = content[body_start:].strip()
        # Remove leading --- or dashes
        body = re.sub(r"^-{2,}\s*\n?", "", body).strip()
        if body:
            return subject, body

    # Fallback: first line as subject, rest as body
    lines = content.strip().split("\n", 1)
    subject = lines[0].strip().strip("#").strip("*").strip()
    body = lines[1].strip() if len(lines) > 1 else content
    return subject, body


def _wrap_html(body: str) -> str:
    """Ensure body is wrapped in basic HTML if it isn't already."""
    if "<html" in body.lower() or "<body" in body.lower():
        return body
    # Convert markdown-ish text to basic HTML
    html_body = body.replace("\n\n", "</p><p>").replace("\n", "<br>")
    return f"""<html><body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
<div style="max-width: 600px; margin: 0 auto; padding: 20px;">
<p>{html_body}</p>
</div>
</body></html>"""


async def send_emails_via_gmail(tenant_id: str, emails: list[dict]) -> list[dict]:
    """Send email dicts via Gmail. Returns results for each send."""
    from backend.config.loader import get_tenant_config, save_tenant_config
    from backend.tools import gmail_tool

    config = get_tenant_config(tenant_id)
    access_token = config.integrations.google_access_token
    refresh_token = config.integrations.google_refresh_token

    if not access_token:
        logger.warning("Gmail not connected for tenant %s", tenant_id)
        return [{"error": "Gmail not connected"}]

    results = []
    for email in emails:
        try:
            result = await gmail_tool.send_email(
                access_token=access_token,
                to=email["to"],
                subject=email["subject"],
                html_body=email["html_body"],
                from_email=config.owner_email,
            )

            # Token expired — refresh and retry
            if result.get("error") == "token_expired" and refresh_token:
                try:
                    new_token = await gmail_tool.refresh_access_token(refresh_token)
                    access_token = new_token
                    config.integrations.google_access_token = new_token
                    save_tenant_config(config)
                    result = await gmail_tool.send_email(
                        access_token=new_token,
                        to=email["to"],
                        subject=email["subject"],
                        html_body=email["html_body"],
                        from_email=config.owner_email,
                    )
                except Exception as e:
                    result = {"error": f"Token refresh failed: {e}"}
        except Exception as e:
            logger.error("Gmail send exception to=%s: %s", email["to"], e)
            result = {"error": f"Send failed: {e}"}

        results.append({"to": email["to"], "subject": email["subject"], **result})
        logger.info("Gmail send to=%s subject=%s result=%s", email["to"], email["subject"], result)

    return results


async def run(tenant_id: str, context: dict | None = None) -> dict:
    result = await _get().run(tenant_id, context)

    content = result.get("result", "")
    if not isinstance(content, str) or not content:
        return result

    # Extract recipient from the original task description
    task_desc = (context or {}).get("action", "")
    recipient = _extract_recipient(task_desc)

    if recipient:
        # Task has a recipient email — parse the draft and send it
        subject, body = _extract_subject_and_body(content)
        html_body = _wrap_html(body)

        logger.info("Auto-sending email to %s (subject: %s)", recipient, subject)
        send_results = await send_emails_via_gmail(tenant_id, [{
            "to": recipient,
            "subject": subject,
            "html_body": html_body,
        }])
        result["emails_sent"] = send_results
        result["send_count"] = len([r for r in send_results if "message_id" in r])

    return result
