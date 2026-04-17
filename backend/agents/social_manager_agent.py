"""Social Manager Agent — adapts content from Content Writer for social platforms + publishes."""
from __future__ import annotations

import json
import logging
import re

from backend.agents.base import BaseAgent, MODEL_HAIKU

logger = logging.getLogger("aria.social_manager")

_agent = None

# Keyword triggers that say "this task should pull from a teammate's
# recent output". Matched case-insensitively against the task action.
_CONTENT_KEYWORDS = (
    "adapt", "promote", "share", "post about", "tweet about",
    "blog post", "latest content", "content writer", "repurpose",
)
_IMAGE_KEYWORDS = (
    "image", "photo", "picture", "banner", "visual", "graphic",
    "illustration", "thumbnail", "with an image", "with a picture",
)
_EMAIL_HOOK_KEYWORDS = (
    "teaser", "launch email", "campaign email", "email we sent",
    "email we just", "tease the email", "announce the email",
)

# Lookback windows per source — kept tight enough that stale assets
# don't leak into a fresh campaign, loose enough to cover typical
# "generate image, then post it" cadences.
_MEDIA_LOOKBACK_MIN = 30
_BLOG_LOOKBACK_MIN = 180
_EMAIL_LOOKBACK_MIN = 120


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
        """Override run to pull related teammate outputs before generating.

        Cross-agent sources (in priority order, concatenated into the
        `source_content` the agent sees):
        - Content Writer's latest blog post  (if task says "adapt / promote / blog")
        - Email Marketer's latest email hook (if task says "teaser / launch email")
        The task description always wins — these are context, not prompts.

        Also pulls the most recent Media Agent image (if task mentions
        "image / photo / banner") and threads it through to the parsed
        posts so publishers and inbox previews can attach it.
        """
        action = (context or {}).get(self.CONTEXT_KEY, self.DEFAULT_CONTEXT)
        action_lower = (action or "").lower()
        context = dict(context or {})

        source_chunks: list[str] = []
        if any(kw in action_lower for kw in _CONTENT_KEYWORDS):
            blog_chunk = _blog_source_chunk(tenant_id)
            if blog_chunk:
                source_chunks.append(blog_chunk)
        if any(kw in action_lower for kw in _EMAIL_HOOK_KEYWORDS):
            email_chunk = _email_hook_source_chunk(tenant_id)
            if email_chunk:
                source_chunks.append(email_chunk)
        if source_chunks:
            context["source_content"] = "\n\n---\n\n".join(source_chunks)
            logger.info(
                "[social_manager] cross-agent sources injected for %s: %d chunks (%d chars)",
                tenant_id, len(source_chunks), sum(len(c) for c in source_chunks),
            )

        # Image attach — pulled separately so the agent's TEXT prompt
        # stays clean and the image rides alongside the post metadata.
        attached_image_url: str | None = None
        if any(kw in action_lower for kw in _IMAGE_KEYWORDS) or _has_explicit_url(action):
            from backend.services.asset_lookup import get_latest_image_url
            attached_image_url = _extract_inline_url(action) or get_latest_image_url(
                tenant_id, within_minutes=_MEDIA_LOOKBACK_MIN,
            )
            if attached_image_url:
                logger.info(
                    "[social_manager] attaching Media image to posts for %s: %s",
                    tenant_id, attached_image_url,
                )

        result = await super().run(tenant_id, context)

        raw = result.get("result", "")
        posts = _parse_posts(raw)
        if posts:
            if attached_image_url:
                # Every post in the bundle gets the same image — simplest
                # UX. Callers can strip it per-platform later if needed.
                for p in posts:
                    p["image_url"] = attached_image_url
            result["social_posts"] = posts
        if attached_image_url:
            result["image_url"] = attached_image_url

        return result


_URL_RE = re.compile(r"https?://\S+?\.(?:png|jpg|jpeg|gif|webp|svg)(?:\?\S*)?", re.IGNORECASE)


def _has_explicit_url(text: str) -> bool:
    return bool(text) and bool(_URL_RE.search(text))


def _extract_inline_url(text: str) -> str | None:
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0) if m else None


def _blog_source_chunk(tenant_id: str) -> str:
    """Latest Content Writer blog/article, shaped for the social agent's
    source-content block."""
    from backend.services.asset_lookup import get_recent_blog_post
    row = get_recent_blog_post(tenant_id, within_minutes=_BLOG_LOOKBACK_MIN)
    if not row:
        return ""
    title = row.get("title", "")
    content = (row.get("content") or "")[:2000]
    return f"[SOURCE: Content Writer blog post]\nTitle: {title}\n\n{content}"


def _email_hook_source_chunk(tenant_id: str) -> str:
    """Latest Email Marketer draft's subject + preview — used when the
    task asks for a teaser / launch announcement post."""
    from backend.services.asset_lookup import get_recent_email_hook
    hook = get_recent_email_hook(tenant_id, within_minutes=_EMAIL_LOOKBACK_MIN)
    if not hook:
        return ""
    return (
        "[SOURCE: Email Marketer draft to tease]\n"
        f"Subject: {hook['subject']}\n"
        f"Preview: {hook['preview_snippet']}"
    )


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
