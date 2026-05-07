"""Marketing Reports service — aggregates agent activity into persisted
"State of the Union" reports for the Reports tab.

Two-step generation:

  1. Aggregate raw counters from agent_logs / inbox_items / email_messages
     / campaign_reports for the requested window. All Supabase queries
     run synchronously (the supabase-py client is sync-only).
  2. Call Claude Haiku with the counters and have it write a short
     summary + a 3-paragraph markdown body. The numbers are already in
     the prompt so the LLM only adds narrative — it doesn't have to
     compute anything.

Then we render any associated charts via visualizer.render_chart_from_block,
upload them to Supabase Storage, and persist a marketing_reports row
with the body, summary, chart URLs, and raw metrics.

Public API:
  - generate_state_of_union(tenant_id) -> dict
      7-day cross-agent narrative. The default Generate button in the UI.
  - generate_agent_productivity(tenant_id) -> dict
      Bar chart + short blurb on tasks completed per agent.
  - list_reports(tenant_id, limit) -> list[dict]
  - get_report(tenant_id, report_id) -> dict | None
  - delete_report(tenant_id, report_id) -> dict
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.services.realtime import agent_display_name
from backend.services.supabase import get_db
from backend.services.visualizer import (
    render_chart_from_block,
    upload_chart_to_storage,
)

logger = logging.getLogger("aria.services.reports")


# Agent slug → display name. Single source of truth lives in
# backend/services/realtime.py:_AGENT_DISPLAY_NAMES (also used by the
# task-completed toast so labels stay consistent across the app). The
# frontend mirror is `AGENT_NAMES` in
# frontend/lib/agent-config.ts / frontend/app/(dashboard)/inbox/page.tsx
# — update both sides when a new agent is added.


# ── Aggregation helpers ──────────────────────────────────────────────


def _window_iso(days: int) -> tuple[str, str]:
    """Return (start_iso, end_iso) for a rolling window of N days."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _count_agent_logs(tenant_id: str, since_iso: str) -> dict[str, int]:
    """Group completed agent_logs rows by agent_name within the window."""
    sb = get_db()
    try:
        res = (
            sb.table("agent_logs")
            .select("agent_name,status,timestamp")
            .eq("tenant_id", tenant_id)
            .gte("timestamp", since_iso)
            .execute()
        )
    except Exception as e:
        logger.warning("[reports] agent_logs aggregation failed: %s", e)
        return {}

    counts: Counter[str] = Counter()
    for row in (res.data or []):
        if (row.get("status") or "").lower() in ("completed", "completed_with_warning"):
            counts[row.get("agent_name") or "unknown"] += 1
    return dict(counts)


def _count_inbox_items(tenant_id: str, since_iso: str) -> dict[str, Any]:
    """Group inbox_items by status within the window. The inbox is the
    user-facing record of what each agent produced — separate signal
    from agent_logs (which is the *attempt* count, not the *output*
    count)."""
    sb = get_db()
    try:
        res = (
            sb.table("inbox_items")
            .select("agent,status,type,created_at")
            .eq("tenant_id", tenant_id)
            .gte("created_at", since_iso)
            .execute()
        )
    except Exception as e:
        logger.warning("[reports] inbox_items aggregation failed: %s", e)
        return {"total": 0, "by_status": {}, "by_agent": {}}

    rows = res.data or []
    by_status: Counter[str] = Counter()
    by_agent: Counter[str] = Counter()
    for r in rows:
        by_status[(r.get("status") or "unknown")] += 1
        by_agent[(r.get("agent") or "unknown")] += 1
    return {
        "total": len(rows),
        "by_status": dict(by_status),
        "by_agent": dict(by_agent),
    }


def _count_email_messages(tenant_id: str, since_iso: str) -> dict[str, int]:
    """Outbound + inbound email counts within the window."""
    sb = get_db()
    try:
        res = (
            sb.table("email_messages")
            .select("direction,message_timestamp")
            .eq("tenant_id", tenant_id)
            .gte("message_timestamp", since_iso)
            .execute()
        )
    except Exception as e:
        logger.warning("[reports] email_messages aggregation failed: %s", e)
        return {"sent": 0, "received": 0}

    sent = 0
    received = 0
    for r in (res.data or []):
        d = (r.get("direction") or "").lower()
        if d == "outbound":
            sent += 1
        elif d == "inbound":
            received += 1
    return {"sent": sent, "received": received}


def _campaign_spend_totals(tenant_id: str) -> dict[str, float]:
    """Sum spend / impressions / clicks / conversions across every
    campaign_reports row for this tenant. NOT windowed — campaign data
    is sparse and we want lifetime totals on the Snapshot view."""
    sb = get_db()
    try:
        res = (
            sb.table("campaign_reports")
            .select("raw_metrics_json")
            .eq("tenant_id", tenant_id)
            .execute()
        )
    except Exception as e:
        logger.warning("[reports] campaign_reports aggregation failed: %s", e)
        return {}

    totals: dict[str, float] = {
        "spend": 0.0, "impressions": 0.0, "clicks": 0.0, "conversions": 0.0,
    }
    for r in (res.data or []):
        metrics = (r.get("raw_metrics_json") or {}).get("totals") or {}
        for k in totals.keys():
            v = metrics.get(k)
            if isinstance(v, (int, float)):
                totals[k] += v
    # Round so the prompt + UI don't carry float noise
    return {k: round(v, 2) for k, v in totals.items()}


# ── Chart rendering ──────────────────────────────────────────────────


def _render_agent_productivity_chart(
    tenant_id: str, agent_counts: dict[str, int],
) -> dict[str, str] | None:
    """Render a branded bar chart of tasks-per-agent. Returns
    {url, type, title} for embedding in chart_urls, or None on failure."""
    if not agent_counts:
        return None
    # Re-key with display names so the x-axis reads cleanly
    display_data = {
        agent_display_name(k): v
        for k, v in agent_counts.items()
        if v > 0
    }
    if not display_data:
        return None

    block = {
        "type": "bar",
        "title": "Tasks completed (last 7 days)",
        "data": display_data,
    }
    png = render_chart_from_block(block)
    if not png:
        return None
    url = upload_chart_to_storage(tenant_id, png)
    if not url:
        return None
    return {"url": url, "type": "bar", "title": block["title"]}


# ── Claude narrative ─────────────────────────────────────────────────


_STATE_OF_UNION_SYSTEM = """You are the Chief Marketing Strategist for {business_name}.
You are writing a "State of the Union" report summarizing the last 7 days of
marketing activity for the founder.

You will be given JSON counters. Use the actual numbers in your write-up — do
not invent values. If a counter is zero, acknowledge it ("no campaigns ran",
"no replies came in") rather than glossing over it.

Output FORMAT — strict, no extra preamble:

SUMMARY: <one sentence, ≤ 240 characters, plain text. Lead with the most
notable signal (highest agent activity, big campaign spend, surge in replies).>

BODY:
<3 short markdown paragraphs separated by blank lines. NO heading marks
(# / ##). Paragraph 1 = what got done. Paragraph 2 = what's working /
what isn't. Paragraph 3 = recommended focus for the next 7 days. Tone
is concise and operational — no fluff, no bullet padding.>"""


def _deterministic_narrative(metrics: dict[str, Any]) -> tuple[str, str]:
    """Build a (summary, body) pair purely from the aggregated counters.
    Used as the fallback when the Claude CLI is unreachable so the
    Generate button is never a hard 500."""
    agents = metrics.get("tasks_by_agent") or {}
    total_tasks = sum(agents.values())
    sent = (metrics.get("emails") or {}).get("sent", 0)
    received = (metrics.get("emails") or {}).get("received", 0)

    summary = (
        f"{total_tasks} agent task{'s' if total_tasks != 1 else ''} completed, "
        f"{sent} email{'s' if sent != 1 else ''} sent, "
        f"{received} repl{'ies' if received != 1 else 'y'} received in the last 7 days."
    )
    per_agent_lines = "\n".join(
        f"- {agent_display_name(k)}: {v} task{'s' if v != 1 else ''}"
        for k, v in sorted(agents.items(), key=lambda kv: -kv[1])
    ) or "- No agent runs recorded."
    body = (
        f"Your marketing team completed {total_tasks} tasks across the last 7 days:\n\n"
        f"{per_agent_lines}\n\n"
        f"Outbound email volume hit {sent}; "
        f"{received} repl{'ies' if received != 1 else 'y'} came back in.\n\n"
        "Review pending drafts in your Inbox, or ask the CEO chat for a "
        "recommended next step based on this week's signal."
    )
    return summary[:280], body


async def _claude_narrative(
    business_name: str, metrics: dict[str, Any],
) -> tuple[str, str]:
    """Ask Haiku to write a (summary, body) pair from the counters.

    Falls back to `_deterministic_narrative` whenever Claude is
    unreachable or returns malformed output, so the Generate button
    is never a hard 500.
    """
    import json as _json
    from backend.tools.claude_cli import call_claude, MODEL_HAIKU

    try:
        raw = await call_claude(
            _STATE_OF_UNION_SYSTEM.format(business_name=business_name or "your business"),
            f"Counters for the last 7 days:\n```json\n{_json.dumps(metrics, indent=2)}\n```",
            max_tokens=900,
            model=MODEL_HAIKU,
        )
    except Exception as e:
        logger.warning("[reports] claude narrative call failed: %s", e)
        raw = ""

    summary = ""
    body = ""
    if raw:
        # Strict format: SUMMARY: ...\n\nBODY:\n...
        for line in raw.splitlines():
            if line.upper().startswith("SUMMARY:") and not summary:
                summary = line.split(":", 1)[1].strip()
                break
        body_idx = raw.upper().find("BODY:")
        if body_idx != -1:
            body = raw[body_idx + len("BODY:"):].strip()

    if not summary or not body:
        return _deterministic_narrative(metrics)

    return summary[:280], body


# ── Public report generators ─────────────────────────────────────────


async def generate_state_of_union(tenant_id: str) -> dict[str, Any]:
    """Generate a 7-day cross-agent State of the Union report.

    Aggregates → renders agent-productivity bar chart → asks Claude to
    write the narrative → persists into marketing_reports → returns the
    full row for the frontend to display immediately.
    """
    from backend.config.loader import get_tenant_config

    start_iso, end_iso = _window_iso(7)

    cfg = get_tenant_config(tenant_id)
    business_name = (cfg.business_name or "").strip()

    tasks_by_agent = _count_agent_logs(tenant_id, start_iso)
    inbox = _count_inbox_items(tenant_id, start_iso)
    emails = _count_email_messages(tenant_id, start_iso)
    spend = _campaign_spend_totals(tenant_id)

    metrics: dict[str, Any] = {
        "tasks_by_agent": tasks_by_agent,
        "tasks_total": sum(tasks_by_agent.values()),
        "inbox": inbox,
        "emails": emails,
        "campaign_totals": spend,
        "window_days": 7,
    }

    chart = _render_agent_productivity_chart(tenant_id, tasks_by_agent)
    chart_urls: list[dict[str, str]] = [chart] if chart else []

    summary, body = await _claude_narrative(business_name, metrics)

    row = {
        "tenant_id": tenant_id,
        "report_type": "state_of_union",
        "agent": "ARIA CEO",
        "title": "State of the Union",
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
        logger.exception("[reports] insert state_of_union failed: %s", e)
        raise

    saved = (ins.data or [None])[0] or row
    return saved


async def generate_agent_productivity(tenant_id: str) -> dict[str, Any]:
    """Lightweight report: just the agent-productivity bar chart with a
    deterministic one-paragraph blurb. Cheaper than state_of_union (no
    LLM call), useful as a quick visual the founder can refresh often."""
    start_iso, end_iso = _window_iso(7)
    tasks_by_agent = _count_agent_logs(tenant_id, start_iso)
    chart = _render_agent_productivity_chart(tenant_id, tasks_by_agent)

    total = sum(tasks_by_agent.values())
    if total > 0:
        top_agent_slug, top_count = max(tasks_by_agent.items(), key=lambda kv: kv[1])
        top_display = agent_display_name(top_agent_slug)
        summary = (
            f"{total} task{'s' if total != 1 else ''} completed in 7 days — "
            f"{top_display} led with {top_count}."
        )
    else:
        summary = "No agent runs recorded in the last 7 days."

    body = (
        "Bar chart shows total tasks completed per agent across the last "
        "7 days. Tasks include any agent run that finished with status "
        "`completed` — drafts, sends, posts, plans, and image generations "
        "all count.\n\n"
        + (
            "Hover the bars in the chart to see exact counts. Use this as "
            "a rough load-balance signal: if one agent is doing 90% of "
            "the work, you may be over-relying on a single channel."
            if total > 0
            else "Once you delegate work to your agents, this chart will "
                 "fill in. Try asking the CEO chat to draft a blog post or "
                 "an email sequence to start the loop."
        )
    )

    row = {
        "tenant_id": tenant_id,
        "report_type": "agent_productivity",
        "agent": "ARIA CEO",
        "title": "Agent Productivity (7d)",
        "summary": summary,
        "body_markdown": body,
        "chart_urls": [chart] if chart else [],
        "metrics": {"tasks_by_agent": tasks_by_agent, "total": total},
        "period_start": start_iso,
        "period_end": end_iso,
    }

    sb = get_db()
    try:
        ins = sb.table("marketing_reports").insert(row).execute()
    except Exception as e:
        logger.exception("[reports] insert agent_productivity failed: %s", e)
        raise
    return (ins.data or [row])[0]


# ── List / fetch / delete ────────────────────────────────────────────


def list_reports(tenant_id: str, limit: int = 50) -> dict[str, Any]:
    sb = get_db()
    res = (
        sb.table("marketing_reports")
        .select("*")
        .eq("tenant_id", tenant_id)
        .order("created_at", desc=True)
        .limit(max(1, min(limit, 200)))
        .execute()
    )
    return {"reports": res.data or []}


def get_report(tenant_id: str, report_id: str) -> dict[str, Any] | None:
    sb = get_db()
    try:
        res = (
            sb.table("marketing_reports")
            .select("*")
            .eq("id", report_id)
            .eq("tenant_id", tenant_id)
            .single()
            .execute()
        )
        return res.data
    except Exception:
        return None


def delete_report(tenant_id: str, report_id: str) -> dict[str, Any]:
    """Delete a report. Returns {deleted, found} so the router can map
    a no-op delete (already gone / wrong tenant) to a 404 instead of a
    silent 200."""
    sb = get_db()
    res = (
        sb.table("marketing_reports")
        .delete()
        .eq("id", report_id)
        .eq("tenant_id", tenant_id)
        .execute()
    )
    return {"deleted": report_id, "found": bool(res.data)}
