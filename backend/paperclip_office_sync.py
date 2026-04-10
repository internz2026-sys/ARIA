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
   active path, but it never was — see ARIA_log.md for the post-mortem.

2. sync_agent_statuses(sio) — the Virtual Office sync.
   Maps each Paperclip agent's status (idle/running/paused) to an ARIA
   Virtual Office state and emits agent_status_change Socket.IO events
   so the walking sprites in /office reflect what's actually happening
   in Paperclip.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

from backend.orchestrator import _urllib_request, get_company_id
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

# In-memory set to skip known issues without hitting the DB every cycle
_processed_issues: set[str] = set()

# Statuses Paperclip uses to mean "the agent finished its work"
_FINISHED_STATUSES = {
    "done", "in_review", "completed", "closed", "resolved",
}


def _is_finished(status: str) -> bool:
    return bool(status) and status.lower() in _FINISHED_STATUSES


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


def _fetch_agent_output(issue_id: str, original_message: str) -> str | None:
    """GET /api/issues/{id}/comments and return the agent's reply.

    pick_agent_output handles both the user-message exclusion and the
    `[tenant_id=...` framing-prefix filter so we don't import ARIA's own
    chat wrapper as if it were the agent's reply.
    """
    raw = _urllib_request("GET", f"/api/issues/{issue_id}/comments")
    comments = normalize_comments(raw)
    return pick_agent_output(comments, exclude_text=original_message)


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


async def poll_completed_issues():
    """Check Paperclip for completed agent issues and import results to ARIA inbox.

    Hot path: this runs every 5 seconds. The in-memory `_processed_issues`
    set is the primary dedupe layer (zero DB hits per cycle for known IDs);
    the DB existence check is the cold-start safety net for restarts.
    """
    company_id = get_company_id()
    if not company_id:
        return

    _load_processed_ids_from_db()

    issues = _urllib_request("GET", f"/api/companies/{company_id}/issues")
    if not issues:
        return

    issue_list = issues if isinstance(issues, list) else issues.get("data", issues.get("issues", []))

    sb = get_db()

    # Diagnostic counters so silent skips show up in summary log lines
    finished = imported = skipped_no_tenant = skipped_no_output = 0

    for issue in issue_list:
        issue_id = issue.get("id", "")
        status = issue.get("status", "")
        raw_title = issue.get("title", "")
        # Strip tenant_id prefix from title: [uuid] actual title
        title = re.sub(r"^\[[a-f0-9-]{36}\]\s*", "", raw_title)

        if _is_finished(status):
            finished += 1

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
            _processed_issues.add(issue_id)
            skipped_no_tenant += 1
            continue

        agent_name = _extract_agent_name(issue)
        content_type = _determine_content_type(agent_name, title)

        # Pull the agent's output. The user message (issue body) gets
        # excluded so we don't re-import the user's own prompt as the
        # agent's reply.
        original_message = issue.get("body") or ""
        output = _fetch_agent_output(issue_id, original_message)

        if not output:
            output = issue.get("body") or title
            if not output or len(output) < 50:
                logger.warning(
                    f"[poller] issue {issue.get('identifier', issue_id)} has no usable output "
                    f"(status={status}, body_len={len(issue.get('body') or '')}, "
                    f"title={title[:60]!r}) — marking processed"
                )
                _processed_issues.add(issue_id)
                skipped_no_output += 1
                continue

        # Dedupe: have we already imported this Paperclip issue?
        try:
            existing = (
                sb.table("inbox_items")
                .select("id")
                .eq("tenant_id", tenant_id)
                .eq("paperclip_issue_id", issue_id)
                .limit(1)
                .execute()
            )
        except Exception:
            # Fallback if paperclip_issue_id column doesn't exist yet
            existing = (
                sb.table("inbox_items")
                .select("id")
                .eq("tenant_id", tenant_id)
                .ilike("title", f"%{title[:50]}%")
                .limit(1)
                .execute()
            )
        if existing.data:
            _processed_issues.add(issue_id)
            continue

        # Insert the inbox item
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
            result = sb.table("inbox_items").insert(row).execute()
            if result.data:
                imported += 1
                logger.warning(
                    f"[poller] imported {issue.get('identifier', issue_id)} -> "
                    f"inbox ({agent_name}, {len(output)} chars): {title[:60]}"
                )
                # Bell notification
                try:
                    sb.table("notifications").insert({
                        "tenant_id": tenant_id,
                        "title": f"New from {agent_name}: {title[:60]}",
                        "body": output[:200],
                        "category": "inbox",
                        "href": "/inbox",
                    }).execute()
                except Exception:
                    pass
            _processed_issues.add(issue_id)
        except Exception as e:
            logger.error(f"[poller] failed to import {issue_id}: {e}")

    # Emit a summary line only when there's something interesting to report
    if imported or skipped_no_tenant or skipped_no_output:
        logger.warning(
            f"[poller] cycle: {finished} finished, {imported} imported, "
            f"{skipped_no_tenant} no_tenant, {skipped_no_output} no_output"
        )


# ──────────────────────────────────────────────────────────────────────────
# Virtual Office Status Sync — sync_agent_statuses
# ──────────────────────────────────────────────────────────────────────────

# Track previous status to only emit on change
_prev_agent_status: dict[str, str] = {}


async def sync_agent_statuses(sio):
    """Poll Paperclip agent statuses and emit Virtual Office events."""
    company_id = get_company_id()
    if not company_id:
        return

    agents = _urllib_request("GET", f"/api/companies/{company_id}/agents")
    if not agents:
        return

    agent_list = agents if isinstance(agents, list) else agents.get("data", [])
    tenant_ids = _get_cached_tenant_ids()

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
            except Exception:
                pass

        if aria_status != "idle":
            logger.info(f"Virtual Office: {aria_id} → {aria_status} (from Paperclip)")
