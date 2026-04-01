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

Write for beginners who have never used ads before. Use clear, readable formatting.

Output in clean markdown (NOT JSON). Structure your response like this:

# Campaign: [Campaign Name]

## Overview
- **Platform:** Facebook / LinkedIn / Google
- **Objective:** [Goal]
- **Duration:** [Timeframe]
- **Budget:** $[amount]/day or $[amount] total

## Target Audience
- [Who to target]
- [Demographics, interests, job titles]

## Ad Creatives

### Ad Variant 1
**Headline:** [headline]
**Primary Text:** [ad copy]
**Description:** [description]
**CTA Button:** [Learn More / Sign Up / etc.]

### Ad Variant 2
[Same format]

## Step-by-Step Setup Guide
1. [First step with exact instructions]
2. [Next step]
3. [Continue...]

## A/B Testing Plan
- Test A: [variant]
- Test B: [variant]
- Run for [duration], measure [metric]

## Budget Recommendations
- [Budget breakdown and optimization tips]

Keep it actionable, copy-paste ready, and beginner-friendly."""


def _get():
    global _agent
    if _agent is None:
        _agent = AdStrategistAgent()
    return _agent


AGENT_NAME = AdStrategistAgent.AGENT_NAME


async def run(tenant_id: str, context: dict | None = None) -> dict:
    return await _get().run(tenant_id, context)
