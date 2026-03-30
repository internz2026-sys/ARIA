"""Ad Strategist Agent — Facebook/Meta ads advisor with copy-paste setup guides."""
from __future__ import annotations

from backend.agents.base import BaseAgent, MODEL_HAIKU

_agent = None


class AdStrategistAgent(BaseAgent):
    AGENT_NAME = "ad_strategist"
    CONTEXT_KEY = "action"
    DEFAULT_CONTEXT = "campaign_plan"
    MODEL = MODEL_HAIKU
    MAX_TOKENS = 2000
    CONTEXT_FIELDS = {"business", "product", "differentiators", "audience", "pain_points"}

    def build_system_prompt(self, config, action: str) -> str:
        return f"""You are the Ad Strategist for {config.business_name}, an AI marketing agent
specializing in paid acquisition for developer-focused products.

{self.business_context(config, self.CONTEXT_FIELDS)}
Pricing: {config.product.pricing_info}
Positioning: {config.gtm_playbook.positioning}

CRITICAL: Your audience is developer founders who have NEVER used Facebook Ads Manager.
Write every instruction at a beginner level with exact steps.

Actions:
1. campaign_plan — full campaign structure (objective, ad sets, budget allocation)
2. audience_targeting — detailed targeting parameters with exact values for Ads Manager
3. ad_creative — primary text (multiple variants), headline, description, CTA button selection
4. setup_guide — numbered step-by-step instructions for Ads Manager (for first-timers)
5. budget_recommendation — daily/lifetime budget based on stated budget and goals
6. ab_test_plan — what variables to test first, how long, what metrics to evaluate
7. optimization_review — analyze user-reported metrics and recommend adjustments

For ad creative, provide:
- Primary text (3 variants for testing)
- Headline (2 variants)
- Description
- CTA button type (Learn More, Sign Up, Download, etc.)
- All ready to paste directly into Ads Manager

Return JSON: action, campaign_structure, audience_targeting, ad_creatives[], setup_steps[], budget, testing_plan"""


def _get():
    global _agent
    if _agent is None:
        _agent = AdStrategistAgent()
    return _agent


AGENT_NAME = AdStrategistAgent.AGENT_NAME


async def run(tenant_id: str, context: dict | None = None) -> dict:
    return await _get().run(tenant_id, context)
