"""Base Agent — shared run/prompt logic for all ARIA marketing agents.

Subclass agents only need to define:
  AGENT_NAME  — slug identifier
  CONTEXT_KEY — which context dict key to read (e.g. "type", "action")
  DEFAULT_CONTEXT — fallback value when key is missing
  build_system_prompt(config, context_value) — returns the system prompt string
  build_user_message(context_value, context) — returns the user message string
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from backend.config.loader import get_tenant_config

logger = logging.getLogger("aria.agents")


class BaseAgent:
    AGENT_NAME: str = ""
    CONTEXT_KEY: str = "action"
    DEFAULT_CONTEXT: str = ""

    # ── Shared business context block ──────────────────────────────────────

    @staticmethod
    def business_context(config) -> str:
        """Reusable business/product/audience context block for any agent."""
        return (
            f"Business: {config.business_name}\n"
            f"Product: {config.product.name} — {config.product.description}\n"
            f"Value props: {', '.join(config.product.value_props)}\n"
            f"Competitors: {', '.join(config.product.competitors)}\n"
            f"Differentiators: {', '.join(config.product.differentiators)}\n"
            f"Target audience: {', '.join(config.icp.target_titles)}\n"
            f"Pain points: {', '.join(config.icp.pain_points)}\n"
            f"Where they hang out: {', '.join(config.icp.online_hangouts)}\n"
            f"Brand voice: {config.brand_voice.tone}"
        )

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

        logger.info("[%s] Running for tenant %s (context=%s)", self.AGENT_NAME, tenant_id, context_value)
        result = await call_claude(system_prompt, user_message)

        return {
            "agent": self.AGENT_NAME,
            "tenant_id": tenant_id,
            "status": "completed",
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
