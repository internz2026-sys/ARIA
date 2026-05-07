"""Campaign ROI report generator — aggregates lifetime totals across
every uploaded Meta Ads CSV (campaign_reports.raw_metrics_json.totals)
and renders a branded funnel chart of Impressions → Clicks → Conversions.

This is the cheap deterministic sibling of `generate_state_of_union` —
no Claude call, no LLM narrative. The summary + body are computed
directly from the aggregated counters so the Generate button stays
fast (sub-second) and works even when the CLI is offline.

Public API:
  - generate_campaign_roi(tenant_id) -> dict
      Funnel chart + a deterministic blurb on lifetime ad performance.
      Persists into marketing_reports and returns the saved row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.services.supabase import get_db
from backend.services.visualizer import (
    render_chart_from_block,
    upload_chart_to_storage,
)

logger = logging.getLogger("aria.services.reports_campaign_roi")


# Window length used for the marketing_reports period_start / period_end
# columns. Campaign data is sparse and the totals are lifetime (NOT
# windowed), but the report row still wants a concrete period for the
# UI's "for the period of …" line — fall back to a 30-day envelope so
# the row sorts sensibly alongside the other 7-day reports.
_REPORT_WINDOW_DAYS = 30


# ── Aggregation helpers ──────────────────────────────────────────────


def _window_iso(days: int) -> tuple[str, str]:
    """Return (start_iso, end_iso) for a rolling window of N days."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _campaign_roi_totals(tenant_id: str) -> dict[str, float]:
    """Sum spend / impressions / clicks / conversions across every
    campaign_reports row for this tenant. NOT windowed — campaign data
    is sparse and we want lifetime totals on the ROI funnel."""
    sb = get_db()
    try:
        res = (
            sb.table("campaign_reports")
            .select("raw_metrics_json")
            .eq("tenant_id", tenant_id)
            .execute()
        )
    except Exception as e:
        logger.warning("[reports_campaign_roi] campaign_reports aggregation failed: %s", e)
        return {"spend": 0.0, "impressions": 0.0, "clicks": 0.0, "conversions": 0.0, "rows": 0}

    totals: dict[str, float] = {
        "spend": 0.0, "impressions": 0.0, "clicks": 0.0, "conversions": 0.0,
    }
    rows = res.data or []
    for r in rows:
        metrics = (r.get("raw_metrics_json") or {}).get("totals") or {}
        for k in totals.keys():
            v = metrics.get(k)
            if isinstance(v, (int, float)):
                totals[k] += v
    # Round so the prompt + UI don't carry float noise
    out = {k: round(v, 2) for k, v in totals.items()}
    out["rows"] = len(rows)
    return out


# ── Chart rendering ──────────────────────────────────────────────────


def _render_campaign_roi_funnel(
    tenant_id: str, totals: dict[str, float],
) -> dict[str, str] | None:
    """Render a branded funnel chart of Impressions → Clicks →
    Conversions. Returns {url, type, title} for embedding in
    chart_urls, or None when there's nothing meaningful to plot."""
    funnel_data: dict[str, float] = {}
    for src_key, label in (
        ("impressions", "Impressions"),
        ("clicks", "Clicks"),
        ("conversions", "Conversions"),
    ):
        val = totals.get(src_key)
        if isinstance(val, (int, float)) and val > 0:
            funnel_data[label] = float(val)
    # Need at least 2 stages for a funnel to be meaningful
    if len(funnel_data) < 2:
        return None

    block = {
        "type": "funnel",
        "title": "Campaign ROI Funnel",
        "data": funnel_data,
    }
    png = render_chart_from_block(block)
    if not png:
        return None
    url = upload_chart_to_storage(tenant_id, png)
    if not url:
        return None
    return {"url": url, "type": "funnel", "title": block["title"]}


# ── Deterministic narrative ──────────────────────────────────────────


def _format_money(v: float) -> str:
    """Compact money formatting — $1,234.56 / $0.00 — used in summary."""
    try:
        return f"${v:,.2f}"
    except Exception:
        return f"${v}"


def _format_int(v: float) -> str:
    """Comma-separated integer — campaign metrics are nominally ints
    even if Supabase deserialises them as floats."""
    try:
        return f"{int(round(v)):,}"
    except Exception:
        return str(v)


def _deterministic_narrative(totals: dict[str, float]) -> tuple[str, str]:
    """Build a (summary, body) pair purely from aggregated totals. No
    LLM call — this report is meant to be cheap and instant."""
    spend = float(totals.get("spend") or 0.0)
    impressions = float(totals.get("impressions") or 0.0)
    clicks = float(totals.get("clicks") or 0.0)
    conversions = float(totals.get("conversions") or 0.0)
    rows = int(totals.get("rows") or 0)

    if rows == 0 or (spend == 0 and impressions == 0 and clicks == 0):
        summary = "No campaign data uploaded yet — upload a Meta Ads CSV to populate this report."
        body = (
            "No Meta Ads campaign data has been uploaded for this tenant yet, "
            "so there are no totals to aggregate.\n\n"
            "To populate this report, export a campaign-level CSV from Meta "
            "Ads Manager (Reports → Export) and upload it via the Campaigns "
            "tab. Once at least one CSV is processed, this report will show "
            "lifetime spend, impressions, clicks, and conversions plus a "
            "funnel chart of Impressions → Clicks → Conversions.\n\n"
            "Tip: ask the Ad Strategist agent for a campaign plan before you "
            "upload, so you have a baseline strategy to measure the numbers "
            "against."
        )
        return summary[:280], body

    # Rates — guard against zero-division
    ctr = (clicks / impressions * 100.0) if impressions > 0 else 0.0
    cvr = (conversions / clicks * 100.0) if clicks > 0 else 0.0
    cpc = (spend / clicks) if clicks > 0 else 0.0
    cpa = (spend / conversions) if conversions > 0 else 0.0

    summary = (
        f"{_format_money(spend)} spent across {rows} campaign report"
        f"{'s' if rows != 1 else ''} — "
        f"{_format_int(impressions)} impressions, "
        f"{_format_int(clicks)} clicks, "
        f"{_format_int(conversions)} conversion{'s' if conversions != 1 else ''}."
    )

    para1 = (
        f"Lifetime totals across {rows} uploaded campaign report"
        f"{'s' if rows != 1 else ''}: "
        f"**{_format_money(spend)}** spent, "
        f"**{_format_int(impressions)}** impressions, "
        f"**{_format_int(clicks)}** clicks, "
        f"**{_format_int(conversions)}** conversions."
    )

    rate_bits: list[str] = []
    if impressions > 0:
        rate_bits.append(f"CTR **{ctr:.2f}%**")
    if clicks > 0:
        rate_bits.append(f"CVR **{cvr:.2f}%**")
    if clicks > 0:
        rate_bits.append(f"CPC **{_format_money(cpc)}**")
    if conversions > 0:
        rate_bits.append(f"CPA **{_format_money(cpa)}**")
    para2 = (
        "Derived rates: " + ", ".join(rate_bits) + "."
        if rate_bits
        else "Not enough volume yet to compute meaningful CTR / CVR / CPA."
    )

    para3 = (
        "Funnel chart shows the Impressions → Clicks → Conversions drop-off. "
        "Use the steepest drop as the next optimisation target — "
        "low CTR means the creative isn't grabbing attention, low CVR means "
        "the landing page isn't closing. Upload another CSV after each "
        "optimisation pass to track movement on this funnel."
    )

    body = f"{para1}\n\n{para2}\n\n{para3}"
    return summary[:280], body


# ── Public report generator ──────────────────────────────────────────


async def generate_campaign_roi(tenant_id: str) -> dict[str, Any]:
    """Lightweight campaign ROI report. Aggregates lifetime totals from
    every uploaded Meta Ads CSV for the tenant, renders a funnel chart
    of Impressions → Clicks → Conversions, and persists into
    marketing_reports with a deterministic summary + body — NO Claude
    call, so this is cheap and never blocked on the CLI being up.

    Mirrors `generate_agent_productivity` in shape: aggregate → render →
    deterministic narrative → insert → return saved row.
    """
    start_iso, end_iso = _window_iso(_REPORT_WINDOW_DAYS)

    totals = _campaign_roi_totals(tenant_id)
    chart = _render_campaign_roi_funnel(tenant_id, totals)
    summary, body = _deterministic_narrative(totals)

    row = {
        "tenant_id": tenant_id,
        "report_type": "campaign_roi",
        "agent": "Ad Strategist",
        "title": "Campaign ROI Funnel",
        "summary": summary,
        "body_markdown": body,
        "chart_urls": [chart] if chart else [],
        "metrics": {
            "totals": {
                "spend": totals.get("spend", 0.0),
                "impressions": totals.get("impressions", 0.0),
                "clicks": totals.get("clicks", 0.0),
                "conversions": totals.get("conversions", 0.0),
            },
            "campaign_reports_count": int(totals.get("rows") or 0),
        },
        "period_start": start_iso,
        "period_end": end_iso,
    }

    sb = get_db()
    try:
        ins = sb.table("marketing_reports").insert(row).execute()
    except Exception as e:
        logger.exception("[reports_campaign_roi] insert campaign_roi failed: %s", e)
        raise

    saved = (ins.data or [None])[0] or row
    return saved
