"""Email Marketer Agent — produces copy-paste-ready email campaigns."""
from __future__ import annotations

from backend.agents.base import BaseAgent

_agent = None


class EmailMarketerAgent(BaseAgent):
    AGENT_NAME = "email_marketer"
    CONTEXT_KEY = "type"
    DEFAULT_CONTEXT = "newsletter"

    def build_system_prompt(self, config, campaign_type: str) -> str:
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

Return JSON: campaign_type, emails[] (each with subject_variants[], preview_text, body, send_time, segmentation_notes)"""

    def build_user_message(self, campaign_type: str, context: dict | None) -> str:
        return f"Create {campaign_type} campaign. Context: {context}"


def _get():
    global _agent
    if _agent is None:
        _agent = EmailMarketerAgent()
    return _agent


AGENT_NAME = EmailMarketerAgent.AGENT_NAME


async def run(tenant_id: str, context: dict | None = None) -> dict:
    return await _get().run(tenant_id, context)
