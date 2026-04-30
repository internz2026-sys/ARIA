"""Project Stagnation Monitor.

Identifies inbox_items that have been waiting on the user for too long
("Pending Review" or "Draft" status > 24h) and surfaces them via:

  - GET /api/projects/stale endpoint (Projects page Priority Actions section)
  - CEO chat system prompt injection (so the CEO can open a session with
    "Hey, your v2 Launch project has a LinkedIn draft from yesterday...")
  - Sidebar pulse badge

Snoozing: rows with `snoozed_until > now()` are hidden until that time.
The user clicks Snooze on a stale row, the row disappears for 24h, then
reappears (the column is nulled out only when the row is acted on, not
on snooze expiry — we just compare against now() at query time).

This is a lookup service, NOT a cron job. The monitor runs on-demand
when the frontend fetches /api/projects/stale or when the CEO chat
handler builds its system prompt. No scheduler, no notifications fired
on a timer — per spec, we only nudge the user when they're already in
the app, never via push notifications "every hour".
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from backend.services.supabase import get_db

logger = logging.getLogger("aria.services.projects")

# Statuses that count as "waiting on the user". draft_pending_approval
# is the email flow; needs_review is the catch-all for social posts /
# blog posts / ad copy that the user needs to review. `ready` is here
# too because a 'ready' row that's been sitting for >24h is by
# definition unactioned. `processing` is excluded — it's not stale,
# the agent is still working on it.
_STALE_STATUSES = ("draft_pending_approval", "needs_review", "ready")

_STALE_THRESHOLD_HOURS = 24

# Cap returned rows so the CEO's prompt doesn't blow up its context
# window if a tenant has hundreds of stale drafts.
_DEFAULT_LIMIT = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


def find_stale_items(
    tenant_id: str,
    *,
    hours_old: int = _STALE_THRESHOLD_HOURS,
    limit: int = _DEFAULT_LIMIT,
) -> list[dict]:
    """Return inbox_items that are unactioned and older than `hours_old`.

    Filters:
      - tenant_id matches
      - status is one of the "waiting on user" statuses
      - created_at older than the threshold (default: 24h)
      - snoozed_until is null OR has expired (already past the wake-up)

    Sorted oldest-first so the most-buried tasks bubble to the top of
    the Priority Actions list. Returns [] on any DB hiccup so callers
    (CEO prompt, projects page) never crash on a transient outage.
    """
    if not tenant_id:
        return []
    cutoff = (_now() - timedelta(hours=hours_old)).isoformat()
    now_iso = _now().isoformat()
    try:
        sb = get_db()
        # Two passes — supabase-py's PostgREST builder doesn't expose a
        # clean OR(snoozed_until.is.null, snoozed_until.lt.now()) without
        # raw or_() string concat, which is fragile when the snoozed_until
        # column doesn't exist on a not-yet-migrated DB. Two queries +
        # in-Python merge is more resilient and bounded by `limit`.
        not_snoozed = (
            sb.table("inbox_items")
            .select("id, agent, type, title, content, status, priority, created_at, updated_at, email_draft, chat_session_id, snoozed_until")
            .eq("tenant_id", tenant_id)
            .in_("status", list(_STALE_STATUSES))
            .lt("created_at", cutoff)
            .is_("snoozed_until", "null")
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
        )
        rows = list(not_snoozed.data or [])

        # If we have headroom, also pull rows whose snooze expired —
        # those are stale again and should reappear.
        remaining = max(0, limit - len(rows))
        if remaining > 0:
            try:
                expired = (
                    sb.table("inbox_items")
                    .select("id, agent, type, title, content, status, priority, created_at, updated_at, email_draft, chat_session_id, snoozed_until")
                    .eq("tenant_id", tenant_id)
                    .in_("status", list(_STALE_STATUSES))
                    .lt("created_at", cutoff)
                    .lt("snoozed_until", now_iso)
                    .order("created_at", desc=False)
                    .limit(remaining)
                    .execute()
                )
                rows.extend(expired.data or [])
            except Exception as e:
                # snoozed_until column may not exist yet on an
                # unmigrated DB — non-fatal, just means no expired
                # snoozes to surface this cycle.
                logger.debug("[projects] expired-snooze query failed (column missing?): %s", e)
        return rows
    except Exception as e:
        logger.warning("[projects] find_stale_items failed for tenant %s: %s", tenant_id, e)
        return []


def count_recent_items(tenant_id: str, *, hours: int = 24) -> int:
    """Count inbox_items created in the last `hours`. Used to detect
    the "buried by 5+ newer tasks" condition from the spec — when the
    recent count is high AND there are stale items, the sidebar should
    pulse to remind the user that older items still need attention."""
    if not tenant_id:
        return 0
    cutoff = (_now() - timedelta(hours=hours)).isoformat()
    try:
        sb = get_db()
        res = (
            sb.table("inbox_items")
            .select("id", count="exact")
            .eq("tenant_id", tenant_id)
            .gte("created_at", cutoff)
            .neq("status", "cancelled")
            .limit(1)
            .execute()
        )
        return int(getattr(res, "count", 0) or 0)
    except Exception as e:
        logger.debug("[projects] count_recent_items failed: %s", e)
        return 0


def snooze_item(tenant_id: str, item_id: str, *, hours: int = 24) -> dict:
    """Hide a stale row from the Stagnation Monitor for the next
    `hours`. Returns {ok, snoozed_until?, error?}.

    The row is NOT marked done, deleted, or otherwise altered — only
    the snoozed_until column is set, so the user's data integrity is
    preserved per the "do not auto-mark Done" rule in the spec."""
    if not tenant_id or not item_id:
        return {"ok": False, "error": "tenant_id + item_id required"}
    snooze_until = (_now() + timedelta(hours=hours)).isoformat()
    try:
        sb = get_db()
        sb.table("inbox_items").update({
            "snoozed_until": snooze_until,
            "updated_at": _now().isoformat(),
        }).eq("id", item_id).eq("tenant_id", tenant_id).execute()
        return {"ok": True, "snoozed_until": snooze_until}
    except Exception as e:
        logger.error("[projects] snooze_item failed: %s", e)
        return {"ok": False, "error": "Database update failed"}


def format_stale_for_ceo_prompt(rows: list[dict]) -> str:
    """Render stale items as a prompt-ready block the CEO chat handler
    can append after the Recent Inbox Activity section. Returns "" when
    there are no stale rows (caller doesn't need a None-check).

    The block lists each row with id + agent + age + title so the CEO
    can reference any of them by name when it brings up the reminder
    (e.g. "your LinkedIn launch post from 2 days ago"). Capped at 8
    items in the prompt to keep token cost bounded — the rest are
    visible in the Projects page Priority Actions section."""
    if not rows:
        return ""
    lines = [
        "\n## Stale Items Awaiting Your Review (older than 24h)",
        "These are drafts your agents produced that have been sitting "
        "without your action for at least a day. If this is the start "
        "of a new chat session AND the user hasn't already asked about "
        "something specific, briefly mention 1-2 of these by name "
        "before answering — natural office check-in tone, not a "
        "formal report. If the user IS asking about something else, "
        "stay focused on their question; don't pivot to nag them.",
    ]
    now = _now()
    for r in rows[:8]:
        try:
            created = r.get("created_at") or ""
            # Supabase returns ISO-8601 with timezone; parse defensively.
            if created.endswith("Z"):
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                created_dt = datetime.fromisoformat(created)
            age_hours = max(0, int((now - created_dt).total_seconds() / 3600))
        except Exception:
            age_hours = 24
        if age_hours >= 48:
            age_str = f"{age_hours // 24}d ago"
        else:
            age_str = f"{age_hours}h ago"
        title = (r.get("title") or r.get("type") or "Item")[:80]
        agent = r.get("agent") or "—"
        status = r.get("status") or "—"
        lines.append(
            f"- id: `{r['id']}` · {title} · {age_str} · {status} · from {agent}"
        )
    return "\n".join(lines) + "\n"
