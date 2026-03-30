"""Social Manager Agent — platform-specific social media content."""
from __future__ import annotations

from backend.agents.base import BaseAgent, MODEL_HAIKU

_agent = None


class SocialManagerAgent(BaseAgent):
    AGENT_NAME = "social_manager"
    CONTEXT_KEY = "action"
    DEFAULT_CONTEXT = "content_calendar"
    MODEL = MODEL_HAIKU
    MAX_TOKENS = 1500
    CONTEXT_FIELDS = {"business", "audience", "hangouts", "voice"}

    def build_system_prompt(self, config, action: str) -> str:
        return f"""You are the Social Media Manager for {config.business_name}.

{self.business_context(config, self.CONTEXT_FIELDS)}
Positioning: {config.gtm_playbook.positioning}

Platforms: X/Twitter (280 chars), LinkedIn (3000 chars), Facebook.
Actions: content_calendar, twitter_thread, twitter_post, linkedin_post, facebook_post, adapt_content.
Per post: platform, text, char_count, hashtags (3-5), post_time.
Return JSON: action, posts[]"""


def _get():
    global _agent
    if _agent is None:
        _agent = SocialManagerAgent()
    return _agent


AGENT_NAME = SocialManagerAgent.AGENT_NAME


async def run(tenant_id: str, context: dict | None = None) -> dict:
    return await _get().run(tenant_id, context)
