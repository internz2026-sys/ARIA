"""Paperclip Issue Poller — watches for completed agent issues and imports results to ARIA inbox.

Since Paperclip agents run in a sandboxed Claude CLI environment and cannot
make HTTP calls to ARIA's API directly, this poller bridges the gap:

1. Polls Paperclip for issues completed by agents
2. Reads the agent's output from issue comments
3. Creates inbox items in ARIA from those comments
4. Marks the issue as processed to avoid duplicates
5. Syncs agent run status to Virtual Office (running/idle)
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone

from backend.paperclip_sync import _urllib_request, get_company_id, get_paperclip_agent_id
from backend.services.supabase import get_db

logger = logging.getLogger("aria.paperclip_poller")

# In-memory set to skip known issues without hitting the DB every cycle
_processed_issues: set[str] = set()

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


def _extract_agent_name(issue: dict) -> str:
    """Extract agent name from issue body or assignee."""
    body = issue.get("body") or ""
    match = re.search(r"Agent[:\s]*(content_writer|email_marketer|social_manager|ad_strategist|ceo)", body, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    return "content_writer"


def _determine_content_type(agent_name: str, title: str) -> str:
    """Determine inbox item type based on agent and title."""
    title_lower = title.lower()
    if agent_name == "email_marketer" or "email" in title_lower:
        return "email"
    if agent_name == "social_manager" or "post" in title_lower or "tweet" in title_lower:
        return "social_post"
    if agent_name == "ad_strategist" or "ad" in title_lower or "campaign" in title_lower:
        return "ad_campaign"
    return "blog"


def _get_issue_output(issue: dict) -> str | None:
    """Get the agent's output from issue comments."""
    issue_id = issue["id"]

    comments = _urllib_request("GET", f"/api/issues/{issue_id}/comments")
    if not comments:
        return None

    comment_list = comments if isinstance(comments, list) else comments.get("data", comments.get("comments", []))

    # Find the longest comment (likely the agent's output)
    best_comment = ""
    for c in comment_list:
        body = c.get("body") or c.get("content") or ""
        if len(body) > len(best_comment):
            best_comment = body

    return best_comment if best_comment else None


def _load_processed_ids_from_db():
    """On first run, seed the in-memory set from inbox items that have a paperclip_issue_id."""
    global _processed_issues
    if _processed_issues:
        return  # already seeded
    try:
        sb = get_db()
        # Load all known paperclip issue IDs in one query
        result = sb.table("inbox_items").select("paperclip_issue_id").neq("paperclip_issue_id", None).execute()
        _processed_issues = {row["paperclip_issue_id"] for row in (result.data or []) if row.get("paperclip_issue_id")}
        logger.info(f"Seeded {len(_processed_issues)} processed Paperclip issue IDs from DB")
    except Exception as e:
        # Column might not exist yet — that's fine, we'll fall back to ilike
        logger.debug(f"Could not seed processed IDs (column may not exist): {e}")


async def poll_completed_issues():
    """Check Paperclip for completed agent issues and import results to ARIA inbox."""
    company_id = get_company_id()
    if not company_id:
        return

    # Seed in-memory cache from DB on first run (survives restarts)
    _load_processed_ids_from_db()

    # Get all issues that are done or in_review
    issues = _urllib_request("GET", f"/api/companies/{company_id}/issues")
    if not issues:
        return

    issue_list = issues if isinstance(issues, list) else issues.get("data", issues.get("issues", []))

    sb = get_db()

    for issue in issue_list:
        issue_id = issue.get("id", "")
        status = issue.get("status", "")
        raw_title = issue.get("title", "")
        # Strip tenant_id prefix from title: [uuid] actual title
        title = re.sub(r"^\[[a-f0-9-]{36}\]\s*", "", raw_title)

        # Skip already processed or non-completed issues
        if issue_id in _processed_issues:
            continue
        if status not in ("done", "in_review", "completed"):
            continue

        # Extract context from the issue
        tenant_id = _extract_tenant_id(issue)
        if not tenant_id:
            _processed_issues.add(issue_id)
            continue

        agent_name = _extract_agent_name(issue)
        content_type = _determine_content_type(agent_name, title)

        # Get the agent's output from comments
        output = _get_issue_output(issue)
        if not output:
            # No comments — use the issue body as content
            output = issue.get("body") or title
            if not output or len(output) < 50:
                _processed_issues.add(issue_id)
                continue

        # Check if we already created an inbox item for this Paperclip issue
        # Use exact paperclip_issue_id match (fast, indexed) instead of ilike on title
        try:
            existing = sb.table("inbox_items").select("id").eq("tenant_id", tenant_id).eq("paperclip_issue_id", issue_id).limit(1).execute()
        except Exception:
            # Fallback if paperclip_issue_id column doesn't exist yet
            existing = sb.table("inbox_items").select("id").eq("tenant_id", tenant_id).ilike("title", f"%{title[:50]}%").limit(1).execute()
        if existing.data:
            _processed_issues.add(issue_id)
            continue

        # Create inbox item
        try:
            inbox_status = "needs_review"
            if content_type == "email":
                inbox_status = "draft_pending_approval"

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
                logger.info(f"Imported Paperclip issue {issue.get('identifier', issue_id)} to inbox: {title[:60]}")

                # Create notification
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
            logger.error(f"Failed to import Paperclip issue {issue_id}: {e}")


# ─── Agent Status Sync — Virtual Office ─────────────────────────────────────

# Map Paperclip agent names to ARIA agent IDs
_PAPERCLIP_TO_ARIA = {
    "CEO": "ceo",
    "Content Writer": "content_writer",
    "Email Marketer": "email_marketer",
    "Social Manager": "social_manager",
    "Ad Strategist": "ad_strategist",
}

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

    # Use cached tenant IDs instead of querying DB every 5s
    tenant_ids = _get_cached_tenant_ids()

    for agent in agent_list:
        pc_name = agent.get("name", "")
        aria_id = _PAPERCLIP_TO_ARIA.get(pc_name)
        if not aria_id:
            continue

        # Map Paperclip status to ARIA Virtual Office status
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

        current_task = ""
        if aria_status == "working":
            current_task = f"Running via Paperclip"

        payload = {
            "agent_id": aria_id,
            "status": aria_status,
            "current_task": current_task,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

        # Emit to all tenants
        for tid in tenant_ids:
            try:
                await sio.emit("agent_status_change", payload, room=tid)
            except Exception:
                pass

        if aria_status != "idle":
            logger.info(f"Virtual Office: {aria_id} → {aria_status} (from Paperclip)")
