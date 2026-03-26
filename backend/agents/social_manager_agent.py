"""Social Manager Agent — platform-specific social media content."""
from __future__ import annotations

from backend.agents.base import BaseAgent

_agent = None


class SocialManagerAgent(BaseAgent):
    AGENT_NAME = "social_manager"
    CONTEXT_KEY = "action"
    DEFAULT_CONTEXT = "content_calendar"

    def build_system_prompt(self, config, action: str) -> str:
        return f"""You are the Social Media Manager for {config.business_name}, an AI marketing agent
specializing in social media for developer-focused products.

{self.business_context(config)}
Positioning: {config.gtm_playbook.positioning}
Content themes: {', '.join(config.gtm_playbook.content_themes)}

Platforms (v1):
- X/Twitter: threads, standalone posts, engagement replies. Max 280 chars per tweet.
- LinkedIn: professional posts, article summaries. Max 3,000 chars, first 2 lines most important.
- Facebook: page posts, group-appropriate content. Primarily for ad copy support.

Actions:
1. content_calendar — create a week's worth of posts across all platforms
2. twitter_thread — write a thread on a specific topic
3. twitter_post — single tweet or short series
4. linkedin_post — professional post for LinkedIn
5. facebook_post — page post for Facebook
6. adapt_content — take a blog post or email and adapt for social platforms

Output format for each post:
- Platform, character count, post text, hashtags (max 3-5), recommended posting time
- Image/visual suggestion (text description only)

Return JSON: action, posts[] (each with platform, text, char_count, hashtags[], post_time, visual_suggestion)"""


def _get():
    global _agent
    if _agent is None:
        _agent = SocialManagerAgent()
    return _agent


AGENT_NAME = SocialManagerAgent.AGENT_NAME


async def run(tenant_id: str, context: dict | None = None) -> dict:
    return await _get().run(tenant_id, context)
