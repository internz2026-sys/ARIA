"""Daily Pulse report — a 24-hour activity snapshot for the founder.

Unlike `state_of_union` / `agent_productivity` (in `reports.py`), this
report is **deterministic only** — no Claude call, no chart, just a
template-driven markdown body that reads like a daily standup.

Aggregates four signals over the last 24 hours:

  - Tasks completed from `agent_logs` (status='completed' /
    'completed_with_warning'), grouped by agent
  - Inbox items created from `inbox_items`, grouped by status
  - Outbound + inbound `email_messages`
  - Lifetime count of `campaigns` where status = 'active' (NOT windowed —
    "active" is a current-state signal, not an event count)

Persists into `marketing_reports` with `report_type='daily_pulse'`,
`agent='ARIA CEO'`, `title='Daily Pulse'`, `chart_urls=[]`.

Public API:
  - generate_daily_pulse(tenant_id) -> dict
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.services.supabase import get_db

logger = logging.getLogger("aria.services.reports_daily_pulse")


# Mirrors AGENT_DISPLAY_NAMES in reports.py — kept local so this module
# stays decoupled from the sibling file (different owner). If a new
# agent is added to AGENT_REGISTRY, update both.
AGENT_DISPLAY_NAMES: dict[str, str] = {
    "ceo": "CEO",
    "content_writer": "Content Writer",
    "email_marketer": "Email Marketer",
    "social_manager": "Social Manager",
    "ad_strategist": "Ad Strategist",
    "media": "Media Designer",
}


def _window_iso_24h() -> tuple[str, str]:
    """Return (start_iso, end_iso) for the last 24 hours."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)
    return start.isoformat(), end.isoformat()


# ── Aggregation helpers ──────────────────────────────────────────────


def _count_tasks_by_agent(tenant_id: str, since_iso: str) -> dict[str, int]:
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
        logger.warning("[daily_pulse] agent_logs aggregation failed: %s", e)
        return {}

    counts: Counter[str] = Counter()
    for row in (res.data or []):
        if (row.get("status") or "").lower() in ("completed", "completed_with_warning"):
            counts[row.get("agent_name") or "unknown"] += 1
    return dict(counts)


def _count_inbox_by_status(tenant_id: str, since_iso: str) -> dict[str, Any]:
    """Count inbox_items created in the window, grouped by status."""
    sb = get_db()
    try:
        res = (
            sb.table("inbox_items")
            .select("status,created_at")
            .eq("tenant_id", tenant_id)
            .gte("created_at", since_iso)
            .execute()
        )
    except Exception as e:
        logger.warning("[daily_pulse] inbox_items aggregation failed: %s", e)
        return {"total": 0, "by_status": {}}

    rows = res.data or []
    by_status: Counter[str] = Counter()
    for r in rows:
        by_status[(r.get("status") or "unknown")] += 1
    return {"total": len(rows), "by_status": dict(by_status)}


def _count_emails(tenant_id: str, since_iso: str) -> dict[str, int]:
    """Count outbound (sent) + inbound (replies received) within window."""
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
        logger.warning("[daily_pulse] email_messages aggregation failed: %s", e)
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


def _count_active_campaigns(tenant_id: str) -> int:
    """Lifetime count of campaigns currently in `status='active'`. Not
    windowed — this is a current-state signal."""
    sb = get_db()
    try:
        res = (
            sb.table("campaigns")
            .select("id", count="exact")
            .eq("tenant_id", tenant_id)
            .eq("status", "active")
            .execute()
        )
    except Exception as e:
        logger.warning("[daily_pulse] campaigns aggregation failed: %s", e)
        return 0
    # supabase-py returns count on .count when count="exact" is set; fall
    # back to len(data) if the client version doesn't surface it.
    count = getattr(res, "count", None)
    if isinstance(count, int):
        return count
    return len(res.data or [])


# ── Markdown rendering ───────────────────────────────────────────────


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or f"{singular}s")


def _build_summary(metrics: dict[str, Any]) -> str:
    """One-line headline ≤200 chars, leading with the most notable signal.

    Priority: replies received → tasks completed → emails sent →
    inbox drafts → active campaigns → quiet day.
    """
    total_tasks = metrics["tasks_total"]
    sent = metrics["emails"]["sent"]
    received = metrics["emails"]["received"]
    inbox_total = metrics["inbox"]["total"]
    active_campaigns = metrics["campaigns_active"]

    if received > 0:
        s = (
            f"{received} email {_plural(received, 'reply', 'replies')} came in "
            f"over the last 24h — review and respond."
        )
    elif total_tasks > 0:
        s = (
            f"{total_tasks} agent {_plural(total_tasks, 'task')} completed in "
            f"the last 24h, {sent} {_plural(sent, 'email')} sent."
        )
    elif sent > 0:
        s = f"{sent} {_plural(sent, 'email')} sent in the last 24h."
    elif inbox_total > 0:
        s = (
            f"{inbox_total} new inbox {_plural(inbox_total, 'item')} created "
            f"in the last 24h — drafts await your review."
        )
    elif active_campaigns > 0:
        s = (
            f"Quiet day — no agent runs in 24h, but "
            f"{active_campaigns} {_plural(active_campaigns, 'campaign')} "
            f"still active."
        )
    else:
        s = "Quiet 24 hours — no agent runs, no emails, no new inbox items."
    return s[:200]


def _build_body_markdown(metrics: dict[str, Any]) -> str:
    """Render the standup-style body. Uses H3 headers + bullet sections;
    the frontend renderer is a tiny markdown subset so we keep it simple
    (no tables, no nested lists, no inline emphasis)."""
    tasks_by_agent = metrics["tasks_by_agent"]
    total_tasks = metrics["tasks_total"]
    inbox = metrics["inbox"]
    emails = metrics["emails"]
    active_campaigns = metrics["campaigns_active"]

    lines: list[str] = []

    # ── Tasks completed ──────────────────────────────────────────────
    lines.append("### Tasks completed (24h)")
    if total_tasks == 0:
        lines.append("- No agent runs completed in the last 24 hours.")
    else:
        lines.append(
            f"- Total: {total_tasks} {_plural(total_tasks, 'task')} completed"
        )
        for slug, count in sorted(tasks_by_agent.items(), key=lambda kv: -kv[1]):
            display = AGENT_DISPLAY_NAMES.get(slug, slug)
            lines.append(f"- {display}: {count} {_plural(count, 'task')}")
    lines.append("")

    # ── Inbox items ──────────────────────────────────────────────────
    lines.append("### Inbox items created (24h)")
    if inbox["total"] == 0:
        lines.append("- No new inbox items in the last 24 hours.")
    else:
        lines.append(
            f"- Total: {inbox['total']} new {_plural(inbox['total'], 'item')}"
        )
        for status, count in sorted(
            inbox["by_status"].items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"- {status}: {count}")
    lines.append("")

    # ── Email activity ───────────────────────────────────────────────
    lines.append("### Email activity (24h)")
    lines.append(f"- Sent: {emails['sent']}")
    lines.append(f"- Replies received: {emails['received']}")
    lines.append("")

    # ── Active campaigns (current state, not windowed) ───────────────
    lines.append("### Campaigns active")
    if active_campaigns == 0:
        lines.append("- No campaigns currently active.")
    else:
        lines.append(
            f"- {active_campaigns} {_plural(active_campaigns, 'campaign')} "
            f"currently running."
        )

    return "\n".join(lines).rstrip() + "\n"


# ── Public report generator ──────────────────────────────────────────


async def generate_daily_pulse(tenant_id: str) -> dict[str, Any]:
    """Generate a 24-hour Daily Pulse activity snapshot.

    Pure deterministic aggregation + template-based markdown — no chart,
    no Claude call. Persists into `marketing_reports` and returns the
    saved row (or the in-memory dict if the insert response is empty).
    """
    start_iso, end_iso = _window_iso_24h()

    tasks_by_agent = _count_tasks_by_agent(tenant_id, start_iso)
    inbox = _count_inbox_by_status(tenant_id, start_iso)
    emails = _count_emails(tenant_id, start_iso)
    active_campaigns = _count_active_campaigns(tenant_id)

    metrics: dict[str, Any] = {
        "tasks_by_agent": tasks_by_agent,
        "tasks_total": sum(tasks_by_agent.values()),
        "inbox": inbox,
        "emails": emails,
        "campaigns_active": active_campaigns,
        "window_hours": 24,
    }

    summary = _build_summary(metrics)
    body = _build_body_markdown(metrics)

    row = {
        "tenant_id": tenant_id,
        "report_type": "daily_pulse",
        "agent": "ARIA CEO",
        "title": "Daily Pulse",
        "summary": summary,
        "body_markdown": body,
        "chart_urls": [],
        "metrics": metrics,
        "period_start": start_iso,
        "period_end": end_iso,
    }

    sb = get_db()
    try:
        ins = sb.table("marketing_reports").insert(row).execute()
    except Exception as e:
        logger.exception("[daily_pulse] insert failed: %s", e)
        raise

    saved = (ins.data or [None])[0] or row
    return saved
