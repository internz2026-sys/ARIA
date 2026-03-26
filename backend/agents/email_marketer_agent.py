"""Email Marketer Agent — produces email campaigns and can send via Gmail."""
from __future__ import annotations

import json
import logging
import re

from backend.agents.base import BaseAgent

logger = logging.getLogger("aria.email_marketer")

_agent = None


class EmailMarketerAgent(BaseAgent):
    AGENT_NAME = "email_marketer"
    CONTEXT_KEY = "type"
    DEFAULT_CONTEXT = "newsletter"

    def build_system_prompt(self, config, campaign_type: str) -> str:
        gmail_connected = bool(config.integrations.google_access_token)
        send_instructions = ""
        if gmail_connected:
            send_instructions = f"""

IMPORTANT — Gmail is connected for {config.owner_email}. When the task asks you to SEND an email
(not just draft), you MUST include a ```send_email``` JSON block at the END of your response for each
email that should actually be sent:
```send_email
{{"to": "recipient@example.com", "subject": "Subject line", "html_body": "<html email body>"}}
```
You can include multiple ```send_email``` blocks if sending to multiple recipients.
The from address will automatically be {config.owner_email}.
Only include send blocks when explicitly asked to send. For drafts/campaigns, just provide the copy."""

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
Developer founders should be able to copy-paste directly into their ESP.

Return JSON: campaign_type, emails[] (each with subject_variants[], preview_text, body, send_time, segmentation_notes)
{send_instructions}"""

    def build_user_message(self, campaign_type: str, context: dict | None) -> str:
        return f"Create {campaign_type} campaign. Context: {context}"


def _get():
    global _agent
    if _agent is None:
        _agent = EmailMarketerAgent()
    return _agent


AGENT_NAME = EmailMarketerAgent.AGENT_NAME


def parse_send_blocks(content: str) -> list[dict]:
    """Extract ```send_email``` JSON blocks from agent output."""
    blocks = re.findall(r"```send_email\s*\n(.*?)\n```", content, re.DOTALL)
    emails = []
    for block in blocks:
        try:
            d = json.loads(block.strip())
            if d.get("to") and d.get("subject") and d.get("html_body"):
                emails.append(d)
        except json.JSONDecodeError:
            pass
    return emails


async def send_emails_via_gmail(tenant_id: str, emails: list[dict]) -> list[dict]:
    """Send parsed email blocks via Gmail. Returns results for each send."""
    from backend.config.loader import get_tenant_config, save_tenant_config
    from backend.tools import gmail_tool

    config = get_tenant_config(tenant_id)
    access_token = config.integrations.google_access_token
    refresh_token = config.integrations.google_refresh_token

    if not access_token:
        return [{"error": "Gmail not connected"}]

    results = []
    for email in emails:
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

        results.append({"to": email["to"], "subject": email["subject"], **result})
        logger.info("Gmail send to=%s subject=%s result=%s", email["to"], email["subject"], result)

    return results


async def run(tenant_id: str, context: dict | None = None) -> dict:
    result = await _get().run(tenant_id, context)

    # Check if the agent produced send_email blocks
    content = result.get("result", "")
    if isinstance(content, str):
        emails_to_send = parse_send_blocks(content)
        if emails_to_send:
            send_results = await send_emails_via_gmail(tenant_id, emails_to_send)
            result["emails_sent"] = send_results
            result["send_count"] = len([r for r in send_results if "message_id" in r])

    return result
