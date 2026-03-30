"""Ad Strategist Agent — Facebook/Meta ads advisor with copy-paste setup guides."""
from __future__ import annotations

from backend.agents.base import BaseAgent, MODEL_HAIKU

_agent = None


class AdStrategistAgent(BaseAgent):
    AGENT_NAME = "ad_strategist"
    CONTEXT_KEY = "action"
    DEFAULT_CONTEXT = "campaign_plan"
    MODEL = MODEL_HAIKU
    MAX_TOKENS = 1500
    CONTEXT_FIELDS = {"business", "product", "differentiators", "audience", "pain_points"}

    def build_system_prompt(self, config, action: str) -> str:
        return f"""You are the Ad Strategist for {config.business_name}.

{self.business_context(config, self.CONTEXT_FIELDS)}
Pricing: {config.product.pricing_info}
Positioning: {config.gtm_playbook.positioning}

Write for beginners who never used Facebook Ads Manager. Include exact steps.

Actions: campaign_plan, audience_targeting, ad_creative, setup_guide, budget_recommendation, ab_test_plan, optimization_review.
Ad creative: 3 primary text variants, 2 headlines, description, CTA button — paste-ready.
Return JSON: action, campaign_structure, ad_creatives[], setup_steps[], budget"""


def _get():
    global _agent
    if _agent is None:
        _agent = AdStrategistAgent()
    return _agent


AGENT_NAME = AdStrategistAgent.AGENT_NAME


async def run(tenant_id: str, context: dict | None = None) -> dict:
    return await _get().run(tenant_id, context)
