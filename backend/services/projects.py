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
import re
from datetime import datetime, timedelta, timezone

from backend.services.supabase import get_db

logger = logging.getLogger("aria.services.projects")

# Match the leading "# Campaign: <Title>" line emitted by the Ad
# Strategist's markdown template. Tolerates extra whitespace and a
# trailing "(Q2 2026)"-style parenthetical.
_CAMPAIGN_TITLE_RE = re.compile(
    r"^\s*#\s*Campaign\s*:\s*(?P<title>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Loose budget extraction — used as a metadata hint, not a hard
# requirement. Covers "$50/day", "$1,500 total", "$300 per day", etc.
_BUDGET_HINT_RE = re.compile(
    r"\$\s*(?P<amount>[\d,]+(?:\.\d+)?)\s*(?:/\s*day|per\s*day|total)?",
    re.IGNORECASE,
)

# Objective hint — pulls the value off the "**Objective:** X" markdown
# bullet so the Projects page can show "Lead Gen" / "Brand Awareness"
# next to the row without opening the full strategy.
_OBJECTIVE_HINT_RE = re.compile(
    r"^\s*[-*]\s*\*\*Objective:\*\*\s*(?P<objective>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

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


# ---------------------------------------------------------------------------
# Project task creation — Ad Strategist (and future agents) finalize a
# campaign by inserting a tasks row that points back at the inbox item
# carrying the copy-paste instructions. Lets the Projects page surface
# the campaign as a tracked deliverable instead of leaving it buried in
# the inbox.
# ---------------------------------------------------------------------------


def extract_campaign_metadata(content: str) -> dict:
    """Pull the AI-generated campaign title + budget + objective hints
    out of an Ad Strategist markdown reply. Returns a dict with at
    least `{title}`; budget/objective are best-effort and may be
    omitted. The parser is tolerant — agent output is variable, so a
    miss here just yields a less-rich Projects row, never a crash.
    """
    out: dict = {}
    if not content or not isinstance(content, str):
        return out
    m = _CAMPAIGN_TITLE_RE.search(content)
    if m:
        title = m.group("title").strip().strip("*").strip()
        # Strip wrapping brackets the agent sometimes leaves behind
        # when it forgets to fill in the placeholder.
        if title.startswith("[") and title.endswith("]"):
            title = title[1:-1].strip()
        if title and title.lower() != "campaign name":
            out["title"] = title[:200]
    bm = _BUDGET_HINT_RE.search(content)
    if bm:
        out["projected_budget"] = bm.group(0).strip()
    om = _OBJECTIVE_HINT_RE.search(content)
    if om:
        out["campaign_objective"] = om.group("objective").strip()[:120]
    return out


def create_project_task(
    tenant_id: str,
    *,
    agent: str,
    inbox_item_id: str | None,
    title: str | None = None,
    task: str | None = None,
    status: str = "to_do",
    priority: str = "medium",
    metadata: dict | None = None,
) -> dict | None:
    """Insert a tasks row that tracks a finalized agent deliverable.

    Idempotent: if a row already exists for `inbox_item_id` (and it
    isn't trash) we return the existing row instead of inserting a
    duplicate. The unique partial index in the migration is the
    backstop, but we check first to avoid a noisy 23505 in the logs
    when retries / dual-paths fire.

    Returns the row dict, or None on DB error (logged, never raised —
    the agent's deliverable is already in the inbox, so a Projects
    insert failure must not break the user-visible flow).
    """
    if not tenant_id or not agent:
        logger.warning("[projects] create_project_task: missing tenant_id or agent")
        return None
    try:
        sb = get_db()
        # Idempotency: skip if a live task already references this inbox item
        if inbox_item_id:
            try:
                existing = (
                    sb.table("tasks")
                    .select("*")
                    .eq("tenant_id", tenant_id)
                    .eq("inbox_item_id", inbox_item_id)
                    .is_("deleted_at", "null")
                    .limit(1)
                    .execute()
                )
                if existing.data:
                    logger.info(
                        "[projects] task already exists for inbox %s — skipping insert",
                        inbox_item_id,
                    )
                    return existing.data[0]
            except Exception as e:
                # Column missing on unmigrated DB — fall through to
                # insert; the unique index will reject duplicates if it
                # exists, otherwise we accept the small dup risk.
                logger.debug("[projects] idempotency lookup skipped: %s", e)

        row = {
            "tenant_id": tenant_id,
            "agent": agent,
            "task": (task or title or "")[:500],
            "title": (title or "")[:200] or None,
            "priority": priority,
            "status": status,
            "inbox_item_id": inbox_item_id,
            "metadata": metadata or {},
        }
        result = sb.table("tasks").insert(row).execute()
        created = result.data[0] if result.data else None
        if created:
            logger.info(
                "[projects] task %s created for %s/%s (inbox=%s)",
                created.get("id"), tenant_id, agent, inbox_item_id,
            )
        return created
    except Exception as e:
        # 23505 = duplicate key on the partial unique index — race
        # with a concurrent path. Re-fetch and return the winner so
        # the caller still gets a row back.
        msg = str(e)
        if "23505" in msg or "duplicate key" in msg.lower():
            try:
                sb = get_db()
                row = (
                    sb.table("tasks")
                    .select("*")
                    .eq("tenant_id", tenant_id)
                    .eq("inbox_item_id", inbox_item_id)
                    .limit(1)
                    .execute()
                )
                if row.data:
                    logger.info("[projects] task race resolved — returning existing row")
                    return row.data[0]
            except Exception:
                pass
        logger.error("[projects] create_project_task failed: %s", e)
        return None


# Inbox status -> task status mapping for the lightweight sync. We
# only fire on transitions we're confident about; ambiguous statuses
# (processing, ready) leave the task untouched so a human can drive.
_INBOX_TO_TASK_STATUS = {
    "approved": "done",
    "sent": "done",
    "completed": "done",
    "draft_pending_approval": "in_progress",
    "needs_review": "in_progress",
}


def sync_task_status_from_inbox(
    tenant_id: str,
    inbox_item_id: str,
    inbox_status: str,
) -> bool:
    """Update the Projects task row that mirrors `inbox_item_id` to
    reflect the latest inbox status. No-op when no task exists for
    the inbox item, when the mapping is undefined, or when the task
    is already in the target status. Returns True on a real write."""
    if not tenant_id or not inbox_item_id or not inbox_status:
        return False
    target_status = _INBOX_TO_TASK_STATUS.get(inbox_status)
    if not target_status:
        return False
    try:
        sb = get_db()
        existing = (
            sb.table("tasks")
            .select("id, status")
            .eq("tenant_id", tenant_id)
            .eq("inbox_item_id", inbox_item_id)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        if not existing.data:
            return False
        task = existing.data[0]
        if task.get("status") == target_status:
            return False
        sb.table("tasks").update({
            "status": target_status,
            "updated_at": _now().isoformat(),
        }).eq("id", task["id"]).execute()
        logger.info(
            "[projects] synced task %s -> %s (inbox=%s, inbox_status=%s)",
            task["id"], target_status, inbox_item_id, inbox_status,
        )
        return True
    except Exception as e:
        logger.debug("[projects] sync_task_status_from_inbox no-op: %s", e)
        return False
