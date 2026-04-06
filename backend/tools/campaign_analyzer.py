"""Campaign Report Analyzer — AI-powered analysis of Facebook Ads reports.

Uses the Ad Strategist agent persona to generate human-readable campaign reports
from parsed metric data.
"""
from __future__ import annotations

import json
import logging

from backend.config.loader import get_tenant_config
from backend.tools.claude_cli import call_claude, MODEL_HAIKU

logger = logging.getLogger("aria.campaign_analyzer")


def _format_metrics(metrics: dict) -> str:
    """Format raw metrics into a readable summary for the AI."""
    labels = {
        "spend": "Amount Spent",
        "impressions": "Impressions",
        "reach": "Reach",
        "clicks": "Clicks (All)",
        "link_clicks": "Link Clicks",
        "ctr": "CTR (%)",
        "cpc": "Cost Per Click",
        "cpm": "CPM",
        "conversions": "Conversions/Results",
        "cost_per_result": "Cost Per Result",
        "frequency": "Frequency",
        "video_views": "Video Views",
        "video_views_3s": "3s Video Views",
        "thruplays": "ThruPlays",
        "post_engagement": "Post Engagement",
        "page_likes": "Page Likes",
        "roas": "ROAS",
    }
    lines = []
    for key, label in labels.items():
        val = metrics.get(key)
        if val is not None:
            if key == "spend":
                lines.append(f"- {label}: ${val:,.2f}")
            elif key in ("ctr",):
                lines.append(f"- {label}: {val}%")
            elif key in ("cpc", "cpm", "cost_per_result"):
                lines.append(f"- {label}: ${val:,.2f}")
            elif isinstance(val, float):
                lines.append(f"- {label}: {val:,.2f}")
            else:
                lines.append(f"- {label}: {val:,}")
    return "\n".join(lines) if lines else "No metrics available."


async def analyze_report(tenant_id: str, campaign: dict, report: dict) -> dict:
    """Generate an AI campaign report from parsed metrics.

    Returns:
        {
            "report_text": "Full AI report in markdown...",
            "recommendations": "Bullet-pointed recommendations..."
        }
    """
    config = get_tenant_config(tenant_id)

    raw_metrics = report.get("raw_metrics_json", {})
    totals = raw_metrics.get("totals", {})
    campaigns_data = raw_metrics.get("campaigns", [])

    # Build metrics summary
    metrics_text = _format_metrics(totals)

    # Per-campaign breakdown if multiple
    per_campaign = ""
    if len(campaigns_data) > 1:
        parts = []
        for cd in campaigns_data:
            parts.append(f"\n### {cd['campaign_name']}\n{_format_metrics(cd['metrics'])}")
        per_campaign = "\n".join(parts)

    system_prompt = f"""You are the Ad Strategist for {config.business_name}. You are analyzing a Facebook Ads campaign report that was manually uploaded by the user.

Business context:
- Product: {config.product.name} — {config.product.description}
- Positioning: {config.gtm_playbook.positioning}

Your role is to create a clear, actionable campaign performance report. Write for someone who may be running ads for the first time. Be honest about performance — don't sugarcoat bad numbers, but be constructive.

IMPORTANT: This data comes from a manually uploaded Facebook Ads Manager export. Do NOT pretend this is live/real-time data. Refer to it as "the uploaded report" or "the imported data."

Format your response as TWO clearly separated sections:

===REPORT===
(Your full campaign report in clean markdown)

===RECOMMENDATIONS===
(Your actionable recommendations as bullet points)"""

    user_message = f"""Analyze this Facebook Ads campaign report:

**Campaign:** {campaign.get('campaign_name', 'Unknown')}
**Platform:** {campaign.get('platform', 'Facebook')}
**Objective:** {campaign.get('objective', 'Not specified')}
**Report Period:** {report.get('report_start_date', 'Unknown')} to {report.get('report_end_date', 'Unknown')}
**Source:** Manually uploaded CSV ({report.get('source_file_name', 'report.csv')})

### Overall Metrics
{metrics_text}
{f'''
### Per-Campaign Breakdown
{per_campaign}''' if per_campaign else ''}

Generate a comprehensive campaign report with these sections:
1. Overview — campaign name, period, objective, data source
2. Performance Summary — high-level assessment (good/average/poor)
3. Key Metrics — the important numbers with context
4. Observations — what stands out, trends, notable patterns
5. Risks / Issues — any red flags or concerns
6. Recommendations — specific, actionable next steps

End with clear, numbered recommendations."""

    result = await call_claude(
        system_prompt,
        user_message,
        max_tokens=2000,
        tenant_id=tenant_id,
        model=MODEL_HAIKU,
        agent_id="ad_strategist",
    )

    # Split into report + recommendations
    report_text = result
    recommendations = ""

    if "===RECOMMENDATIONS===" in result:
        parts = result.split("===RECOMMENDATIONS===", 1)
        report_text = parts[0].replace("===REPORT===", "").strip()
        recommendations = parts[1].strip()
    elif "===REPORT===" in result:
        report_text = result.replace("===REPORT===", "").strip()

    return {
        "report_text": report_text,
        "recommendations": recommendations,
    }
