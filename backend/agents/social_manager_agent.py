"""Social Manager Agent — adapts content from Content Writer for social platforms + publishes."""
from __future__ import annotations

import json
import logging
import re

from backend.agents.base import BaseAgent, MODEL_HAIKU

logger = logging.getLogger("aria.social_manager")

_agent = None

# Keywords that trigger content library lookup
_CONTENT_KEYWORDS = ["adapt", "promote", "share", "post about", "tweet about",
                     "blog post", "latest content", "content writer", "repurpose"]


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

Create social media posts for TWO platforms from the given task or source content.

Return ONLY valid JSON (no markdown fences):
{{
  "action": "adapt_content",
  "posts": [
    {{"platform": "twitter", "text": "...", "hashtags": ["...", "..."]}},
    {{"platform": "linkedin", "text": "...", "hashtags": ["...", "..."]}}
  ]
}}

Rules:
- Exactly 2 posts: one for Twitter, one for LinkedIn.
- Twitter: max 280 chars including hashtags. Punchy, conversational, native-feeling. 2-3 hashtags max.
- LinkedIn: max 3000 chars. Professional, insightful, thought-leadership tone. Can be longer and more detailed than the tweet. 3-5 hashtags.
- Make both compelling and shareable.
- Each post should feel native to its platform."""

    def build_user_message(self, action: str, context: dict | None) -> str:
        source_content = (context or {}).get("source_content", "")
        if source_content:
            return f"Task: {action}\n\nSource content to adapt:\n{source_content[:2000]}"
        return f"Task: {action}"

    async def run(self, tenant_id: str, context: dict | None = None) -> dict:
        """Override run to fetch content from inbox when task involves adapting."""
        action = (context or {}).get(self.CONTEXT_KEY, self.DEFAULT_CONTEXT)

        # Check if this task needs content from the library
        action_lower = action.lower()
        if any(kw in action_lower for kw in _CONTENT_KEYWORDS):
            source_content = await _fetch_recent_content(tenant_id)
            if source_content:
                context = dict(context or {})
                context["source_content"] = source_content
                logger.info("Injected %d chars of source content for social adaptation", len(source_content))

        # Run the agent normally
        result = await super().run(tenant_id, context)

        # Parse posts but DO NOT auto-publish — posts go to Inbox for approval
        raw = result.get("result", "")
        posts = _parse_posts(raw)
        if posts:
            result["social_posts"] = posts

        return result


async def _fetch_recent_content(tenant_id: str) -> str:
    """Fetch the most recent content from inbox (Content Writer output)."""
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()
        result = sb.table("inbox_items").select("title,content").eq(
            "tenant_id", tenant_id
        ).eq("agent", "content_writer").order(
            "created_at", desc=True
        ).limit(1).execute()

        if result.data:
            item = result.data[0]
            title = item.get("title", "")
            content = item.get("content", "")
            return f"Title: {title}\n\n{content[:2000]}"
    except Exception as e:
        logger.warning("Failed to fetch content for social adaptation: %s", e)
    return ""


def _parse_posts(raw: str) -> list[dict]:
    """Extract posts array from agent output."""
    try:
        # Try direct JSON parse
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
            return data.get("posts", [])
    except (json.JSONDecodeError, KeyError):
        pass

    # Try finding JSON array
    try:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end])
    except (json.JSONDecodeError, KeyError):
        pass

    return []


async def _auto_publish(tenant_id: str, posts: list[dict]) -> list[dict]:
    """Publish parsed posts to connected platforms. Returns results."""
    from backend.config.loader import get_tenant_config
    config = get_tenant_config(tenant_id)
    results = []

    for post in posts:
        platform = post.get("platform", "").lower()
        text = post.get("text", "")
        hashtags = post.get("hashtags", [])
        if not text:
            continue

        # Append hashtags if not already in text
        if hashtags:
            tag_str = " ".join(f"#{t.strip('#')}" for t in hashtags)
            if tag_str not in text:
                full_text = f"{text}\n\n{tag_str}"
            else:
                full_text = text
        else:
            full_text = text

        if platform == "twitter":
            token = config.integrations.twitter_access_token
            if not token:
                results.append({"platform": "twitter", "status": "skipped", "reason": "not_connected"})
                continue
            try:
                from backend.tools import twitter_tool
                # Truncate to 280 chars for Twitter
                tweet_text = full_text[:280]
                r = await twitter_tool.post_tweet(token, tweet_text)
                if r.get("error") == "token_expired" and config.integrations.twitter_refresh_token:
                    tokens = await twitter_tool.refresh_access_token(config.integrations.twitter_refresh_token)
                    config.integrations.twitter_access_token = tokens["access_token"]
                    config.integrations.twitter_refresh_token = tokens.get("refresh_token", config.integrations.twitter_refresh_token)
                    from backend.config.loader import save_tenant_config
                    save_tenant_config(config)
                    r = await twitter_tool.post_tweet(tokens["access_token"], tweet_text)
                if r.get("error"):
                    results.append({"platform": "twitter", "status": "failed", "error": r["error"]})
                else:
                    results.append({"platform": "twitter", "status": "published", "tweet_id": r.get("tweet_id", "")})
                    logger.info("Published tweet for tenant %s: %s", tenant_id, r.get("tweet_id"))
            except Exception as e:
                results.append({"platform": "twitter", "status": "failed", "error": str(e)})

        elif platform == "linkedin":
            results.append({"platform": "linkedin", "status": "skipped", "reason": "not_integrated_yet"})

        elif platform == "facebook":
            results.append({"platform": "facebook", "status": "skipped", "reason": "not_integrated_yet"})

        else:
            results.append({"platform": platform, "status": "skipped", "reason": "unknown_platform"})

    return results


def _get():
    global _agent
    if _agent is None:
        _agent = SocialManagerAgent()
    return _agent


AGENT_NAME = SocialManagerAgent.AGENT_NAME


async def run(tenant_id: str, context: dict | None = None) -> dict:
    return await _get().run(tenant_id, context)
