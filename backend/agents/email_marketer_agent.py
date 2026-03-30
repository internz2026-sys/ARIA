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
    MAX_TOKENS = 2000
    CONTEXT_FIELDS = {"business", "product", "audience", "pain_points", "voice"}

    def build_system_prompt(self, config, action: str) -> str:
        # Detect if this is a send task with a recipient
        recipient = _extract_recipient(action)
        send_note = ""
        if recipient:
            send_note = f"""

IMPORTANT: This email will be SENT to {recipient} from {config.owner_email} after you draft it.
Write a complete, professional, ready-to-send email.

Structure your response as:
SUBJECT: <the subject line>
---
<the full HTML email body>

The HTML body MUST be a complete, self-contained HTML email with ALL styling inline.
Use this exact structure:
- Start with <html><body style="...">
- Use a centered container <div style="max-width:600px; margin:0 auto; ...">
- Use inline styles on EVERY element (background colors, padding, font sizes, borders, etc.)
- Use <table> elements for layout sections (email clients need tables, not flexbox/grid)
- Include styled section headers with background colors
- Include styled CTA buttons with background-color and padding
- Use professional color scheme with brand-appropriate colors
- Do NOT use <style> blocks or CSS classes — ALL styles must be inline
- Do NOT include placeholder text — write real, ready-to-send content."""

        return f"""You are the Email Marketer for {config.business_name}, an AI marketing agent
that creates complete email campaigns for developer-focused products.

{self.business_context(config, self.CONTEXT_FIELDS)}
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
    """Ensure body is wrapped in a complete HTML email structure."""
    body_lower = body.lower()
    # Already a complete HTML document
    if "<html" in body_lower:
        return body
    # Rich HTML fragment (has styled elements, tables, divs) — wrap without mangling
    if any(tag in body_lower for tag in ["<table", "<div", "<td", 'style="', "<h1", "<h2", "<h3", "<section"]):
        return f"""<html><body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6; margin: 0; padding: 0; background-color: #f9f9f9;">
<div style="max-width: 600px; margin: 0 auto; padding: 20px; background-color: #ffffff;">
{body}
</div>
</body></html>"""
    # Plain text — convert to basic HTML
    html_body = body.replace("\n\n", "</p><p>").replace("\n", "<br>")
    return f"""<html><body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6; margin: 0; padding: 0; background-color: #f9f9f9;">
<div style="max-width: 600px; margin: 0 auto; padding: 20px; background-color: #ffffff;">
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

    # Proactively refresh if we have a refresh token but no access token
    if not access_token and refresh_token:
        try:
            access_token = await gmail_tool.refresh_access_token(refresh_token)
            config.integrations.google_access_token = access_token
            save_tenant_config(config)
        except Exception as e:
            logger.warning("Gmail proactive refresh failed for tenant %s: %s", tenant_id, e)

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

            # Token expired — try refresh, or clear if no refresh token
            if result.get("error") == "token_expired" and not refresh_token:
                config.integrations.google_access_token = None
                save_tenant_config(config)
                result = {"error": "Gmail session expired (no refresh token). Please reconnect Gmail in Settings > Integrations."}
            elif result.get("error") == "token_expired" and refresh_token:
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
                    # Refresh failed — clear access token but preserve refresh_token
                    # unless Google explicitly revoked it
                    config.integrations.google_access_token = None
                    if getattr(e, "is_revoked", False):
                        config.integrations.google_refresh_token = None
                        logger.warning("Google revoked refresh token for tenant %s — user must reconnect", tenant_id)
                    else:
                        logger.warning("Gmail token refresh failed (transient) for tenant %s: %s", tenant_id, e)
                    save_tenant_config(config)
                    result = {"error": "Gmail session expired. Please reconnect Gmail in Settings > Integrations."}
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

    # Also try to extract recipient from agent output if not found in task
    if not recipient:
        recipient = _extract_recipient(content)
        if recipient:
            logger.info("Extracted recipient from agent output: %s", recipient)

    if not recipient:
        logger.warning("No recipient email found in task or output for tenant %s. Task: %s", tenant_id, task_desc[:100])

    # Parse subject/body from agent output
    subject, body = _extract_subject_and_body(content)
    html_body = _wrap_html(body)

    # Build a plain-text preview snippet (first 200 chars of body text)
    text_body = re.sub(r'<[^>]+>', '', body).strip()
    preview_snippet = text_body[:200] + ("..." if len(text_body) > 200 else "")

    # Always return a structured draft — NEVER auto-send.
    # The user must explicitly approve before any email is sent.
    result["email_draft"] = {
        "to": recipient or "",
        "subject": subject,
        "html_body": html_body,
        "text_body": text_body,
        "preview_snippet": preview_snippet,
        "status": "draft_pending_approval",
    }

    return result
