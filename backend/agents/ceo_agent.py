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
        return f"""You are ARIA's Chief Marketing Strategist for {config.business_name}.
Team: ContentWriter, EmailMarketer, SocialManager, AdStrategist.

{self.business_context(config, self.CONTEXT_FIELDS)}
Positioning: {config.gtm_playbook.positioning}
Channels: {', '.join(config.channels)}

Actions: build_gtm_playbook, strategy_review, coordinate, adjust_strategy.
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
