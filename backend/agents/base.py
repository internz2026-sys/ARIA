"""Base Agent — shared run/prompt logic for all ARIA marketing agents.

Subclass agents only need to define:
  AGENT_NAME  — slug identifier
  CONTEXT_KEY — which context dict key to read (e.g. "type", "action")
  DEFAULT_CONTEXT — fallback value when key is missing
  build_system_prompt(config, context_value) — returns the system prompt string
  build_user_message(context_value, context) — returns the user message string

Optional overrides for token optimization:
  MODEL          — which Claude model to use (default: Sonnet)
  MAX_TOKENS     — max response tokens (default: 4000)
  CONTEXT_FIELDS — set of business_context fields to include (default: all)

Skills:
  Each agent's docs/agents/skills/{AGENT_NAME}_skills.md file is automatically
  appended to the system prompt as a reference appendix at run time. This is
  the primary way to give an agent domain knowledge (subject-line formulas,
  prompt templates, channel-specific rules, etc.) without touching Python.
  Edit the .md file and the change takes effect on the next run — no restart
  needed.
"""
from __future__ import annotations

import logging
import pathlib
from datetime import datetime, timezone

from backend.config.loader import get_tenant_config
from backend.tools.claude_cli import MODEL_SONNET, MODEL_HAIKU

logger = logging.getLogger("aria.agents")

# docs/agents/skills/ relative to repo root
_SKILLS_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "docs" / "agents" / "skills"


def _load_agent_skill(agent_name: str) -> str:
    """Read the agent's skill MD file from disk on every call.

    No cache — files are small (<10KB) and re-reading per invocation lets
    users edit a skill MD and see the change on the next agent run without
    restarting the backend. Returns an empty string if no skill file exists
    for this agent (so callers can safely concatenate the result).
    """
    if not agent_name:
        return ""
    path = _SKILLS_DIR / f"{agent_name}_skills.md"
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to load skill MD for %s: %s", agent_name, e)
    return ""

# All available business context fields
_ALL_CONTEXT_FIELDS = {
    "business", "product", "value_props", "competitors", "differentiators",
    "audience", "pain_points", "hangouts", "voice",
}


class BaseAgent:
    AGENT_NAME: str = ""
    CONTEXT_KEY: str = "action"
    DEFAULT_CONTEXT: str = ""

    # ── Token optimization — override in subclass ─────────────────────────
    MODEL: str = MODEL_SONNET          # default: Sonnet for quality
    MAX_TOKENS: int = 4000             # default: 4000
    CONTEXT_FIELDS: set[str] | None = None  # None = all fields

    # ── Shared business context block ──────────────────────────────────────

    @staticmethod
    def business_context(config, fields: set[str] | None = None) -> str:
        """Return the condensed agent brief if available, else reconstruct.

        The agent_brief is a ~150 token paragraph generated once after
        onboarding. It replaces the ~800 token field-by-field reconstruction.
        Falls back to field reconstruction for tenants without a brief
        (older accounts or when brief generation failed).

        Args:
            config: TenantConfig object
            fields: Optional set of field keys to include (only used in fallback).
                    Valid keys: business, product, value_props, competitors,
                    differentiators, audience, pain_points, hangouts, voice
        """
        # Prefer the pre-generated brief (massive token savings)
        if config.agent_brief:
            return config.agent_brief

        # Fallback: reconstruct from individual fields (skip empty ones to save tokens)
        all_lines = {
            "business": (f"Business: {config.business_name}", bool(config.business_name)),
            "product": (f"Product: {config.product.name} — {config.product.description}", bool(config.product.name)),
            "value_props": (f"Value props: {', '.join(config.product.value_props)}", bool(config.product.value_props)),
            "competitors": (f"Competitors: {', '.join(config.product.competitors)}", bool(config.product.competitors)),
            "differentiators": (f"Differentiators: {', '.join(config.product.differentiators)}", bool(config.product.differentiators)),
            "audience": (f"Target audience: {', '.join(config.icp.target_titles)}", bool(config.icp.target_titles)),
            "pain_points": (f"Pain points: {', '.join(config.icp.pain_points)}", bool(config.icp.pain_points)),
            "hangouts": (f"Where they hang out: {', '.join(config.icp.online_hangouts)}", bool(config.icp.online_hangouts)),
            "voice": (f"Brand voice: {config.brand_voice.tone}", bool(config.brand_voice.tone)),
        }
        if fields is None:
            return "\n".join(text for text, has_data in all_lines.values() if has_data)
        return "\n".join(text for k, (text, has_data) in all_lines.items() if k in fields and has_data)

    @staticmethod
    def gtm_context(config) -> str:
        """Reusable GTM playbook context block."""
        return (
            f"Positioning: {config.gtm_playbook.positioning}\n"
            f"Messaging pillars: {', '.join(config.gtm_playbook.messaging_pillars)}\n"
            f"Content themes: {', '.join(config.gtm_playbook.content_themes)}\n"
            f"Channel strategy: {', '.join(config.gtm_playbook.channel_strategy)}"
        )

    # ── Override in subclass ───────────────────────────────────────────────

    def build_system_prompt(self, config, context_value: str) -> str:
        raise NotImplementedError

    def build_user_message(self, context_value: str, context: dict | None) -> str:
        return f"Action: {context_value}. Context: {context or 'No additional context'}"

    # ── Shared run logic ──────────────────────────────────────────────────

    async def run(self, tenant_id: str, context: dict | None = None) -> dict:
        config = get_tenant_config(tenant_id)
        context_value = (context or {}).get(self.CONTEXT_KEY, self.DEFAULT_CONTEXT)

        system_prompt = self.build_system_prompt(config, context_value)
        user_message = self.build_user_message(context_value, context)

        # Append the agent's skill MD as a reference appendix. This is what
        # gives each agent its domain knowledge (subject-line formulas, image
        # prompt templates, deliverability rules, etc.) without baking it
        # into Python. Edit docs/agents/skills/{agent_name}_skills.md to
        # change behavior — takes effect on the next run.
        skill_md = _load_agent_skill(self.AGENT_NAME)
        if skill_md:
            system_prompt = (
                f"{system_prompt}\n\n"
                f"--- {self.AGENT_NAME.upper()} SKILLS REFERENCE (consult these before responding) ---\n"
                f"{skill_md}"
            )
            logger.info("[%s] Loaded skill MD (%d chars)", self.AGENT_NAME, len(skill_md))

        # Analytics + learning feedback layers, all best-effort and
        # silently no-op when the tenant has no history or when the
        # relevant table/column hasn't been migrated yet.
        #
        #   1. Top performers — recent approved/sent/published outputs
        #      to emulate the structure of.
        #   2. Style memory — diffs of drafts the user has edited, so
        #      the model learns the tenant's preferred voice.
        #   3. Cancellation reasons — "don't do this again" signals the
        #      user left when rejecting a prior draft.
        try:
            from backend.services.asset_lookup import (
                summarize_top_performers_for_prompt,
                summarize_style_memory_for_prompt,
                summarize_cancel_reasons_for_prompt,
            )
            perf_block = summarize_top_performers_for_prompt(
                tenant_id, agent=self.AGENT_NAME, limit=3,
            )
            if perf_block:
                system_prompt = f"{system_prompt}\n\n{perf_block}"
            style_block = summarize_style_memory_for_prompt(
                tenant_id, agent=self.AGENT_NAME, limit=3,
            )
            if style_block:
                system_prompt = f"{system_prompt}\n\n{style_block}"
            cancel_block = summarize_cancel_reasons_for_prompt(
                tenant_id, agent=self.AGENT_NAME, limit=3,
            )
            if cancel_block:
                system_prompt = f"{system_prompt}\n\n{cancel_block}"
        except Exception as e:
            logger.debug("[%s] feedback prompt inject skipped: %s", self.AGENT_NAME, e)

        # Content library recall: scan the tenant's archive for older
        # outputs by this same agent whose title overlaps the task. Lets
        # the model adapt a prior asset instead of cold-generating a
        # near-duplicate. Uses a cheap ILIKE on a small sample of task
        # tokens — Qdrant-backed semantic search would be nicer but
        # would add a service dependency for marginal gain at small
        # tenant scales.
        try:
            from backend.services.asset_lookup import get_related_content

            # Turn the task description into ~3 content keywords for the
            # ILIKE. Skip common / short stopwords — otherwise every task
            # matches on "the" and we flood the prompt.
            stop = {"the", "and", "for", "with", "from", "into", "this", "that",
                    "your", "our", "write", "create", "draft", "make", "send",
                    "post", "tweet", "email", "about", "using", "tell", "them"}
            words = [w.strip(".,!?:;\"'").lower() for w in (context_value or "").split()]
            keywords = [w for w in words if len(w) >= 5 and w not in stop][:3]
            rows: list = []
            for kw in keywords:
                rows.extend(get_related_content(tenant_id, topic_query=kw, limit=2))
                if len(rows) >= 3:
                    break
            # De-dup by id
            seen: set = set()
            uniq = []
            for r in rows:
                rid = r.get("id")
                if rid and rid not in seen:
                    seen.add(rid)
                    uniq.append(r)
                if len(uniq) >= 3:
                    break
            if uniq:
                lines = ["## Related prior work (adapt before regenerating)"]
                for r in uniq:
                    title = (r.get("title") or "")[:80]
                    body_preview = (r.get("body") or "")[:160].replace("\n", " ")
                    lines.append(f"- {title} — {body_preview}")
                system_prompt = f"{system_prompt}\n\n" + "\n".join(lines)
        except Exception as e:
            logger.debug("[%s] content_library recall skipped: %s", self.AGENT_NAME, e)

        from backend.tools.claude_cli import call_claude  # lazy to avoid circular __init__

        logger.info("[%s] Running for tenant %s (model=%s, max_tokens=%d, context=%s)",
                     self.AGENT_NAME, tenant_id, self.MODEL, self.MAX_TOKENS, context_value)
        result = await call_claude(
            system_prompt,
            user_message,
            max_tokens=self.MAX_TOKENS,
            tenant_id=tenant_id,
            model=self.MODEL,
            agent_id=self.AGENT_NAME,
        )

        return {
            "agent": self.AGENT_NAME,
            "tenant_id": tenant_id,
            "status": "completed",
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
