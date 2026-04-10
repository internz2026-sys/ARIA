"""Paperclip Office Sync — pushes Paperclip agent run status to the Virtual Office.

This module used to also import completed Paperclip issues into ARIA's
inbox (via poll_completed_issues), but that path was redundant: agents
on the claude_local adapter already POST their results back to
/api/inbox/{tenant_id}/items via the aria-backend-api skill (Path A).
Two write paths kept fighting each other and producing duplicates, so
the inbox importer was removed and only the office-sync poller remains.

What's still here:
- sync_agent_statuses(sio): every 5 seconds, GET Paperclip's
  /api/companies/{id}/agents, map each agent's status to an ARIA
  Virtual Office state ("idle"|"working"), and emit Socket.IO
  agent_status_change events to every tenant room. This is what
  powers the walking sprites in /office when a Paperclip run is
  active.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from backend.orchestrator import _urllib_request, get_company_id
from backend.services.supabase import get_db

logger = logging.getLogger("aria.paperclip_poller")

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


# ─── Agent Status Sync — Virtual Office ─────────────────────────────────────

# Map Paperclip agent display names to ARIA agent IDs
_PAPERCLIP_TO_ARIA = {
    "CEO": "ceo",
    "Content Writer": "content_writer",
    "Email Marketer": "email_marketer",
    "Social Manager": "social_manager",
    "Ad Strategist": "ad_strategist",
    "Media Designer": "media",
    "Media": "media",
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
            current_task = "Running via Paperclip"

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
