"""Paperclip background loops — inbox importer + Virtual Office status sync.

This module runs in a background asyncio task started in server.py's lifespan.
It does TWO related jobs against Paperclip's API every few seconds:

1. poll_completed_issues() — the inbox importer.
   Paperclip agents run inside the claude_local sandbox which (in our setup)
   blocks outbound `curl` without manual permission prompts. The agents
   therefore CAN'T POST results back to ARIA's /api/inbox endpoint via the
   aria-backend-api skill. Instead, when an agent finishes, it writes its
   output as a comment on the Paperclip issue. This poller scrapes those
   comments and creates the corresponding inbox_items rows.
   This was deleted briefly on 2026-04-10 thinking the skill curl was the
   active path, but it never was — see docs/ARIA_log.md for the post-mortem.

2. sync_agent_statuses(sio) — the Virtual Office sync.
   Maps each Paperclip agent's status (idle/running/paused) to an ARIA
   Virtual Office state and emits agent_status_change Socket.IO events
   so the walking sprites in /office reflect what's actually happening
   in Paperclip.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from backend.orchestrator import _urllib_request, get_company_id, log_agent_action
from backend.services.paperclip_chat import normalize_comments, pick_agent_output
from backend.services.supabase import get_db

logger = logging.getLogger("aria.paperclip_office_sync")


# ─── Tenant ID cache (refreshed every 60s instead of every poll) ─────────────
_tenant_ids_cache: list[str] = []
_tenant_ids_last_refresh: float = 0
_TENANT_CACHE_TTL = 60  # seconds


def _get_cached_tenant_ids() -> list[str]:
    """Return cached tenant IDs, refreshing from DB at most once per minute."""
    global _tenant_ids_cache, _tenant_ids_last_refresh
    now = time.monotonic()
    if now - _tenant_ids_last_refresh > _TENANT_CACHE_TTL:
        try:
            sb = get_db()
            tenants = sb.table("tenant_configs").select("tenant_id").execute()
            _tenant_ids_cache = [t["tenant_id"] for t in (tenants.data or [])]
            _tenant_ids_last_refresh = now
        except Exception:
            pass  # keep stale cache on error
    return _tenant_ids_cache


# ──────────────────────────────────────────────────────────────────────────
# Inbox Importer — poll_completed_issues
# ──────────────────────────────────────────────────────────────────────────

# In-memory set to skip known issues without hitting the DB every cycle.
# Bounded to prevent unbounded growth -- when the set hits the cap, the
# oldest entries get evicted (LRU-ish via set rebuild). The 5000 cap
# corresponds to roughly a month of activity at moderate volume; beyond
# that the cold-start DB seed will catch any false-negative re-imports.
_processed_issues: set[str] = set()
_PROCESSED_ISSUES_MAX = 5000


# ─── Stall detection ────────────────────────────────────────────────────────
# Tracks the first time we saw each unfinished issue + which issues we've
# already alerted about being stalled. Both are bounded — issues that hit
# `_processed_issues` get cleaned up on next stall pass.
_issue_first_seen_at: dict[str, float] = {}
_stalled_alerted: set[str] = set()
_STALL_THRESHOLD_SECONDS = 180  # 3 minutes per spec


# Statuses where the agent is still working / not yet finished. Used by
# stall detection — we only alert on issues stuck in these states. Tied
# to Paperclip's status vocabulary; updated alongside _UNFINISHED if
# new states are added.
# Statuses that count as "agent is actively working on this" for stall
# alerts. We deliberately EXCLUDE "backlog" — Paperclip's inbox-lite
# endpoint (which agents poll for assignments) returns only
# todo/in_progress/blocked, so backlog issues never get picked up by
# any agent. Treating backlog as in-progress meant every old failed
# delegation in the queue fired a stall alert after 3min on every
# backend restart, producing waves of "Content Writer is still
# working" ghost toasts. If you genuinely want backlog tasks to be
# picked up, change their status to "todo" instead of widening this set.
_IN_PROGRESS_STATUSES = ("todo", "to_do", "in_progress", "in-progress", "running")


def _drop_from_stall_tracking(issue_id: str) -> None:
    """Drop an issue from the stall-tracking dicts. Called when an issue
    finishes or otherwise leaves the in_progress states so the dicts
    don't grow unbounded over the process lifetime."""
    _issue_first_seen_at.pop(issue_id, None)
    _stalled_alerted.discard(issue_id)


def _add_processed(issue_id: str) -> None:
    """Add an issue id to the processed set, evicting if over cap."""
    if len(_processed_issues) >= _PROCESSED_ISSUES_MAX:
        # Evict half the set in one go to amortize the cost. We can't do
        # true LRU without an OrderedDict, but the dedupe column in
        # inbox_items is the authoritative dedupe layer -- this set is
        # just a hot-path optimization.
        try:
            keep = list(_processed_issues)[_PROCESSED_ISSUES_MAX // 2:]
            _processed_issues.clear()
            _processed_issues.update(keep)
        except Exception:
            _processed_issues.clear()
    # NOTE: must call .add() on the set directly here -- a previous
    # global rename of `_processed_issues.add` -> `_add_processed` caught
    # this line too and turned it into infinite recursion. Easy mistake
    # to repeat. Don't.
    _processed_issues.add(issue_id)
    # Issue is finalized -> drop it from stall tracking so the dicts
    # stay bounded over the process lifetime.
    _drop_from_stall_tracking(issue_id)


# Statuses Paperclip uses to mean "the agent finished its work"
_FINISHED_STATUSES = {
    "done", "in_review", "completed", "closed", "resolved",
}

# Statuses Paperclip uses to mean "the run failed and won't produce
# output". Watcher checks these to fail-fast instead of polling.
_FAILED_STATUSES = {
    "failed", "cancelled", "canceled", "error", "errored",
}


def _is_finished(status: str) -> bool:
    return bool(status) and status.lower() in _FINISHED_STATUSES


def _is_failed(status: str) -> bool:
    return bool(status) and status.lower() in _FAILED_STATUSES


def _extract_tenant_id(issue: dict) -> str | None:
    """Extract tenant_id from issue title (format: [tenant_id] task description)."""
    title = issue.get("title", "")

    # Primary: extract from title prefix [uuid]
    match = re.match(r"\[([a-f0-9-]{36})\]", title)
    if match:
        return match.group(1)

    # Fallback: check body
    body = issue.get("body") or issue.get("description") or ""
    match = re.search(r"Tenant ID[:\s]*`?([a-f0-9-]{36})`?", body, re.IGNORECASE)
    if match:
        return match.group(1)

    # Fallback: any UUID in title or body
    for text in [title, body]:
        match = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", text)
        if match:
            return match.group(1)

    return None


# Map Paperclip agent display name -> ARIA agent slug. Used to figure out
# which ARIA agent owns an issue from the assignee field, which is more
# reliable than regex on the issue body.
_PAPERCLIP_TO_ARIA = {
    "CEO": "ceo",
    "Content Writer": "content_writer",
    "Email Marketer": "email_marketer",
    "Social Manager": "social_manager",
    "Ad Strategist": "ad_strategist",
    "Media Designer": "media",
    "Media": "media",
}


def _extract_agent_name(issue: dict) -> str:
    """Resolve the ARIA agent slug for an issue, preferring the assignee field.

    Order:
      1. assignee object's name (e.g. "Email Marketer") via _PAPERCLIP_TO_ARIA
      2. legacy body regex `Agent: <slug>`
      3. default to content_writer
    """
    # Nested assignee object
    assignee = issue.get("assignee") or issue.get("assigneeAgent") or {}
    if isinstance(assignee, dict):
        name = assignee.get("name") or assignee.get("displayName") or ""
        if name in _PAPERCLIP_TO_ARIA:
            return _PAPERCLIP_TO_ARIA[name]
        slug = assignee.get("slug") or assignee.get("urlKey")
        if slug:
            return slug

    # Top-level assignee name string
    name = issue.get("assigneeName") or ""
    if name in _PAPERCLIP_TO_ARIA:
        return _PAPERCLIP_TO_ARIA[name]

    # Legacy: regex on body
    body = issue.get("body") or ""
    match = re.search(
        r"Agent[:\s]*(content_writer|email_marketer|social_manager|ad_strategist|ceo|media)",
        body,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).lower()

    return "content_writer"


def _determine_content_type(agent_name: str, title: str) -> str:
    """Determine inbox item type based on agent and title."""
    title_lower = title.lower()
    if agent_name == "media" or "image" in title_lower or "banner" in title_lower:
        return "image"
    if agent_name == "email_marketer" or "email" in title_lower:
        return "email"
    if agent_name == "social_manager" or "post" in title_lower or "tweet" in title_lower:
        return "social_post"
    if agent_name == "ad_strategist" or "ad" in title_lower or "campaign" in title_lower:
        return "ad_campaign"
    return "blog"


def _fetch_agent_output(issue_id: str, original_message: str, *, expected_agent: str | None = None) -> str | None:
    """GET /api/issues/{id}/comments and return the agent's reply.

    pick_agent_output handles the user-message exclusion, the
    `[tenant_id=...` / `[wake]` framing-prefix filter, and (when
    expected_agent is given) the CEO author-skip so we don't import the
    CEO's own staging dumps as the delegated agent's reply.
    """
    raw = _urllib_request("GET", f"/api/issues/{issue_id}/comments")
    comments = normalize_comments(raw)
    return pick_agent_output(comments, exclude_text=original_message, expected_agent=expected_agent)


def _load_processed_ids_from_db():
    """On first run, seed the in-memory set from inbox items that have a paperclip_issue_id."""
    global _processed_issues
    if _processed_issues:
        return  # already seeded
    try:
        sb = get_db()
        result = sb.table("inbox_items").select("paperclip_issue_id").neq("paperclip_issue_id", None).execute()
        _processed_issues = {row["paperclip_issue_id"] for row in (result.data or []) if row.get("paperclip_issue_id")}
        logger.info(f"Seeded {len(_processed_issues)} processed Paperclip issue IDs from DB")
    except Exception as e:
        logger.debug(f"Could not seed processed IDs (column may not exist): {e}")


async def poll_completed_issues(sio=None) -> int:
    """Check Paperclip for completed agent issues and import results to ARIA inbox.

    Hot path: this runs every 5 seconds (or 30s in idle mode). The in-memory
    `_processed_issues` set is the primary dedupe layer (zero DB hits per
    cycle for known IDs); the DB existence check is the cold-start safety
    net for restarts.

    `sio` (optional) — if provided, this function also emits `task_stalled`
    Socket.IO events for issues that have been stuck in todo / in_progress
    for more than _STALL_THRESHOLD_SECONDS (3min). Each issue is alerted
    at most once per process lifetime. Without sio the stall detection is
    skipped (legacy callers).

    Returns the number of new inbox rows imported on this tick. The
    background loop uses this to decide whether to back off the polling
    interval (no work for N consecutive cycles -> 30s).
    """
    import time as _time

    company_id = get_company_id()
    if not company_id:
        return 0

    _load_processed_ids_from_db()

    issues = _urllib_request("GET", f"/api/companies/{company_id}/issues")
    if not issues:
        return 0

    issue_list = issues if isinstance(issues, list) else issues.get("data", issues.get("issues", []))

    sb = get_db()

    # Diagnostic counters so silent skips show up in summary log lines
    finished = imported = skipped_no_tenant = skipped_no_output = 0
    now_ts = _time.time()

    for issue in issue_list:
        issue_id = issue.get("id", "")
        status = issue.get("status", "")
        raw_title = issue.get("title", "")
        # Strip tenant_id prefix from title: [uuid] actual title
        title = re.sub(r"^\[[a-f0-9-]{36}\]\s*", "", raw_title)

        if _is_finished(status):
            finished += 1
            # Cleanup: a finished issue can't be stalled. Drop its
            # first-seen entry so the dict stays bounded over time.
            _issue_first_seen_at.pop(issue_id, None)

        # Stall detection — runs BEFORE the processed/finished short-
        # circuits below so a stalled-but-still-running issue surfaces
        # even when the processed set is large.
        if sio is not None and issue_id and not _is_finished(status) and not _is_failed(status):
            if (status or "").lower() in _IN_PROGRESS_STATUSES:
                first_seen = _issue_first_seen_at.setdefault(issue_id, now_ts)
                age = now_ts - first_seen
                if age >= _STALL_THRESHOLD_SECONDS and issue_id not in _stalled_alerted:
                    tenant_id = _extract_tenant_id(issue)
                    if tenant_id:
                        agent_name = _extract_agent_name(issue)
                        try:
                            await sio.emit("task_stalled", {
                                "paperclip_issue_id": issue_id,
                                "tenant_id": tenant_id,
                                "agent": agent_name,
                                "title": title[:200],
                                "stalled_seconds": int(age),
                            }, room=tenant_id)
                            _stalled_alerted.add(issue_id)
                            logger.warning(
                                "[poller] stall alert: agent=%s tenant=%s age=%ds title=%s",
                                agent_name, tenant_id, int(age), title[:60],
                            )
                        except Exception as e:
                            logger.debug("[poller] task_stalled emit failed: %s", e)

        if issue_id in _processed_issues:
            continue
        if not _is_finished(status):
            continue

        tenant_id = _extract_tenant_id(issue)
        if not tenant_id:
            logger.warning(
                f"[poller] no tenant_id in issue {issue.get('identifier', issue_id)} "
                f"(title={raw_title[:80]!r}) — marking processed"
            )
            _add_processed(issue_id)
            skipped_no_tenant += 1
            continue

        # Validate the tenant actually exists. A stale UUID in an old
        # issue title (from a tenant that's been deleted) would otherwise
        # insert orphan inbox rows that nobody can ever see.
        if tenant_id not in _get_cached_tenant_ids():
            logger.warning(
                f"[poller] tenant {tenant_id} from issue "
                f"{issue.get('identifier', issue_id)} is not an active tenant — marking processed"
            )
            _add_processed(issue_id)
            skipped_no_tenant += 1
            continue

        agent_name = _extract_agent_name(issue)
        content_type = _determine_content_type(agent_name, title)

        # Pull the agent's output. The user message (issue body) gets
        # excluded so we don't re-import the user's own prompt as the
        # agent's reply. Pass the expected agent name so pick_agent_output
        # can prefer comments authored by the right agent and skip the
        # CEO's own staging dumps.
        original_message = issue.get("body") or ""
        output = _fetch_agent_output(issue_id, original_message, expected_agent=agent_name)

        if not output:
            # CRITICAL: do NOT fall back to issue.body here. The body is
            # the user's original prompt. Importing it as "agent output"
            # makes failed runs look successful and shows the user their
            # own question echoed back. Mark as processed and skip --
            # the user can re-trigger if they want.
            logger.warning(
                f"[poller] issue {issue.get('identifier', issue_id)} has no agent reply "
                f"(status={status}, agent={agent_name}, "
                f"title={title[:60]!r}) — marking processed without import"
            )
            _add_processed(issue_id)
            skipped_no_output += 1
            continue

        # Dedupe: have we already imported this Paperclip issue?
        # Wrapped in to_thread so the supabase-py sync HTTP call doesn't
        # block the event loop. The poller runs every 5s and a single
        # blocking call here would stall every other request for the
        # duration.
        def _check_existing():
            try:
                return (
                    sb.table("inbox_items")
                    .select("id")
                    .eq("tenant_id", tenant_id)
                    .eq("paperclip_issue_id", issue_id)
                    .limit(1)
                    .execute()
                )
            except Exception:
                return (
                    sb.table("inbox_items")
                    .select("id")
                    .eq("tenant_id", tenant_id)
                    .ilike("title", f"%{title[:50]}%")
                    .limit(1)
                    .execute()
                )
        existing = await asyncio.to_thread(_check_existing)
        if existing.data:
            _add_processed(issue_id)
            continue

        # Insert the inbox item + bell notification. Both writes are
        # wrapped in to_thread so they don't block the event loop.
        try:
            inbox_status = "draft_pending_approval" if content_type == "email" else "needs_review"
            row = {
                "tenant_id": tenant_id,
                "title": title[:200],
                "content": output,
                "type": content_type,
                "agent": agent_name,
                "priority": issue.get("priority", "medium"),
                "status": inbox_status,
                "paperclip_issue_id": issue_id,
            }
            result = await asyncio.to_thread(lambda: sb.table("inbox_items").insert(row).execute())
            if result.data:
                imported += 1
                logger.warning(
                    "[poller] imported %s -> inbox (%s, %d chars): %s",
                    issue.get("identifier", issue_id), agent_name, len(output), title[:60],
                )
                notif_row = {
                    "tenant_id": tenant_id,
                    "title": f"New from {agent_name}: {title[:60]}",
                    "body": output[:200],
                    "category": "inbox",
                    "href": "/inbox",
                }
                try:
                    await asyncio.to_thread(lambda: sb.table("notifications").insert(notif_row).execute())
                except Exception:
                    pass
                # Close the lifecycle with a completion log so the Virtual
                # Office Recent Activity panel shows "task done" next to
                # the earlier paperclip_dispatch row. Without this, every
                # agent looks perpetually "starting work, never finishing".
                try:
                    await log_agent_action(
                        tenant_id, agent_name, "paperclip_completed",
                        {"task": title[:200], "paperclip_issue_id": issue_id, "chars": len(output)},
                    )
                except Exception:
                    pass
                # Mirror this row to content_library + embed into Qdrant
                # for long-term cross-session recall. Runs off the event
                # loop since both writes are synchronous.
                try:
                    from backend.services.content_index import index_inbox_row
                    inserted_row = (result.data or [{}])[0]
                    merged = {**row, **inserted_row}  # carry id + timestamps
                    await asyncio.to_thread(index_inbox_row, merged)
                except Exception as ix_err:
                    logger.debug("[poller] content_index skipped: %s", ix_err)
            _add_processed(issue_id)
        except Exception as e:
            logger.error("[poller] failed to import %s: %s", issue_id, e)

    # Emit a summary line only when there's something interesting to report
    if imported or skipped_no_tenant or skipped_no_output:
        logger.warning(
            f"[poller] cycle: {finished} finished, {imported} imported, "
            f"{skipped_no_tenant} no_tenant, {skipped_no_output} no_output"
        )

    return imported


# ──────────────────────────────────────────────────────────────────────────
# Virtual Office Status Sync — sync_agent_statuses
# ──────────────────────────────────────────────────────────────────────────

# Track previous status to only emit on change
_prev_agent_status: dict[str, str] = {}


async def sync_agent_statuses(sio) -> int:
    """Poll Paperclip agent statuses and emit Virtual Office events.

    Returns the number of agents whose status actually changed on this
    tick. The background loop uses this as a signal that something is
    happening, so it should stay in fast-poll mode rather than backing
    off to the idle interval.
    """
    company_id = get_company_id()
    if not company_id:
        return 0

    agents = _urllib_request("GET", f"/api/companies/{company_id}/agents")
    if not agents:
        return 0

    agent_list = agents if isinstance(agents, list) else agents.get("data", [])
    tenant_ids = _get_cached_tenant_ids()
    changed = 0

    for agent in agent_list:
        pc_name = agent.get("name", "")
        aria_id = _PAPERCLIP_TO_ARIA.get(pc_name)
        if not aria_id:
            continue

        pc_status = agent.get("status", "idle")
        aria_status = "idle"
        if pc_status in ("running", "active"):
            aria_status = "working"
        elif pc_status == "paused":
            aria_status = "idle"

        # Only emit if status changed
        prev = _prev_agent_status.get(aria_id)
        if prev == aria_status:
            continue
        _prev_agent_status[aria_id] = aria_status
        changed += 1

        current_task = "Running via Paperclip" if aria_status == "working" else ""
        payload = {
            "agent_id": aria_id,
            "status": aria_status,
            "current_task": current_task,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

        for tid in tenant_ids:
            try:
                await sio.emit("agent_status_change", payload, room=tid)
            except Exception as e:
                # Don't bare-except: at least log so we notice when sprite
                # updates stop reaching browsers (e.g. websocket dead, room
                # not joined yet, sio backend down).
                logger.debug(
                    "[office-sync] sio.emit agent_status_change failed for "
                    f"tenant {tid} agent {aria_id}: {type(e).__name__}: {e}"
                )

        if aria_status != "idle":
            logger.info(f"Virtual Office: {aria_id} → {aria_status} (from Paperclip)")

    return changed
