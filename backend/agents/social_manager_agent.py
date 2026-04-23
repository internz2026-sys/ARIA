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
_MEDIA_LOOKBACK_MIN = 360   # 6h (was 30m — cliff was too tight for cross-session composition)
_BLOG_LOOKBACK_MIN = 1440   # 24h
_EMAIL_LOOKBACK_MIN = 720   # 12h


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
    {{"platform": "twitter", "text": "<tweet body only>", "hashtags": ["...", "..."], "image_url": "<optional URL>"}},
    {{"platform": "linkedin", "text": "<linkedin body only>", "hashtags": ["...", "..."], "image_url": "<optional URL>"}}
  ]
}}

Rules:
- Exactly 2 posts: one for Twitter, one for LinkedIn.
- Twitter: max 280 chars including hashtags. Punchy, conversational, native-feeling. 2-3 hashtags max.
- LinkedIn: max 3000 chars. Professional, insightful, thought-leadership tone. Can be longer and more detailed than the tweet. 3-5 hashtags.
- Make both compelling and shareable.
- Each post should feel native to its platform.

STRICT body isolation — NEVER put any of these inside `text`:
- Section headers like `## X (Twitter) Post`, `## LinkedIn Post`, `**~264 characters**`, `**[Attach image: ...]**`
- Character-count descriptors like `X post (268 chars):`, `LinkedIn post (2,145 chars):`
- Delivery summaries: `Deliverables:`, `Status:`, `Social posts delivered`, `Created and posted ...`, `Image embedded: ...`
- Inbox item IDs: `(item abc-123-...)`
- Raw Supabase URLs (`https://*.supabase.co/...`) — those go ONLY in `image_url`.

`text` is the LIVE post body; if it shouldn't appear on X or LinkedIn, it must not appear in `text`. The backend strips leaked noise as a safety net, but relying on the sanitizer is a failure — write clean posts."""

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
        # Tier 1: explicit inline URL in the task text.
        # Tier 2: get_latest_image_url (time-windowed).
        # Tier 3: find_referenced_asset when the task has anaphora
        #   ("the banner", "my latest image", "from earlier") — catches
        #   cross-session references the tight window misses.
        attached_image_url: str | None = None
        from backend.services.asset_lookup import (
            get_latest_image_url, find_referenced_asset,
            extract_image_url_from_row, task_has_reference,
        )
        wants_image = (
            any(kw in action_lower for kw in _IMAGE_KEYWORDS)
            or _has_explicit_url(action)
            or task_has_reference(action)
        )
        if wants_image:
            attached_image_url = _extract_inline_url(action) or get_latest_image_url(
                tenant_id, within_minutes=_MEDIA_LOOKBACK_MIN,
            )
            if not attached_image_url and task_has_reference(action):
                for row in find_referenced_asset(
                    tenant_id, text_hint=action, agent="media",
                    types=["image"], limit=3,
                ):
                    u = extract_image_url_from_row(row)
                    if u:
                        attached_image_url = u
                        break
            if attached_image_url:
                logger.info(
                    "[social_manager] attaching Media image to posts for %s: %s",
                    tenant_id, attached_image_url,
                )

        result = await super().run(tenant_id, context)

        raw = result.get("result", "")
        posts = _parse_posts(raw)
        if posts:
            # Last-ditch image rescue: if no explicit image was attached
            # but a post text contains a Supabase URL, promote it to
            # image_url so the card renders correctly and the body
            # sanitizer can strip the URL from the visible copy.
            if not attached_image_url:
                for p in posts:
                    leaked = _extract_supabase_url(p.get("text", ""))
                    if leaked:
                        attached_image_url = leaked
                        break
            if attached_image_url:
                for p in posts:
                    p["image_url"] = attached_image_url
            result["social_posts"] = posts
            # ALWAYS re-serialize to canonical JSON so frontend parsing
            # works regardless of whether the agent returned JSON or
            # markdown. The inbox watcher saves result["result"] to the
            # `content` column verbatim, and the frontend's
            # parseSocialPosts reads posts back out of that string; if
            # we left the raw markdown in place the publish cards never
            # render and the image_url doesn't survive persistence.
            result["result"] = json.dumps({
                "action": "adapt_content",
                "posts": posts,
            })
        else:
            # Degraded path — the agent wrote a summary instead of
            # parseable posts. Sanitize the raw output so the
            # "what the agent wrote instead" panel doesn't leak
            # status/deliverables/item-id/Supabase URL noise to the user.
            result["result"] = _sanitize_social_text(raw) or raw
        if attached_image_url:
            result["image_url"] = attached_image_url

        return result


_URL_RE = re.compile(r"https?://\S+?\.(?:png|jpg|jpeg|gif|webp|svg)(?:\?\S*)?", re.IGNORECASE)

# Supabase storage URL — explicit match so the sanitizer can move these
# out of post text even when the extension is missing or query-stripped.
_SUPABASE_URL_RE = re.compile(r"https?://[^\s)]*\.supabase\.co/[^\s)]+", re.IGNORECASE)

# Lines we never want the user to see in a social post body. These are
# internal-plumbing phrases the Paperclip agent sometimes leaks into
# its reply when it writes a "delivery summary" instead of the post
# text the system prompt asked for.
_NOISE_LINE_PATTERNS = [
    re.compile(r"^\s*##?\s*(x\s*\(twitter\)|twitter|x)\s*post\s*$", re.IGNORECASE),
    re.compile(r"^\s*##?\s*linkedin\s*post\s*$", re.IGNORECASE),
    re.compile(r"^\s*\*\*\s*~?\d+\s*characters?\s*\*\*\s*$", re.IGNORECASE),
    re.compile(r"^\s*\*\*?\s*\[?attach\s+image:?.*\]?\*?\*?\s*$", re.IGNORECASE),
    re.compile(r"^\s*deliverables?\s*:?\s*$", re.IGNORECASE),
    re.compile(r"^\s*status\s*:.*$", re.IGNORECASE),
    re.compile(r"^\s*image\s+embedded\s*:.*$", re.IGNORECASE),
    re.compile(r"^\s*social\s+posts\s+delivered\s*$", re.IGNORECASE),
    re.compile(r"^\s*created\s+and\s+posted\b.*$", re.IGNORECASE),
    re.compile(r"^\s*[-*]\s*x\s*(?:\s*\(?twitter\)?)?\s*post\s*\(\s*[\d,]+\s*chars?\s*\).*$", re.IGNORECASE),
    re.compile(r"^\s*[-*]\s*linkedin\s*post\s*\(\s*[\d,]+\s*chars?\s*\).*$", re.IGNORECASE),
    re.compile(r"^\s*[-*]\s*image\s+embedded\s*:.*$", re.IGNORECASE),
]

# Inline fragments to strip from any line we do keep.
_INLINE_ITEM_REF_RE = re.compile(r"\s*\(\s*item\s+[0-9a-f-]{16,}\s*\)", re.IGNORECASE)


def _sanitize_social_text(text: str) -> str:
    """Strip internal-plumbing lines from a social post body.

    Covers three failure modes we've seen in production:
      1. Agent writes a "delivery summary" (Deliverables / Status /
         Image embedded / item <uuid>) instead of the post itself
      2. Agent leaks char-count descriptors like "X post (268 chars):"
         as if they were bullets in the final copy
      3. Agent pastes the raw Supabase storage URL into the post text

    Returns the cleaned text. Raw Supabase URLs are removed from body
    text (they belong in image_url, not in the visible copy). Empty
    lines are collapsed so the stripped output doesn't leave gaps.
    """
    if not text:
        return text
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        if any(pat.match(line) for pat in _NOISE_LINE_PATTERNS):
            continue
        stripped = line
        stripped = _INLINE_ITEM_REF_RE.sub("", stripped)
        stripped = _SUPABASE_URL_RE.sub("", stripped).rstrip()
        # If the ONLY content on this line was a supabase URL / item ref,
        # drop the line entirely.
        if not stripped.strip():
            if line.strip():
                continue
        cleaned_lines.append(stripped)
    # Collapse runs of blank lines introduced by the stripping.
    collapsed: list[str] = []
    blank_run = 0
    for line in cleaned_lines:
        if line.strip():
            blank_run = 0
            collapsed.append(line)
        else:
            blank_run += 1
            if blank_run <= 1:
                collapsed.append(line)
    return "\n".join(collapsed).strip()


def _extract_supabase_url(text: str) -> str | None:
    """Pull the first Supabase storage URL out of arbitrary text.

    Used as a last-resort source for the image when the agent leaked
    the URL into its body instead of putting it in image_url.
    """
    if not text:
        return None
    m = _SUPABASE_URL_RE.search(text)
    return m.group(0) if m else None


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
    """Extract posts array from agent output.

    Tries, in order:
      1. Direct JSON parse (the system prompt's preferred format)
      2. JSON array parse
      3. Markdown fallback — handles the ``**X (Twitter)** — 256 chars:``
         / ``**LinkedIn** — ...`` format the Paperclip-skilled agent
         sometimes emits instead of JSON. Without this fallback, the
         frontend's publish buttons never render, the pipeline-image
         hook never fires, and the user sees raw markdown in the
         inbox instead of platform cards.

    Every returned post has its `text` field run through the noise
    sanitizer — strips Deliverables:/Status:/item-id/Supabase URL leaks
    the agent sometimes pastes into the body.
    """
    parsed: list[dict] = []
    try:
        # Try direct JSON parse
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
            posts = data.get("posts", [])
            if posts:
                parsed = posts
    except (json.JSONDecodeError, KeyError):
        pass

    if not parsed:
        # Try finding JSON array
        try:
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start >= 0 and end > start:
                maybe = json.loads(raw[start:end])
                if isinstance(maybe, list) and maybe:
                    parsed = maybe
        except (json.JSONDecodeError, KeyError):
            pass

    if not parsed:
        # Markdown fallback
        parsed = _parse_posts_from_markdown(raw) or []

    for p in parsed:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            p["text"] = _sanitize_social_text(p["text"])
    return parsed


# Platform-header detector for the markdown fallback.
# Matches things like:
#   **X (Twitter)** — 256 chars:
#   **Twitter**: ...
#   **LinkedIn** — Full post covering pain points...
#   **X:**
# Captures the platform label and everything until the next platform
# header or end of string.
_PLATFORM_HEADER_RE = re.compile(
    r"\*\*\s*(x(?:\s*\(twitter\))?|twitter|linkedin|facebook)\s*\*\*"
    r"\s*(?:[—\-:]\s*)?(.*?)"
    r"(?=(?:\n\s*\*\*\s*(?:x(?:\s*\(twitter\))?|twitter|linkedin|facebook)\s*\*\*)|$)",
    re.IGNORECASE | re.DOTALL,
)
_HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")


def _normalize_platform(label: str) -> str:
    lab = label.lower().strip()
    if "linkedin" in lab:
        return "linkedin"
    if "facebook" in lab:
        return "facebook"
    return "twitter"  # x, x (twitter), twitter all collapse to twitter


def _parse_posts_from_markdown(raw: str) -> list[dict]:
    """Pull twitter/linkedin/facebook posts out of a markdown reply.

    Strategy: find each **Platform** header, take the body until the
    next header, strip the leading "— 256 chars:" metadata line,
    extract the post text (prefer a ``> blockquote`` when present,
    fall back to the whole section), collect hashtags into a list.
    """
    if not raw:
        return []
    posts: list[dict] = []
    seen_platforms: set[str] = set()
    for m in _PLATFORM_HEADER_RE.finditer(raw):
        platform = _normalize_platform(m.group(1))
        if platform in seen_platforms:
            continue
        seen_platforms.add(platform)
        body = (m.group(2) or "").strip()
        if not body:
            continue

        # Drop leading char-count / descriptor line like "256 chars:" or
        # "Full thought-leadership post covering pain points..." when a
        # blockquote follows. We want the ACTUAL post text.
        lines = body.splitlines()
        quoted_lines: list[str] = []
        non_quoted_lines: list[str] = []
        for line in lines:
            s = line.strip()
            if s.startswith(">"):
                quoted_lines.append(s.lstrip(">").strip())
            elif s:
                non_quoted_lines.append(s)

        if quoted_lines:
            text = "\n".join(quoted_lines).strip()
        else:
            # No blockquote. Drop a metadata-style FIRST line (e.g.
            # "256 chars:", "Full thought-leadership post — ...") only
            # when there's actual post content after it. If the
            # descriptor IS the entire body, keep it — the agent wrote
            # a summary instead of a real post and the user should at
            # least see what landed so they can regenerate.
            looks_like_descriptor = non_quoted_lines and (
                non_quoted_lines[0].endswith(":")
                or " chars" in non_quoted_lines[0].lower()
                or non_quoted_lines[0].lower().startswith(("full ", "long ", "short "))
            )
            if looks_like_descriptor and len(non_quoted_lines) > 1:
                non_quoted_lines = non_quoted_lines[1:]
            text = "\n".join(non_quoted_lines).strip()

        if not text:
            continue

        # Collect hashtags and strip them from the text when they appear
        # as a trailing "Hashtags:" line. Inline hashtags stay inside
        # the post text (Twitter norm).
        hashtags: list[str] = []
        hashtag_line_match = re.search(
            r"^\s*hashtags?\s*:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE,
        )
        if hashtag_line_match:
            hashtags = _HASHTAG_RE.findall(hashtag_line_match.group(1))
            text = re.sub(
                r"^\s*hashtags?\s*:.*$", "", text, flags=re.IGNORECASE | re.MULTILINE,
            ).strip()
        else:
            hashtags = _HASHTAG_RE.findall(text)

        # De-dupe hashtags preserving first-seen order
        seen_tags: set[str] = set()
        dedup_tags: list[str] = []
        for tag in hashtags:
            low = tag.lower()
            if low not in seen_tags:
                seen_tags.add(low)
                dedup_tags.append(tag)

        if text:
            posts.append({"platform": platform, "text": text, "hashtags": dedup_tags})

    return posts


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
