"""Email Marketer Agent — produces email campaigns and sends via Gmail."""
from __future__ import annotations

import logging
import re

from backend.agents.base import BaseAgent, MODEL_HAIKU

logger = logging.getLogger("aria.email_marketer")

_agent = None

# Regex to find email addresses in task descriptions
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Image URL extractor — matches http(s) URLs ending in a common image ext,
# or markdown image syntax ![alt](url). Used both when the CEO pastes a URL
# straight into the task ("with image: https://.../hero.png") and when
# pulling a URL out of a recent media-agent inbox row.
_IMG_URL_RE = re.compile(
    r"https?://[^\s<>\"')]+?\.(?:png|jpg|jpeg|gif|webp|svg)(?:\?[^\s<>\"')]*)?",
    re.IGNORECASE,
)
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")

# Keyword triggers that say "the user wants an image in this email".
# Matched case-insensitively against the task description. Phrases were
# picked from the CEO's most common delegation wording — "include an
# image of X", "with a banner showing Y", etc.
_IMAGE_INTENT_RE = re.compile(
    r"\b(image|images|photo|photos|picture|pictures|banner|hero image|"
    r"visual|visuals|graphic|graphics|illustration|illustrations|"
    r"thumbnail|screenshot|screenshots)\b",
    re.IGNORECASE,
)

# Task phrases that say "digest / tease / repurpose the latest blog post
# from the Content Writer into an email". When matched, we pull the most
# recent content_writer inbox row and feed its title + excerpt to the
# agent as source material.
_BLOG_DIGEST_RE = re.compile(
    r"\b(newsletter.*(blog|post|article)|digest|recap|repurpose.*blog|"
    r"tease.*blog|email.*(blog|post|article)|blog.*email|"
    r"latest (blog|post|article))\b",
    re.IGNORECASE,
)

# How far back to look for teammate outputs. Image lookback is tight
# because stale media assets get cross-campaign-leaky fast; blog
# lookback is looser because a Content Writer post is a deliberate
# artifact that stays relevant for longer.
_MEDIA_LOOKBACK_MINUTES = 360   # 6h — user may return after lunch / next morning
_BLOG_LOOKBACK_MINUTES = 1440   # 24h — blog posts stay relevant a day


class EmailMarketerAgent(BaseAgent):
    AGENT_NAME = "email_marketer"
    CONTEXT_KEY = "action"  # tasks come in via context={"action": "..."}
    DEFAULT_CONTEXT = "draft a newsletter"
    MAX_TOKENS = 2000
    MODEL = MODEL_HAIKU
    CONTEXT_FIELDS = {"business", "audience", "pain_points", "voice"}

    def build_system_prompt(self, config, action: str) -> str:
        # Detect if this is a send task with a recipient
        recipient = _extract_recipient(action)
        send_note = ""
        if recipient:
            send_note = f"""

SENDING to {recipient} from {config.owner_email}. Write ready-to-send email.

Format: SUBJECT: <subject>\n---\n<full HTML email body>

HTML rules: complete <html><body> document, ALL styles inline, table-based layout, styled CTA buttons, no CSS classes/style blocks."""

        return f"""You are the Email Marketer for {config.business_name}.

{self.business_context(config, self.CONTEXT_FIELDS)}
Positioning: {config.gtm_playbook.positioning}

Types: welcome_sequence, newsletter, launch_sequence, re_engagement, product_update.
Per email: subject line (2 A/B variants), preview text, HTML body, send time.
Keep concise, value-driven, one CTA per email.
{send_note}"""

    def build_user_message(self, action: str, context: dict | None) -> str:
        """Prepend any cross-agent source material so the model sees it
        alongside the user's task. Kept in the user message (not the
        system prompt) because it's per-task context, not agent identity.
        """
        source = (context or {}).get("source_content", "")
        base = action or "Draft a newsletter campaign."
        if source:
            return f"{source}\n\n---\n\n{base}"
        return base


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


def _extract_image_urls_from_text(text: str) -> list[str]:
    """Pull image URLs the CEO (or user) pasted directly into the task.

    Accepts both raw URLs and markdown image syntax. De-duplicated in
    first-seen order.
    """
    if not text:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for m in _MD_IMG_RE.finditer(text):
        u = m.group(1)
        if u not in seen:
            urls.append(u)
            seen.add(u)
    for m in _IMG_URL_RE.finditer(text):
        u = m.group(0)
        if u not in seen:
            urls.append(u)
            seen.add(u)
    return urls


def _find_recent_media_image(tenant_id: str, *, task_hint: str = "") -> str | None:
    """Image lookup with a two-tier fallback.

    1. `get_latest_image_url` with the primary window — fast path for
       "just made it, use it now" cases.
    2. `find_referenced_asset` (wider lookback + ILIKE + optional
       semantic match) when the task text has anaphoric phrases
       ("the banner", "my latest", "from earlier") and tier 1 missed.
    """
    from backend.services.asset_lookup import (
        get_latest_image_url, find_referenced_asset, extract_image_url_from_row,
        task_has_reference,
    )
    url = get_latest_image_url(tenant_id, within_minutes=_MEDIA_LOOKBACK_MINUTES)
    if url:
        return url
    if task_hint and task_has_reference(task_hint):
        rows = find_referenced_asset(
            tenant_id, text_hint=task_hint, agent="media", types=["image"], limit=3,
        )
        for row in rows:
            u = extract_image_url_from_row(row)
            if u:
                return u
    return None


def _find_recent_blog_source(tenant_id: str) -> str:
    """Return a source-content block built from the latest Content
    Writer output, shaped for the email agent's user message. Empty
    string on miss — callers concatenate and move on.
    """
    from backend.services.asset_lookup import get_recent_blog_post
    row = get_recent_blog_post(tenant_id, within_minutes=_BLOG_LOOKBACK_MINUTES)
    if not row:
        return ""
    title = row.get("title", "")
    body = (row.get("content") or "")[:2500]
    return (
        "[SOURCE: Content Writer blog post — digest this into an email]\n"
        f"Title: {title}\n\n{body}"
    )


def _render_email_image_block(url: str) -> str:
    """Render a responsive <img> block to prepend to the email body.

    Inline styles only — Gmail strips <style> tags, and many clients
    ignore or rewrite classes. Max-width 560 matches the 600-px container
    we wrap everything in so the image fills without overflowing.
    """
    safe_url = url.replace('"', "%22")
    return (
        '<div style="text-align:center; margin: 0 0 20px 0;">'
        f'<img src="{safe_url}" alt="" '
        'style="display:block; max-width:100%; width:100%; height:auto; '
        'border-radius:8px; margin:0 auto;" />'
        "</div>\n"
    )


def _inject_image_into_html(html_body: str, url: str) -> str:
    """Place the image block after the opening <body>/<div> if present,
    otherwise prepend it. Keeps the branded header styling intact when
    the agent already returned a full HTML document."""
    block = _render_email_image_block(url)
    # Try to inject right after <body ...> so it lands ABOVE the content
    # but INSIDE the branded container <div> that _wrap_html creates.
    m = re.search(r"<body[^>]*>\s*(<div[^>]*>)", html_body, re.IGNORECASE)
    if m:
        return html_body[: m.end()] + "\n" + block + html_body[m.end():]
    m = re.search(r"<body[^>]*>", html_body, re.IGNORECASE)
    if m:
        return html_body[: m.end()] + "\n" + block + html_body[m.end():]
    return block + html_body


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
    """Send a batch of email dicts. Returns one result per send.

    Name kept for backward-compat with existing callers, but the
    transport is now provider-agnostic — actual channel (Resend /
    Gmail) is decided by `email_provider.send_email` based on env +
    tenant config.

    Each result dict contains: {to, subject, message_id, provider, error?}
    """
    from backend.services import email_provider

    results = []
    for email in emails:
        send_result = await email_provider.send_email(
            tenant_id,
            to=email["to"],
            subject=email["subject"],
            html_body=email["html_body"],
            text_body=email.get("text_body", ""),
            inbox_item_id=email.get("inbox_item_id", ""),
            inbound_thread_id=email.get("thread_id", ""),
        )
        adapted: dict = {
            "to": email["to"],
            "subject": email["subject"],
            "provider": send_result.get("provider", ""),
        }
        if send_result.get("success"):
            adapted["message_id"] = send_result.get("message_id") or ""
        else:
            adapted["error"] = send_result.get("error") or "send_failed"
        results.append(adapted)
        logger.info(
            "[email_marketer] send to=%s subject=%s provider=%s result=%s",
            email["to"], email["subject"], adapted["provider"],
            "ok" if send_result.get("success") else adapted.get("error"),
        )
    return results


async def run(tenant_id: str, context: dict | None = None) -> dict:
    # Cross-agent context injection: if the task describes a newsletter /
    # digest / email-about-our-blog, pull the Content Writer's most
    # recent output and pipe it in as source_content. The class's
    # build_user_message prepends it for the model to see.
    task_desc = (context or {}).get("action", "")
    if task_desc and _BLOG_DIGEST_RE.search(task_desc):
        blog_source = _find_recent_blog_source(tenant_id)
        if blog_source:
            context = dict(context or {})
            # Preserve any source_content the caller already set; just
            # append so both survive.
            existing = context.get("source_content", "")
            context["source_content"] = (
                f"{existing}\n\n{blog_source}" if existing else blog_source
            )
            logger.info(
                "[email_marketer] digesting Content Writer blog for tenant %s", tenant_id,
            )

    result = await _get().run(tenant_id, context)

    content = result.get("result", "")
    if not isinstance(content, str) or not content:
        return result

    # Extract recipient from the original task description
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

    # ── Cross-agent: pull an image from the Media Agent if appropriate ──
    # Priority:
    #   1. Any URL/markdown image pasted straight into the task by CEO
    #   2. The newest Media Agent image in this tenant's inbox (only if
    #      the task text actually asks for an image — otherwise we'd
    #      attach random prior assets to every email).
    image_urls_from_task = _extract_image_urls_from_text(task_desc)
    chosen_image_url: str | None = image_urls_from_task[0] if image_urls_from_task else None
    if not chosen_image_url and task_desc:
        from backend.services.asset_lookup import task_has_reference
        wants_image = bool(_IMAGE_INTENT_RE.search(task_desc)) or task_has_reference(task_desc)
        if wants_image:
            chosen_image_url = _find_recent_media_image(tenant_id, task_hint=task_desc)
        if chosen_image_url:
            logger.info(
                "[email_marketer] attached recent Media Agent image to email for tenant %s: %s",
                tenant_id, chosen_image_url,
            )
    if chosen_image_url:
        html_body = _inject_image_into_html(html_body, chosen_image_url)

    # Build a plain-text preview snippet (first 200 chars of body text)
    text_body = re.sub(r'<[^>]+>', '', body).strip()
    preview_snippet = text_body[:200] + ("..." if len(text_body) > 200 else "")

    # Always return a structured draft — NEVER auto-send.
    # The user must explicitly approve before any email is sent.
    draft: dict = {
        "to": recipient or "",
        "subject": subject,
        "html_body": html_body,
        "text_body": text_body,
        "preview_snippet": preview_snippet,
        "status": "draft_pending_approval",
    }
    if chosen_image_url:
        # Surface it in the draft payload so the frontend editor can show
        # the attached image separately (and future code can re-render
        # the email without re-scanning the HTML).
        draft["image_urls"] = [chosen_image_url]
    result["email_draft"] = draft

    return result
