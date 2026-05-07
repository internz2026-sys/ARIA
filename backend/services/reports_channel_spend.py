"""Channel Spend report — pie-chart breakdown of where the marketing
budget actually went over the last 30 days, across three channels:

  - Facebook / Meta Ads (real $ from campaign_reports.raw_metrics_json.totals.spend
    on rows where platform="facebook")
  - Email (proxy: outbound email_messages count × $0.001/send — symbolic
    cost since SMTP is essentially free)
  - Social (proxy: inbox_items where agent="social_manager" and
    status="sent" × $0.10/post — founder time-cost proxy)

Pure deterministic generator — no Claude call. Mirrors the structure of
`generate_agent_productivity` in `backend/services/reports.py`:
aggregate → render chart → persist row → return saved row.

Public API:
  - generate_channel_spend(tenant_id) -> dict
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

logger = logging.getLogger("aria.services.reports_channel_spend")


# Per-unit cost coefficients for the proxy channels. Tuned to be small but
# non-zero so they show up on the pie even when ads spend dwarfs them. If
# the user later wires real cost tracking (e.g. Mailchimp invoices), swap
# these for actuals.
EMAIL_COST_PER_SEND = 0.001
SOCIAL_COST_PER_POST = 0.10

# Rolling window — 30 days matches the typical ad-platform billing cycle.
WINDOW_DAYS = 30


# ── Aggregation helpers ──────────────────────────────────────────────


def _window_iso(days: int) -> tuple[str, str]:
    """Return (start_iso, end_iso) for a rolling window of N days."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _facebook_ad_spend(tenant_id: str, since_iso: str) -> float:
    """Sum spend across campaign_reports rows for facebook campaigns
    inside the window. Reads created_at as the windowing field — the
    raw_metrics_json totals are already lifetime-to-date for that report
    snapshot, so windowing on the report's own timestamp is the closest
    we get to monthly attribution without a campaign->daily-spend table."""
    sb = get_db()
    try:
        res = (
            sb.table("campaign_reports")
            .select("platform,raw_metrics_json,created_at")
            .eq("tenant_id", tenant_id)
            .gte("created_at", since_iso)
            .execute()
        )
    except Exception as e:
        logger.warning("[channel_spend] campaign_reports aggregation failed: %s", e)
        return 0.0

    total = 0.0
    for r in (res.data or []):
        platform = (r.get("platform") or "").strip().lower()
        if platform != "facebook":
            continue
        metrics = (r.get("raw_metrics_json") or {}).get("totals") or {}
        v = metrics.get("spend")
        if isinstance(v, (int, float)):
            total += float(v)
    return round(total, 2)


def _outbound_email_count(tenant_id: str, since_iso: str) -> int:
    """Count outbound email_messages within the window."""
    sb = get_db()
    try:
        res = (
            sb.table("email_messages")
            .select("direction,message_timestamp")
            .eq("tenant_id", tenant_id)
            .eq("direction", "outbound")
            .gte("message_timestamp", since_iso)
            .execute()
        )
    except Exception as e:
        logger.warning("[channel_spend] email_messages aggregation failed: %s", e)
        return 0
    return len(res.data or [])


def _social_post_count(tenant_id: str, since_iso: str) -> int:
    """Count social_manager inbox_items marked sent within the window."""
    sb = get_db()
    try:
        res = (
            sb.table("inbox_items")
            .select("agent,status,created_at")
            .eq("tenant_id", tenant_id)
            .eq("agent", "social_manager")
            .eq("status", "sent")
            .gte("created_at", since_iso)
            .execute()
        )
    except Exception as e:
        logger.warning("[channel_spend] inbox_items aggregation failed: %s", e)
        return 0
    return len(res.data or [])


# ── Chart rendering ──────────────────────────────────────────────────


def _render_spend_pie(
    tenant_id: str, channel_spend: dict[str, float],
) -> dict[str, str] | None:
    """Render a pie chart of channel-spend split. Returns
    {url, type, title} for embedding in chart_urls, or None on failure
    (no spend at all, render error, or upload error)."""
    # Drop zero slices — the pie reads cleaner with only present channels.
    display_data = {k: v for k, v in channel_spend.items() if v > 0}
    if not display_data:
        return None

    block = {
        "type": "pie",
        "title": f"Channel spend split (last {WINDOW_DAYS} days)",
        "data": display_data,
    }
    png = render_chart_from_block(block)
    if not png:
        return None
    url = upload_chart_to_storage(tenant_id, png)
    if not url:
        return None
    return {"url": url, "type": "pie", "title": block["title"]}


# ── Deterministic narrative ──────────────────────────────────────────


def _format_money(v: float) -> str:
    """Compact USD formatter — no decimals when whole, two when not."""
    if abs(v - round(v)) < 0.005:
        return f"${int(round(v))}"
    return f"${v:,.2f}"


def _channel_spend_narrative(
    channel_spend: dict[str, float],
    counters: dict[str, Any],
) -> tuple[str, str]:
    """Build a (summary, body) pair purely from the aggregated counters.
    Two short markdown paragraphs — no LLM call."""
    fb = channel_spend.get("Facebook Ads", 0.0)
    em = channel_spend.get("Email", 0.0)
    so = channel_spend.get("Social (organic)", 0.0)
    total = fb + em + so

    if total <= 0:
        summary = (
            f"No measurable channel spend recorded in the last {WINDOW_DAYS} days."
        )
        body = (
            f"No Facebook ad spend, no outbound email volume, and no published "
            f"social posts in the last {WINDOW_DAYS} days — there's nothing yet "
            "to split across the pie chart.\n\n"
            "Once you launch a campaign, send an email sequence, or publish a "
            "batch of social posts, this report will fill in with the actual "
            "channel mix and proxy costs."
        )
        return summary[:280], body

    # Identify the dominant channel for the summary line.
    largest_channel, largest_spend = max(
        channel_spend.items(), key=lambda kv: kv[1]
    )
    largest_pct = (largest_spend / total) * 100 if total > 0 else 0.0

    summary = (
        f"{_format_money(total)} estimated channel spend over "
        f"{WINDOW_DAYS} days — {largest_channel} dominated at "
        f"{largest_pct:.0f}%."
    )

    email_sends = counters.get("email_outbound_count", 0)
    social_posts = counters.get("social_posts_count", 0)
    body = (
        f"Estimated total channel spend across the last {WINDOW_DAYS} days "
        f"came in at {_format_money(total)}: "
        f"{_format_money(fb)} on Facebook / Meta Ads, "
        f"{_format_money(em)} on email "
        f"({email_sends} outbound send{'s' if email_sends != 1 else ''} "
        f"× ${EMAIL_COST_PER_SEND:.3f} per send), and "
        f"{_format_money(so)} on organic social "
        f"({social_posts} post{'s' if social_posts != 1 else ''} "
        f"× ${SOCIAL_COST_PER_POST:.2f} founder-time proxy).\n\n"
        f"The pie chart shows where the budget is actually flowing. "
        f"Email and social proxies are deliberately small so the chart "
        "stays honest about real cash spend on paid channels — but they "
        "still show whether you're investing time in owned channels at "
        "all. If one slice is missing entirely, that's a channel you "
        "haven't activated yet."
    )
    return summary[:280], body


# ── Public report generator ──────────────────────────────────────────


async def generate_channel_spend(tenant_id: str) -> dict[str, Any]:
    """Generate a 30-day channel-spend pie-chart report.

    Aggregates Facebook ad spend + email-send proxy + social-post proxy →
    renders the pie chart → persists into marketing_reports → returns
    the saved row for the frontend to display immediately.

    Pure deterministic — no Claude call. Mirrors the structure of
    `generate_agent_productivity` in `backend/services/reports.py`.
    """
    start_iso, end_iso = _window_iso(WINDOW_DAYS)

    fb_spend = _facebook_ad_spend(tenant_id, start_iso)
    email_count = _outbound_email_count(tenant_id, start_iso)
    social_count = _social_post_count(tenant_id, start_iso)

    email_spend = round(email_count * EMAIL_COST_PER_SEND, 2)
    social_spend = round(social_count * SOCIAL_COST_PER_POST, 2)

    channel_spend: dict[str, float] = {
        "Facebook Ads": fb_spend,
        "Email": email_spend,
        "Social (organic)": social_spend,
    }
    total_spend = round(fb_spend + email_spend + social_spend, 2)

    counters: dict[str, Any] = {
        "facebook_spend": fb_spend,
        "email_outbound_count": email_count,
        "email_proxy_cost_per_send": EMAIL_COST_PER_SEND,
        "email_spend_proxy": email_spend,
        "social_posts_count": social_count,
        "social_proxy_cost_per_post": SOCIAL_COST_PER_POST,
        "social_spend_proxy": social_spend,
    }

    metrics: dict[str, Any] = {
        "channel_spend": channel_spend,
        "total_spend": total_spend,
        "counters": counters,
        "window_days": WINDOW_DAYS,
    }

    chart = _render_spend_pie(tenant_id, channel_spend)
    chart_urls: list[dict[str, str]] = [chart] if chart else []

    summary, body = _channel_spend_narrative(channel_spend, counters)

    row = {
        "tenant_id": tenant_id,
        "report_type": "channel_spend",
        "agent": "ARIA CEO",
        "title": f"Channel Spend ({WINDOW_DAYS}d)",
        "summary": summary,
        "body_markdown": body,
        "chart_urls": chart_urls,
        "metrics": metrics,
        "period_start": start_iso,
        "period_end": end_iso,
    }

    sb = get_db()
    try:
        ins = sb.table("marketing_reports").insert(row).execute()
    except Exception as e:
        logger.exception("[channel_spend] insert failed: %s", e)
        raise

    saved = (ins.data or [None])[0] or row
    return saved
