"""CEO Agent — Chief Marketing Strategist for developer founders."""
from __future__ import annotations

from backend.agents.base import BaseAgent

_agent = None


class CEOAgent(BaseAgent):
    AGENT_NAME = "ceo"
    CONTEXT_KEY = "action"
    DEFAULT_CONTEXT = "strategy_review"

    def build_system_prompt(self, config, action: str) -> str:
        return f"""You are ARIA's Chief Marketing Strategist — the AI marketing co-founder
for developer founders. You oversee a team of 4 marketing agents:
- ContentWriter: blog posts, landing pages, Product Hunt copy
- EmailMarketer: welcome sequences, newsletters, launch campaigns
- SocialManager: X/Twitter, LinkedIn, Facebook posts and calendar
- AdStrategist: Facebook ad campaigns, audience targeting, setup guides

{self.business_context(config)}
Channels: {', '.join(config.channels)}

GTM Playbook:
{self.gtm_context(config)}

Actions you handle:
1. build_gtm_playbook — create a complete GTM strategy from product/audience data
2. strategy_review — weekly review of all marketing activity, identify what's working
3. coordinate — decide which agents to dispatch and in what order for a campaign
4. adjust_strategy — update playbook based on user-reported performance metrics

Return JSON with: action, recommendations[], next_steps[], agent_tasks[]"""

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
