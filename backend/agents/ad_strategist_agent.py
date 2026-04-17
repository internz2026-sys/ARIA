"""Ad Strategist Agent — Facebook/Meta ads advisor with copy-paste setup guides."""
from __future__ import annotations

import logging
import re

from backend.agents.base import BaseAgent, MODEL_HAIKU

logger = logging.getLogger("aria.ad_strategist")

_agent = None

# Triggers for attaching a recent Media Agent image as the hero creative
# for the ad. Kept in the agent (not asset_lookup) so the lookback and
# keyword policy can diverge from email/social without coupling.
_IMAGE_INTENT_RE = re.compile(
    r"\b(image|images|photo|photos|picture|pictures|banner|hero|"
    r"visual|visuals|graphic|graphics|creative|creatives|"
    r"thumbnail|screenshot|screenshots)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+?\.(?:png|jpg|jpeg|gif|webp|svg)(?:\?\S*)?", re.IGNORECASE)
_MEDIA_LOOKBACK_MIN = 60  # ads tolerate slightly staler images than email/social


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
    """Cross-agent hook: if the ad task mentions a hero image / creative
    or pastes a URL, attach the latest Media Agent image so the ad draft
    ships with a visual. The image URL rides on the result dict as
    `image_url` — the frontend's ad editor picks it up next to the copy.
    """
    result = await _get().run(tenant_id, context)

    action = (context or {}).get("action", "") or ""
    image_url: str | None = None
    m = _URL_RE.search(action)
    if m:
        image_url = m.group(0)
    elif _IMAGE_INTENT_RE.search(action):
        try:
            from backend.services.asset_lookup import get_latest_image_url
            image_url = get_latest_image_url(tenant_id, within_minutes=_MEDIA_LOOKBACK_MIN)
        except Exception as e:
            logger.warning("[ad_strategist] media lookup failed for %s: %s", tenant_id, e)

    if image_url:
        result["image_url"] = image_url
        logger.info("[ad_strategist] attaching hero image to ad draft for %s", tenant_id)

    return result
