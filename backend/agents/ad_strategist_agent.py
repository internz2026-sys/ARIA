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
_MEDIA_LOOKBACK_MIN = 720  # 12h — ads tolerate staler images than email/social


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

# Campaign: <unique descriptive title — REQUIRED>

The title is mandatory and must be specific, descriptive, and unique to this campaign — it becomes the row label in the user's Projects folder so they can find this campaign later. Examples of GOOD titles: "Q2 Lead Gen — SMAPS-SIS", "Back-to-School Promo — Aug 2026", "Free Trial Push — LinkedIn Devs". BAD titles to NEVER emit: "[Campaign Name]", "Campaign Name", "Untitled", "Facebook Ad Campaign", or anything with a bracketed placeholder. If the user's request hints at an objective ("lead gen", "free trial sign-ups"), reflect that in the title; otherwise build it from the product + audience + season/quarter.

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

## Charts (optional — use ONLY when a visual makes the strategy clearer)
When a chart would help the user grasp the plan (budget split, audience tier weights, funnel projections), emit a [GRAPH_DATA] block with valid JSON. ARIA renders these as branded PNG charts automatically — DO NOT use ASCII art, markdown tables, or describe charts in prose.

Supported chart types:
- `pie` — budget allocation across campaign tiers (Awareness/Retargeting/Conversion), channel mix, audience tier splits
- `bar` — demographic breakdowns, interest weights, projected metric comparisons
- `funnel` — conversion projections (Impressions → Clicks → Leads → Customers)

Format strictly:
[GRAPH_DATA]
{{"type": "pie", "title": "Monthly Budget Allocation", "data": {{"Awareness": 50, "Retargeting": 30, "Conversion": 20}}}}
[/GRAPH_DATA]

Rules:
- Numbers only in `data` values (no "$50" — use 50)
- Cap at 3 charts per campaign plan
- Title under 50 chars
- DO NOT emit a chart for every section — only when it actually adds clarity
- DO NOT describe what the chart will show in prose; the rendered image speaks for itself

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

    Also renders any [GRAPH_DATA] blocks the agent emitted into branded
    PNG charts via backend/services/visualizer.py and replaces them
    with markdown image references inline. Failures are silent — the
    text falls through unchanged so the campaign plan still ships.
    """
    result = await _get().run(tenant_id, context)

    # Chart rendering — runs first so the campaign-plan text in
    # `result["result"]` already has the rendered chart URLs by the
    # time the inbox finalization path picks it up. Wrapped in
    # try/except per spec: malformed data must NOT crash the run.
    try:
        from backend.services.visualizer import process_ad_strategist_text
        if isinstance(result.get("result"), str) and tenant_id:
            transformed = process_ad_strategist_text(tenant_id, result["result"])
            if transformed != result["result"]:
                result["result"] = transformed
    except Exception as e:
        logger.warning("[ad_strategist] chart rendering skipped: %s", e)

    action = (context or {}).get("action", "") or ""
    image_url: str | None = None
    m = _URL_RE.search(action)
    if m:
        image_url = m.group(0)
    else:
        try:
            from backend.services.asset_lookup import (
                get_latest_image_url, find_referenced_asset,
                extract_image_url_from_row, task_has_reference,
            )
            wants_image = bool(_IMAGE_INTENT_RE.search(action)) or task_has_reference(action)
            if wants_image:
                image_url = get_latest_image_url(tenant_id, within_minutes=_MEDIA_LOOKBACK_MIN)
                if not image_url and task_has_reference(action):
                    for row in find_referenced_asset(
                        tenant_id, text_hint=action, agent="media",
                        types=["image"], limit=3,
                    ):
                        u = extract_image_url_from_row(row)
                        if u:
                            image_url = u
                            break
        except Exception as e:
            logger.warning("[ad_strategist] media lookup failed for %s: %s", tenant_id, e)

    if image_url:
        result["image_url"] = image_url
        logger.info("[ad_strategist] attaching hero image to ad draft for %s", tenant_id)

    return result
