"""Socket.IO real-time infrastructure — single instance + stateless emit helpers.

Senior-dev replacement for routers/ceo.py's `from backend.server import sio`
lazy import. The `sio` instance lives here so any router can import it
normally at module load time, no circular-load risk.

What's here:
  - sio: AsyncServer instance, the singleton used by every Socket.IO
    emit / room-join / WebSocket handler in the codebase. Mounted in
    server.py via `socketio.ASGIApp(sio, other_asgi_app=app)`.
  - emit_task_completed: high-signal "agent finished" event the
    dashboard layout subscribes to for the success toast. Stateless —
    just builds a payload and emits.
  - agent_display_name: tiny helper for human-readable agent names.

What's NOT here (deliberately kept in server.py until those helpers
have a clean home):
  - _emit_agent_status: writes to the module-level _live_agent_status
    dict in server.py + persists to Supabase
  - _emit_scheduled_task_created: calls _notify which lives in
    server.py
  - _emit_sync_events: also calls _notify
"""
from __future__ import annotations

import logging

import socketio

logger = logging.getLogger("aria.services.realtime")


# ── Socket.IO singleton ──────────────────────────────────────────────────
# The whole codebase emits via this instance. server.py mounts it as
# `socketio.ASGIApp(sio, other_asgi_app=app)` and exports `socket_app` as
# the actual ASGI entry point.
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")


# ── Agent display names ──────────────────────────────────────────────────
# Used by emit_task_completed so the toast reads "Social Manager finished"
# instead of "social_manager finished".
_AGENT_DISPLAY_NAMES: dict[str, str] = {
    "ceo": "ARIA CEO",
    "content_writer": "Content Writer",
    "email_marketer": "Email Marketer",
    "social_manager": "Social Manager",
    "ad_strategist": "Ad Strategist",
    "media": "Media Designer",
}


def agent_display_name(slug: str) -> str:
    """Map an agent slug to a human-readable name. Falls back to a
    Title-Cased version of the slug for unknown agents."""
    if not slug:
        return "Agent"
    return _AGENT_DISPLAY_NAMES.get(slug) or slug.replace("_", " ").title()


# ── Agent-finished signal ────────────────────────────────────────────────
async def emit_task_completed(
    tenant_id: str,
    *,
    inbox_item_id: str,
    agent_id: str,
    title: str,
    content_type: str,
    status: str,
) -> None:
    """Emit a `task_completed` Socket.IO event so the dashboard can show
    a "Social Manager finished — View Draft" toast and the Kanban widget
    can move the row out of In Progress.

    Distinct from `inbox_new_item` / `inbox_item_updated` — those are
    low-level CRUD events the inbox page uses to refresh its list.
    `task_completed` is a higher-signal event the dashboard layout
    subscribes to for the success toast. We want exactly ONE emission
    per agent finish, not the 2-3 inbox_item_updated emissions that
    fire during a placeholder upsert.

    Best-effort — a socket hiccup never fails the underlying inbox
    save. Skip emission for placeholders (status='processing'); the
    toast should fire only once the real content has landed.
    """
    if not tenant_id or not inbox_item_id or not agent_id:
        return
    if status == "processing":
        return  # placeholders are NOT completions
    try:
        await sio.emit("task_completed", {
            "inbox_item_id": inbox_item_id,
            "tenant_id": tenant_id,
            "agent": agent_id,
            "agent_display_name": agent_display_name(agent_id),
            "title": title or "Draft ready",
            "type": content_type,
            "status": status,
        }, room=tenant_id)
    except Exception as e:
        logger.debug("[task_completed] socket emit failed: %s", e)
