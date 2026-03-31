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
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from backend.config.loader import get_tenant_config
from backend.tools.claude_cli import MODEL_SONNET, MODEL_HAIKU

logger = logging.getLogger("aria.agents")

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

        # Fallback: reconstruct from individual fields
        all_lines = {
            "business": f"Business: {config.business_name}",
            "product": f"Product: {config.product.name} — {config.product.description}",
            "value_props": f"Value props: {', '.join(config.product.value_props)}",
            "competitors": f"Competitors: {', '.join(config.product.competitors)}",
            "differentiators": f"Differentiators: {', '.join(config.product.differentiators)}",
            "audience": f"Target audience: {', '.join(config.icp.target_titles)}",
            "pain_points": f"Pain points: {', '.join(config.icp.pain_points)}",
            "hangouts": f"Where they hang out: {', '.join(config.icp.online_hangouts)}",
            "voice": f"Brand voice: {config.brand_voice.tone}",
        }
        if fields is None:
            return "\n".join(all_lines.values())
        return "\n".join(v for k, v in all_lines.items() if k in fields)

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
