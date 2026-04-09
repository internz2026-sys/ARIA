"""CEO Agent — Chief Marketing Strategist for developer founders."""
from __future__ import annotations

from backend.agents.base import BaseAgent

_agent = None


class CEOAgent(BaseAgent):
    AGENT_NAME = "ceo"
    CONTEXT_KEY = "action"
    DEFAULT_CONTEXT = "strategy_review"
    MAX_TOKENS = 1500
    CONTEXT_FIELDS = {"business", "product", "audience", "voice"}

    def build_system_prompt(self, config, action: str) -> str:
        # Defensive field access — when invoked via Paperclip's scheduled
        # heartbeat, the first-active-tenant fallback may select a tenant
        # whose onboarding is incomplete. Don't throw on missing fields.
        positioning = ""
        gtm = getattr(config, "gtm_playbook", None)
        if gtm is not None:
            positioning = getattr(gtm, "positioning", "") or ""
        channels = getattr(config, "channels", None) or []
        channels_str = ", ".join(channels) if channels else "(no channels configured)"
        positioning_str = positioning or "(no positioning yet — onboarding incomplete)"

        return f"""You are ARIA's Chief Marketing Strategist for {config.business_name}.
Team: ContentWriter, EmailMarketer, SocialManager, AdStrategist, Media.

{self.business_context(config, self.CONTEXT_FIELDS)}
Positioning: {positioning_str}
Channels: {channels_str}

Actions: build_gtm_playbook, strategy_review, coordinate, adjust_strategy.
For image/visual tasks, delegate to Media agent with agent_tasks: [{{"agent": "media", "prompt": "description"}}].
Return JSON: action, recommendations[], next_steps[], agent_tasks[]"""

    def build_user_message(self, action: str, context: dict | None) -> str:
        return f"Action: {action}. Context: {context or 'Perform weekly strategy review'}"


def _get():
    global _agent
    if _agent is None:
        _agent = CEOAgent()
    return _agent


AGENT_NAME = CEOAgent.AGENT_NAME


async def run(tenant_id: str, context: dict | None = None) -> dict:
    return await _get().run(tenant_id, context)
