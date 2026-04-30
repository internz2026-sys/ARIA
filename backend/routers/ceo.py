"""CEO Chat Router — /api/ceo/chat/* read + delete endpoints.

Slice 4d of the multi-batch refactor. Lifts the chat-history read
endpoints + session delete endpoints out of server.py into a
dedicated router. Slice 4c will add the POST /api/ceo/chat handler
itself (the heavy one).

Endpoints owned here:
  - GET    /api/ceo/chat/{session_id}/history       — fetch messages
  - GET    /api/ceo/chat/sessions/{tenant_id}       — list sessions
  - POST   /api/ceo/chat/sessions/{tenant_id}/bulk-delete
  - DELETE /api/ceo/chat/sessions/{tenant_id}/{session_id}

Behavior is unchanged. server.py removed the inline definitions and
includes this router via `app.include_router(ceo_router)`.

Cross-module touchpoints:
  - In-memory chat state (chat_sessions + session_locks) lives in
    backend/services/chat_state.py since slice 4a. The history
    endpoint checks the cache before going to Supabase; the delete
    endpoints drop the session's cache entry + lock alongside the
    DB row so stale state can't leak past the delete.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.chat_state import chat_sessions, session_locks
from backend.services.supabase import get_db

logger = logging.getLogger("aria.routers.ceo")

router = APIRouter(prefix="/api/ceo/chat", tags=["CEO Chat"])


@router.get("/{session_id}/history")
async def ceo_chat_history(session_id: str):
    """Get chat history for a session — loads from DB.

    Cache-first: if the session is already in the in-memory cache
    (because a recent chat turn loaded or wrote to it), return that
    directly. Falls through to a chat_messages SELECT otherwise and
    populates the cache so the next call hits the fast path.
    """
    if session_id in chat_sessions and chat_sessions[session_id]:
        return {"session_id": session_id, "messages": chat_sessions[session_id]}
    try:
        sb = get_db()
        result = (
            sb.table("chat_messages")
            .select("role,content,delegations")
            .eq("session_id", session_id)
            .order("created_at")
            .execute()
        )
        messages = [
            {
                "role": r["role"],
                "content": r["content"],
                "delegations": r.get("delegations", []),
            }
            for r in result.data
        ]
        if messages:
            chat_sessions[session_id] = messages
        return {"session_id": session_id, "messages": messages}
    except Exception as e:
        logger.debug("[ceo] history fetch failed for %s: %s", session_id, e)
        return {"session_id": session_id, "messages": []}


@router.get("/sessions/{tenant_id}")
async def list_chat_sessions(tenant_id: str):
    """List all chat sessions for a tenant, newest first."""
    try:
        sb = get_db()
        result = (
            sb.table("chat_sessions")
            .select("id,title,created_at,updated_at")
            .eq("tenant_id", tenant_id)
            .order("updated_at", desc=True)
            .execute()
        )
        return {"sessions": result.data}
    except Exception as e:
        logger.debug("[ceo] list_sessions failed for tenant %s: %s", tenant_id, e)
        return {"sessions": []}


class BulkDeleteSessionsRequest(BaseModel):
    session_ids: list[str]


@router.post("/sessions/{tenant_id}/bulk-delete")
async def bulk_delete_chat_sessions(tenant_id: str, body: BulkDeleteSessionsRequest):
    """Bulk-delete multiple chat sessions in a single Supabase round-trip.

    Uses `.in_("id", session_ids)` so the whole operation is one DELETE
    regardless of how many rows are being removed. chat_messages cascade
    via the existing ON DELETE CASCADE FK.

    Tenant-scoped: the query filters by tenant_id so a caller can't
    delete sessions that don't belong to them even if they guessed the
    ids. Idempotent on "already gone" — the response reports the count
    of rows that actually matched at delete-time so the UI can show
    accurate feedback.
    """
    sb = get_db()
    ids = [sid for sid in (body.session_ids or []) if isinstance(sid, str) and sid]
    if not ids:
        return {"ok": True, "deleted": 0}

    # Filter incoming ids down to the ones that actually belong to this
    # tenant. A forged id for another tenant is silently dropped (not
    # 403'd) so a bulk request with one bad id still processes the good
    # ones.
    owned = (
        sb.table("chat_sessions")
        .select("id")
        .eq("tenant_id", tenant_id)
        .in_("id", ids)
        .execute()
    )
    safe_ids = [r["id"] for r in (owned.data or [])]
    if not safe_ids:
        return {"ok": True, "deleted": 0}

    sb.table("chat_sessions").delete().in_("id", safe_ids).execute()

    # Drop in-memory state for deleted sessions so a stale lock or
    # cached message list can't leak past the delete.
    for sid in safe_ids:
        try:
            chat_sessions.pop(sid, None)
            session_locks.pop(sid, None)
        except Exception:
            pass

    return {"ok": True, "deleted": len(safe_ids), "deleted_ids": safe_ids}


@router.delete("/sessions/{tenant_id}/{session_id}")
async def delete_chat_session(tenant_id: str, session_id: str):
    """Hard-delete a CEO chat session.

    The chat_messages table has ON DELETE CASCADE on its session_id
    foreign key (see backend/sql/create_chat_tables.sql), so dropping
    the session row also drops every message attached to it. No
    orphan messages are left behind.

    Scoped by tenant_id so a caller can't delete another tenant's
    session even if they guessed the session_id. Also clears any
    in-process chat lock for that session_id so the next fresh
    session can take its slot without a stale mutex.
    """
    sb = get_db()

    # Verify tenant ownership before deleting — session_ids are tenant-
    # prefixed (`chat_{tenant_id}_{ts}`) but we still double-check.
    row = (
        sb.table("chat_sessions")
        .select("id,tenant_id")
        .eq("id", session_id)
        .limit(1)
        .execute()
    )
    if not row.data:
        # Idempotent: if the row is already gone, treat as success so
        # double-clicks from the UI don't surface a 404.
        return {"ok": True, "deleted": 0}
    if row.data[0].get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")

    sb.table("chat_sessions").delete().eq("id", session_id).execute()

    # Drop the in-memory session lock + history for this session so the
    # backend doesn't keep stale state around after the DB row is gone.
    try:
        chat_sessions.pop(session_id, None)
        session_locks.pop(session_id, None)
    except Exception:
        pass

    return {"ok": True, "deleted": 1}


# ──────────────────────────────────────────────────────────────────────────
# Action-result formatter — used by the CEO chat handler to render
# action execution results as readable markdown in the assistant turn.
#
# Slice 4c1 (2026-04-30) lifted this out of server.py — pure function,
# no external state, no Socket.IO. Self-contained except for its local
# nested _render_item_row helper. Safe to import directly without lazy
# resolution.
# ──────────────────────────────────────────────────────────────────────────


def _format_action_result(action_name: str, result: dict) -> str:
    """Format an action result as readable markdown for the chat response."""
    if not result:
        return ""

    # ── Error results ──
    if result.get("error"):
        return f"**Error:** {result['error']}"

    # ═══════════ READ operations ═══════════

    # ── Contacts ──
    if action_name == "read_contacts":
        contacts = result.get("contacts", [])
        total = result.get("total", len(contacts))
        if not contacts:
            return "No contacts found in your CRM."
        lines = [f"**CRM Contacts** ({total} total)\n"]
        lines.append("| Name | Email | Status | Source |")
        lines.append("|------|-------|--------|--------|")
        for c in contacts[:20]:
            lines.append(f"| {c.get('name', '—')} | {c.get('email') or '—'} | {c.get('status') or '—'} | {c.get('source') or '—'} |")
        if total > 20:
            lines.append(f"\n*Showing 20 of {total} contacts.*")
        return "\n".join(lines)

    if action_name == "read_companies":
        companies = result.get("companies", [])
        if not companies:
            return "No companies found in your CRM."
        lines = [f"**CRM Companies** ({len(companies)} total)\n"]
        lines.append("| Name | Domain | Industry | Size |")
        lines.append("|------|--------|----------|------|")
        for c in companies[:20]:
            lines.append(f"| {c.get('name', '—')} | {c.get('domain') or '—'} | {c.get('industry') or '—'} | {c.get('size') or '—'} |")
        return "\n".join(lines)

    if action_name == "read_deals":
        deals = result.get("deals", [])
        if not deals:
            return "No deals found in your pipeline."
        lines = [f"**CRM Deals** ({len(deals)} total)\n"]
        lines.append("| Title | Value | Stage |")
        lines.append("|-------|-------|-------|")
        for d in deals[:20]:
            val = d.get("value", 0)
            lines.append(f"| {d.get('title', '—')} | {f'${val:,.0f}' if val else '—'} | {d.get('stage') or '—'} |")
        return "\n".join(lines)

    if action_name == "read_inbox":
        items = result.get("items", [])
        filter_used = result.get("filter_used") or {}
        tenant_total = result.get("tenant_total")
        recent_fallback = result.get("recent_fallback") or []

        # Helper — render rows with the ID visible so the CEO can feed
        # it straight into schedule_task. The ID is what the scheduler
        # uses to link payload.inbox_item_id → the right draft; without
        # it the CEO has no way to reference a specific item.
        def _render_item_row(i: int, it: dict) -> str:
            title = it.get("title") or it.get("type") or "Item"
            iid = it.get("id") or "—"
            agent = it.get("agent") or "—"
            status = it.get("status") or "—"
            itype = it.get("type") or "—"
            return (
                f"{i}. **{title}** — {status} · {itype} · from {agent}\n"
                f"   id: `{iid}`"
            )

        if items:
            filter_note = ""
            if filter_used:
                parts = [f"{k}={v}" for k, v in filter_used.items()]
                filter_note = f" (filter: {', '.join(parts)})"
            lines = [f"**Inbox** — {len(items)} items{filter_note}\n"]
            for i, item in enumerate(items[:15], 1):
                lines.append(_render_item_row(i, item))
            return "\n".join(lines)

        # Empty — three cases, all in plain language. The CEO reads
        # whatever comes back and speaks to the user in-character;
        # never expose "tenant", "lookup", "records", or filter names.
        if recent_fallback:
            # Something exists in the inbox — just not what the strict
            # filter asked for. Show the recent list so the CEO can
            # identify the right one without having to re-query.
            lines = [
                f"Here are your {len(recent_fallback)} most recent inbox items — "
                "one of these is likely what you meant:\n",
            ]
            for i, item in enumerate(recent_fallback, 1):
                lines.append(_render_item_row(i, item))
            return "\n".join(lines)

        if tenant_total == 0:
            # Genuinely empty — no items ever.
            return "Your inbox doesn't have any drafts yet. Want me to have one of the agents create one?"

        # Inbox has items but both the filter and the fallback came
        # back empty in this moment (rare — usually a transient DB
        # blip). Keep the voice friendly and actionable.
        return (
            "I'm having a little trouble pulling up the latest drafts right now. "
            "Give me a moment and ask again, or tell me what you'd like me to check for specifically."
        )

    if action_name == "read_tasks":
        tasks = result.get("tasks", [])
        if not tasks:
            return "No tasks found."
        lines = [f"**Tasks** ({len(tasks)} total)\n"]
        lines.append("| Agent | Task | Priority | Status |")
        lines.append("|-------|------|----------|--------|")
        for t in tasks[:20]:
            lines.append(f"| {t.get('agent', '—')} | {t.get('task', '—')[:60]} | {t.get('priority') or '—'} | {t.get('status') or '—'} |")
        return "\n".join(lines)

    if action_name == "read_activities":
        activities = result.get("activities", [])
        if not activities:
            return "No CRM activities found."
        lines = [f"**CRM Activities** ({len(activities)} recent)\n"]
        for a in activities[:15]:
            ts = a.get("created_at", "")[:10] if a.get("created_at") else ""
            lines.append(f"- **{a.get('type', '—')}** — {a.get('description', '—')} ({ts})")
        return "\n".join(lines)

    if action_name == "read_email_threads":
        threads = result.get("threads", [])
        if not threads:
            return "No email threads found."
        lines = [f"**Email Threads** ({len(threads)} total)\n"]
        lines.append("| Subject | Contact | Status |")
        lines.append("|---------|---------|--------|")
        for t in threads[:20]:
            lines.append(f"| {t.get('subject') or '—'} | {t.get('contact_email') or '—'} | {t.get('status') or '—'} |")
        return "\n".join(lines)

    if action_name == "read_notifications":
        notifs = result.get("notifications", [])
        if not notifs:
            return "No notifications."
        lines = [f"**Notifications** ({len(notifs)} recent)\n"]
        for n in notifs[:15]:
            read_icon = "" if n.get("is_read") else " (unread)"
            lines.append(f"- **{n.get('title', '—')}**{read_icon} — {n.get('category', '')} — {(n.get('created_at') or '')[:10]}")
        return "\n".join(lines)

    if action_name == "read_agent_logs":
        logs = result.get("logs", [])
        if not logs:
            return "No agent logs found."
        lines = [f"**Agent Logs** ({len(logs)} recent)\n"]
        lines.append("| Agent | Action | Status | Time |")
        lines.append("|-------|--------|--------|------|")
        for l in logs[:20]:
            ts = (l.get("timestamp") or "")[:16].replace("T", " ")
            lines.append(f"| {l.get('agent_name', '—')} | {l.get('action', '—')[:40]} | {l.get('status') or '—'} | {ts} |")
        return "\n".join(lines)

    # ═══════════ CREATE operations ═══════════

    if action_name == "create_contact":
        c = result.get("contact", {})
        if c:
            return f"**Contact created:** {c.get('name', '')} ({c.get('email') or 'no email'}) — Status: {c.get('status', 'lead')}"
        return "Contact created."

    if action_name == "create_company":
        c = result.get("company", {})
        if c:
            return f"**Company created:** {c.get('name', '')}"
        return "Company created."

    if action_name == "create_deal":
        d = result.get("deal", {})
        if d:
            val = d.get("value", 0)
            return f"**Deal created:** {d.get('title', '')} — {f'${val:,.0f}' if val else 'no value'} — Stage: {d.get('stage', 'lead')}"
        return "Deal created."

    if action_name == "create_task":
        t = result.get("task", {})
        if t:
            return f"**Task created:** {t.get('task', '')} — Assigned to: {t.get('agent', '')} — Priority: {t.get('priority', 'medium')}"
        return "Task created."

    if action_name == "create_activity":
        a = result.get("activity", {})
        if a:
            return f"**Activity logged:** {a.get('type', '')} — {a.get('description', '')}"
        return "Activity logged."

    # ═══════════ UPDATE operations ═══════════

    if action_name == "update_contact":
        changes = result.get("changes", {})
        return f"**Contact updated** (ID: {result.get('updated', '—')}). Changes: {', '.join(f'{k}={v}' for k, v in changes.items() if k != 'updated_at')}"

    if action_name == "update_company":
        changes = result.get("changes", {})
        return f"**Company updated** (ID: {result.get('updated', '—')}). Changes: {', '.join(f'{k}={v}' for k, v in changes.items() if k != 'updated_at')}"

    if action_name == "update_deal":
        changes = result.get("changes", {})
        return f"**Deal updated** (ID: {result.get('updated', '—')}). Changes: {', '.join(f'{k}={v}' for k, v in changes.items() if k != 'updated_at')}"

    if action_name == "update_task_status":
        return f"**Task updated** (ID: {result.get('updated', '—')}) — New status: {result.get('new_status', '—')}"

    if action_name == "update_inbox_status":
        return f"**Inbox item updated** (ID: {result.get('updated', '—')}) — New status: {result.get('new_status', '—')}"

    if action_name == "update_email_thread":
        return f"**Email thread updated** (ID: {result.get('updated', '—')}) — New status: {result.get('new_status', '—')}"

    if action_name == "mark_notifications_read":
        count = result.get("marked_read", 0)
        return f"**Notifications marked as read:** {count}"

    # ═══════════ DELETE operations ═══════════

    if action_name in ("delete_contact", "delete_company", "delete_deal", "delete_task", "delete_inbox_item"):
        entity = action_name.replace("delete_", "").replace("_", " ").title()
        return f"**{entity} deleted** (ID: {result.get('deleted', '—')})"

    # ═══════════ Special operations ═══════════

    if action_name == "publish_social_post":
        return f"**Post published to Twitter** — Tweet ID: {result.get('tweet_id', '—')}"

    if action_name == "publish_to_linkedin":
        return f"**Post published to LinkedIn** — Post ID: {result.get('post_id', '—')}"

    if action_name == "send_email_draft":
        return f"**Email sent** to {result.get('to', '—')} — Subject: {result.get('subject', '—')}"

    if action_name == "send_whatsapp":
        return f"**WhatsApp message sent** to {result.get('to', '—')}"

    if action_name == "sync_gmail":
        return f"**Gmail synced** — {result.get('imported', 0)} new messages imported"

    if action_name == "run_agent":
        return f"**Agent `{result.get('ran', '—')}` started** — Status: {result.get('status', '—')}"

    if action_name == "draft_email_reply":
        return f"**Email reply drafted** for thread {result.get('thread_id', '—')} — sent to inbox for approval"

    if action_name == "cancel_draft":
        return f"**Draft cancelled** (ID: {result.get('updated', '—')})"

    # ═══════════ Scheduler operations ═══════════

    if action_name == "schedule_pending_draft":
        if result.get("error"):
            return f"**Couldn't queue the schedule:** {result['error']}"
        when = result.get("scheduled_at", "")
        when_human = when[:16].replace("T", " ") if when else "the requested time"
        return (
            f"**Locked in:** I'll schedule the draft for **{when_human}** "
            "the moment the Email Marketer's output lands. No need to ask me again."
        )

    if action_name == "schedule_task":
        t = result.get("task", {})
        if t:
            return f"**Task scheduled:** {t.get('title', '')} — {t.get('task_type', '')} at {t.get('scheduled_at', '')}"
        return "Task scheduled."

    if action_name == "read_scheduled_tasks":
        tasks = result.get("tasks", [])
        if not tasks:
            return "No scheduled tasks found."
        lines = [f"**Scheduled Tasks** ({len(tasks)} total)\n"]
        lines.append("| Title | Type | Scheduled At | Status |")
        lines.append("|-------|------|-------------|--------|")
        for t in tasks[:20]:
            sa = (t.get("scheduled_at") or "")[:16].replace("T", " ")
            lines.append(f"| {t.get('title', '—')} | {t.get('task_type', '—')} | {sa} | {t.get('status', '—')} |")
        return "\n".join(lines)

    if action_name == "reschedule_task":
        return f"**Task rescheduled** (ID: {result.get('updated', '—')}) — New time: {result.get('changes', {}).get('scheduled_at', '—')}"

    if action_name == "cancel_scheduled_task":
        return f"**Scheduled task cancelled** (ID: {result.get('updated', '—')})"

    if action_name == "execute_scheduled_now":
        if result.get("error"):
            return f"**Execution failed:** {result['error']}"
        return "**Task executed immediately** — check inbox/notifications for results."

    return ""


