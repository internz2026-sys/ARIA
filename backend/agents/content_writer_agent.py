"""Content Writer Agent — creates marketing content for developer founders."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from backend.agents.base import BaseAgent, MODEL_SONNET, MODEL_HAIKU

logger = logging.getLogger("aria.content_writer")

_agent = None

# Task phrases that mean "build an FAQ / help doc from our actual
# customer conversations". When matched, we scrape recent inbound email
# replies for question-shaped sentences and feed them as source material.
_FAQ_INTENT_RE = re.compile(
    r"\b(faq|frequently asked|common questions|customer questions|help center|"
    r"q&a|help doc|objection handling)\b",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(r"([A-Z][^.!?\n]{8,200}\?)")

# Task phrases that mean "write a case study / customer success story".
# Combines with the CRM enrichment already applied to content_writer
# delegations — when matched, we additionally pull the most recent
# closed-won deal so the copy can reference the actual deal details.
_CASE_STUDY_INTENT_RE = re.compile(
    r"\b(case study|success story|customer story|testimonial)\b",
    re.IGNORECASE,
)

_QUESTION_LOOKBACK_DAYS = 60
_DEAL_LOOKBACK_DAYS = 180
_MEDIA_LOOKBACK_MINUTES = 360   # 6h

# Task phrases that mean "the user wants an image embedded in this content".
_IMAGE_INTENT_RE = re.compile(
    r"\b(image|photo|picture|banner|hero|visual|graphic|illustration|"
    r"thumbnail|screenshot|logo)\b",
    re.IGNORECASE,
)


class ContentWriterAgent(BaseAgent):
    AGENT_NAME = "content_writer"
    CONTEXT_KEY = "type"
    DEFAULT_CONTEXT = "blog_post"
    CONTEXT_FIELDS = {"business", "product", "audience", "pain_points", "voice"}

    # Use Haiku for short-form, Sonnet for long-form
    _HAIKU_TYPES = {"landing_page", "product_hunt", "show_hn", "email_copy"}

    def build_system_prompt(self, config, content_type: str) -> str:
        # Dynamic model selection based on content type
        if content_type in self._HAIKU_TYPES:
            self.MODEL = MODEL_HAIKU
            self.MAX_TOKENS = 2000
        else:
            self.MODEL = MODEL_SONNET
            self.MAX_TOKENS = 3000

        return f"""You are the Content Writer for {config.business_name}.

{self.business_context(config, self.CONTEXT_FIELDS)}
Positioning: {config.gtm_playbook.positioning}

Create {content_type} content. Match brand voice, include one clear CTA, ready to copy-paste.
Return JSON: content_type, title, body, cta_text, word_count"""

    def build_user_message(self, content_type: str, context: dict | None) -> str:
        source = (context or {}).get("source_content", "")
        base = f"Create {content_type} content. Context: {context}"
        if source:
            return f"{source}\n\n---\n\n{base}"
        return base


def _get():
    global _agent
    if _agent is None:
        _agent = ContentWriterAgent()
    return _agent


AGENT_NAME = ContentWriterAgent.AGENT_NAME


def _collect_customer_questions(tenant_id: str) -> str:
    """Scrape question-shaped sentences out of recent inbound replies.

    The Conversations page already indexes inbound email into
    email_messages; we read that, pull anything shaped like a question
    ("Does it…?", "Can we…?"), dedupe, and return the top ~12 so the
    FAQ blog post has real customer language to answer.
    """
    try:
        from backend.services.supabase import get_db
        sb = get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_QUESTION_LOOKBACK_DAYS)).isoformat()
        rows = (
            sb.table("email_messages")
            .select("text_body, preview_snippet")
            .eq("tenant_id", tenant_id)
            .eq("direction", "inbound")
            .gte("message_timestamp", cutoff)
            .order("message_timestamp", desc=True)
            .limit(200)
            .execute()
        )
        if not rows.data:
            return ""
        questions: list[str] = []
        seen: set[str] = set()
        for r in rows.data:
            body = (r.get("text_body") or r.get("preview_snippet") or "").strip()
            if not body:
                continue
            for m in _QUESTION_RE.finditer(body):
                q = re.sub(r"\s+", " ", m.group(1).strip())
                key = q.lower()
                if key in seen:
                    continue
                seen.add(key)
                questions.append(q)
                if len(questions) >= 12:
                    break
            if len(questions) >= 12:
                break
        if not questions:
            return ""
        bulleted = "\n".join(f"- {q}" for q in questions)
        return (
            "[SOURCE: questions collected from recent customer email replies — "
            "answer each one in the FAQ]\n" + bulleted
        )
    except Exception as e:
        logger.warning("[content_writer] customer question scrape failed for %s: %s", tenant_id, e)
        return ""


def _collect_closed_won_for_case_study(tenant_id: str) -> str:
    """Pull the most recent closed-won deal + its contact + notes.

    Lets the content_writer frame a real customer's experience rather
    than inventing a generic testimonial. PII (emails, names) stays in
    the task text — the agent is instructed to anonymize in the final
    copy if the deal's privacy tag demands it.
    """
    try:
        from backend.services.supabase import get_db
        sb = get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_DEAL_LOOKBACK_DAYS)).isoformat()
        deals = (
            sb.table("crm_deals")
            .select("id, title, value, stage, contact_id, notes, updated_at")
            .eq("tenant_id", tenant_id)
            .in_("stage", ["closed_won", "won", "Closed Won"])
            .gte("updated_at", cutoff)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        if not deals.data:
            return ""
        deal = deals.data[0]
        contact_block = ""
        cid = deal.get("contact_id")
        if cid:
            c = (
                sb.table("crm_contacts")
                .select("name, email, company_id, status, notes")
                .eq("id", cid)
                .eq("tenant_id", tenant_id)
                .single()
                .execute()
            )
            if c.data:
                contact_block = (
                    f"Customer: {c.data.get('name', '')} <{c.data.get('email', '')}>"
                )
                if c.data.get("notes"):
                    contact_block += f"\nContact notes: {c.data['notes'][:500]}"
        lines = [
            "[SOURCE: most recent closed-won deal — frame the case study around this]",
            f"Deal: {deal.get('title', '')} — value: ${deal.get('value', 0)}",
        ]
        if contact_block:
            lines.append(contact_block)
        if deal.get("notes"):
            lines.append(f"Deal notes: {deal['notes'][:800]}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(
            "[content_writer] closed-won lookup failed for %s: %s", tenant_id, e,
        )
        return ""


async def run(tenant_id: str, context: dict | None = None) -> dict:
    """Cross-agent hook: if the task says FAQ, pull recent customer
    questions; if it says case study, pull the latest closed-won deal.
    Either / both are prepended to the user message as source_content
    before the underlying Content Writer runs.
    """
    task_desc = (context or {}).get("action", "") or (context or {}).get("type", "") or ""
    context = dict(context or {})

    source_chunks: list[str] = []
    if task_desc and _FAQ_INTENT_RE.search(task_desc):
        chunk = _collect_customer_questions(tenant_id)
        if chunk:
            source_chunks.append(chunk)
    if task_desc and _CASE_STUDY_INTENT_RE.search(task_desc):
        chunk = _collect_closed_won_for_case_study(tenant_id)
        if chunk:
            source_chunks.append(chunk)
    if source_chunks:
        existing = context.get("source_content", "")
        joined = "\n\n---\n\n".join(source_chunks)
        context["source_content"] = f"{existing}\n\n{joined}" if existing else joined
        logger.info(
            "[content_writer] cross-agent source injected for %s (%d chunks)",
            tenant_id, len(source_chunks),
        )

    # Cross-agent: pull the latest Media Agent image when the task hints
    # at needing one. Unlike the email/social agents which wrap or attach
    # the image themselves, content_writer's deliverable is text — so we
    # surface the URL as a suggested placement marker in the result body
    # ("Recommended hero image: [IMAGE: <URL>]") so the user can copy-
    # paste the content into a CMS with the image slot already flagged.
    attached_image_url: str | None = None
    if task_desc:
        from backend.services.asset_lookup import (
            get_latest_image_url, find_referenced_asset,
            extract_image_url_from_row, task_has_reference,
        )
        wants_image = bool(_IMAGE_INTENT_RE.search(task_desc)) or task_has_reference(task_desc)
        if wants_image:
            attached_image_url = get_latest_image_url(
                tenant_id, within_minutes=_MEDIA_LOOKBACK_MINUTES,
            )
            if not attached_image_url and task_has_reference(task_desc):
                for row in find_referenced_asset(
                    tenant_id, text_hint=task_desc, agent="media",
                    types=["image"], limit=3,
                ):
                    u = extract_image_url_from_row(row)
                    if u:
                        attached_image_url = u
                        break
            if attached_image_url:
                logger.info(
                    "[content_writer] attaching Media image reference for %s: %s",
                    tenant_id, attached_image_url,
                )

    result = await _get().run(tenant_id, context)

    if attached_image_url:
        body = result.get("result", "") or ""
        marker = (
            f"\n\n---\n"
            f"**Recommended hero placement:** [IMAGE: {attached_image_url}]\n"
            f"(This image was generated by the Media Designer in the current session. "
            f"Place it at the top of the article or above the first CTA.)\n"
        )
        result["result"] = f"{body}{marker}" if isinstance(body, str) else body
        result["image_url"] = attached_image_url

    return result
