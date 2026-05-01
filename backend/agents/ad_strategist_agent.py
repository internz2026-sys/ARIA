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
    # Bumped from 1500 → 2500 on 2026-05-01 to leave room for at least one
    # mandatory [GRAPH_DATA] block in the campaign brief. Production sample
    # of 4 recent runs showed 0/4 emitted graphs because the brief itself
    # consumed ~1400 tokens, leaving no headroom for a JSON chart payload.
    MAX_TOKENS = 2500
    CONTEXT_FIELDS = {"business", "product", "differentiators", "audience", "pain_points"}

    @staticmethod
    def _format_past_performance_block(tenant_id: str) -> str:
        """Build the 'Past Campaign Performance' prompt section.

        Pulls up to 3 recent Ad Strategist campaigns for the tenant
        whose `metadata.performance` is populated, formats each as a
        single summary line, and wraps them in a markdown block the
        model can use to learn from prior wins / losses. Returns an
        empty string if no past campaigns have metrics — caller must
        skip the section entirely instead of injecting an empty block.

        Best-effort: any DB error or import failure short-circuits to
        an empty string so an unmigrated tenant or transient outage
        never blocks the agent run.
        """
        if not tenant_id:
            return ""
        try:
            from backend.services.campaigns import (
                list_recent_campaigns_with_metrics,
            )
        except Exception:
            return ""
        try:
            past = list_recent_campaigns_with_metrics(
                tenant_id, source_type="ad_strategist", limit=3,
            )
        except Exception as e:
            logger.debug(
                "[ad_strategist] past-performance lookup failed: %s", e,
            )
            return ""
        if not past:
            return ""

        lines: list[str] = []
        for camp in past:
            name = (camp.get("campaign_name") or "Untitled")[:80]
            meta = camp.get("metadata") or {}
            perf = meta.get("performance") or {}
            winning = meta.get("winning_variant") or "n/a"

            def _fmt(key: str) -> str:
                v = perf.get(key)
                if v is None or v == "":
                    return "?"
                return str(v)

            clicks = _fmt("clicks")
            leads = _fmt("leads")
            spend = _fmt("spend")
            cpl = _fmt("cpl")
            lines.append(
                f"- {name}: {clicks} clicks, {leads} leads, "
                f"${spend} spend, CPL ${cpl}, winning_variant={winning}"
            )

        return (
            "## Past Campaign Performance (for learning)\n"
            "Reference the following past results from this tenant. Use these to:\n"
            "- Avoid copy/audience patterns that underperformed\n"
            "- Lean into patterns that won\n"
            "- Pick the variant style (A=<X>, B=<Y>) that historically "
            "converted better\n\n"
            + "\n".join(lines)
        )

    def build_system_prompt(self, config, action: str) -> str:
        # Past Performance Context — closes the feedback loop so each
        # new brief is informed by what actually worked last time.
        # Injected RIGHT BEFORE the "Output in clean markdown" line per
        # the workstream spec; falls back to empty string when the
        # tenant has no past Ad Strategist campaigns with metrics.
        past_perf_block = ""
        try:
            tid = str(getattr(config, "tenant_id", "") or "")
            if tid:
                past_perf_block = self._format_past_performance_block(tid)
        except Exception as e:
            logger.debug("[ad_strategist] perf-block injection skipped: %s", e)
        past_perf_section = (
            f"\n\n{past_perf_block}\n\n" if past_perf_block else "\n\n"
        )
        return f"""You are the Ad Strategist for {config.business_name}.

{self.business_context(config, self.CONTEXT_FIELDS)}
Pricing: {config.product.pricing_info}
Positioning: {config.gtm_playbook.positioning}

Write for beginners who have never used ads before. Use clear, readable formatting.
{past_perf_section}Output in clean markdown (NOT JSON). Structure your response like this:

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

## DO NOT include charts or [GRAPH_DATA] blocks in this campaign brief
Campaign briefs are text-only. Charts belong in the AI Report flow that
runs AFTER the user uploads real performance data (clicks, leads, spend
from Meta Ads Manager). That flow has its own prompt and will request
charts based on actual numbers — not on the imagined budget splits in
this brief.

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

    # NOTE: charts are intentionally NOT rendered here anymore. Campaign
    # briefs are text-only — the AI Report flow (campaign_analyzer.py)
    # owns chart rendering since charts there are based on actual uploaded
    # performance data, not imagined budget splits in the brief.

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
