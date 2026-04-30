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

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

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


# ──────────────────────────────────────────────────────────────────────────
# CEO Chat Handler — POST /api/ceo/chat
#
# Slice 4c2 (2026-04-30) lifted the ~842-line chat handler block out of
# server.py: CEOChatMessage model + _summarize_ceo_assistant_message +
# _format_history_message + _last_assistant_index + ceo_chat() wrapper +
# _ceo_chat_impl(). Behavior is unchanged.
#
# Cross-module references (lazy-imported inside the handler to avoid the
# circular-load loop where server.py would try to import this router
# while this router is still resolving its own top-level deps):
#   - sio (Socket.IO instance)
#   - _CEO_MD content + action descriptions
#   - _CRM_NOUN_RE, _CRM_VERB_RE (CRM context heuristic)
#   - _DELEGATE_BLOCK_RE, _ACTION_BLOCK_RE (regex parsers)
#   - _safe_background, _emit_agent_status, _emit_scheduled_task_created
#   - _get_supabase, _auto_title, _execute_delegation
#
# All those symbols still live in server.py at module load time, so the
# inside-function `from backend.server import (...)` resolves correctly
# every time the handler runs.
# ──────────────────────────────────────────────────────────────────────────


class CEOChatMessage(BaseModel):
    session_id: str
    message: str
    tenant_id: str = ""


def _summarize_ceo_assistant_message(content: str) -> str:
    """Compress a CEO assistant message into a one-line summary for history.

    The model is autoregressive and will copy its own prior outputs verbatim
    if it sees them in the context — that's the root cause of the
    'CEO returns a full GTM strategy review on every message' bug, and the
    'CEO uses last message's subject for a new request' bug. By replacing
    each prior CEO turn with a short tag instead of the raw content, the
    model knows the conversation flow exists but has nothing concrete to
    plagiarise.

    Args:
        content: the full CEO response text from a previous turn

    Returns:
        A bracketed one-line summary like '[CEO previously delegated to media]'
        or '[CEO: Hi! How can I help you today?]'
    """
    import re

    if not content:
        return "[CEO previously responded]"

    # Highest-signal patterns first: delegations and actions
    if "```delegate" in content:
        match = re.search(r'"agent"\s*:\s*"([\w_]+)"', content)
        agent = match.group(1) if match else "an agent"
        return f"[CEO previously delegated to {agent}]"
    if "```action" in content:
        match = re.search(r'"action"\s*:\s*"([\w_]+)"', content)
        action = match.group(1) if match else "an action"
        return f"[CEO previously executed action: {action}]"

    # Plain prose: strip markdown and take just the first non-empty line
    cleaned = content.strip()
    cleaned = re.sub(r"```[\s\S]*?```", "", cleaned)        # fenced code blocks
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)  # ATX headers
    cleaned = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", cleaned)  # bold/italic
    cleaned = re.sub(r"^\s*[-*]\s+", "", cleaned, flags=re.MULTILINE)  # bullets
    cleaned = re.sub(r"^\s*\d+\.\s+", "", cleaned, flags=re.MULTILINE)  # numbered

    first_line = ""
    for line in cleaned.split("\n"):
        line = line.strip()
        if line and line not in ("---", "***"):
            first_line = line
            break

    if not first_line:
        return "[CEO previously responded]"
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    return f"[CEO: {first_line}]"


def _format_history_message(m: dict, *, keep_verbatim: bool = False) -> str:
    """Render one prior message for the history block.

    User messages stay verbatim so the model knows what was actually asked.
    CEO assistant messages get summarised to break verbatim-copying priming
    UNLESS keep_verbatim is True, which the caller sets for the immediately
    prior CEO turn so follow-ups like 'go with number 1' have the actual
    options in context.
    """
    if m.get("role") == "user":
        return f"User: {m.get('content', '')}"
    content = m.get("content", "")
    if keep_verbatim:
        return f"CEO (previous response — keep this in mind for the user's reply): {content}"
    return _summarize_ceo_assistant_message(content)


# Threshold for keeping the most recent CEO response verbatim. Anything under
# this size is treated as a conversational reply (clarifying question, brief
# delegation announcement, short status update) and the next user message
# almost certainly refers back to its content. Anything over this size is a
# long-form artifact (GTM review, multi-section report) and including it
# verbatim primes the model to plagiarise its own prior output, which was
# the original reason _summarize_ceo_assistant_message exists.
_KEEP_VERBATIM_MAX_CHARS = 2000


def _last_assistant_index(history: list[dict]) -> int | None:
    """Index in history of the most recent assistant (CEO) message, or None."""
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "assistant":
            return i
    return None


@router.post("")
async def ceo_chat(body: CEOChatMessage):
    """Send a message to the CEO agent. The CEO reads its own .md file and all sub-agent .md files,
    then responds and may delegate tasks to sub-agents.

    Per-session asyncio.Lock prevents two concurrent requests for the same
    session_id from interleaving their session.append() calls and
    corrupting history. Without the lock, a user double-clicking send or
    having the chat open in two tabs could send 2 ceo_chat() calls that
    both call session.append(user) -> call_claude() -> session.append(assistant)
    in interleaved order, producing garbled history and possibly duplicate
    messages saved to Supabase.
    """
    # Lazy chat-state import (same circular-load concern as below).
    from backend.services.chat_state import get_session_lock as _get_chat_session_lock
    lock = _get_chat_session_lock(body.session_id)
    async with lock:
        return await _ceo_chat_impl(body)


async def _ceo_chat_impl(body: CEOChatMessage):
    # Lazy imports — these symbols still live in server.py. Module-level
    # import would create a circular load cycle (server.py imports this
    # router on app startup). By the time this handler runs, server.py
    # has fully loaded so the imports resolve.
    from backend.server import (
        sio,
        _CEO_MD,
        _CEO_ACTION_DESCRIPTIONS,
        _AGENT_MDS,
        _CRM_NOUN_RE,
        _CRM_VERB_RE,
        _CRM_TRIGGER_PHRASES,
        _DELEGATE_BLOCK_RE,
        _ACTION_BLOCK_RE,
        _safe_background,
        _emit_agent_status,
        _emit_scheduled_task_created,
        _get_supabase,
        _auto_title,
        _execute_delegation,
        _watch_and_fire_pending_schedule,
    )
    # Pull through the chat-state + chat-helpers aliases that server.py
    # also re-exports for its own backwards compatibility.
    from backend.services.chat_state import (
        chat_sessions as _chat_sessions,
        get_session_lock as _get_chat_session_lock,
        evict_old_sessions as _evict_chat_sessions,
    )
    from backend.services.chat import (
        save_message as _save_chat_message,
        parse_codeblock_json as _parse_codeblock_json,
    )
    from backend.tools.claude_cli import call_claude, MODEL_OPUS
    import json as _json

    _evict_chat_sessions()
    session = _chat_sessions.setdefault(body.session_id, [])
    is_first_message = len(session) == 0
    session.append({"role": "user", "content": body.message})

    # Persist user message to DB
    tenant_id = body.tenant_id
    _save_chat_message(body.session_id, tenant_id, "user", body.message)
    if is_first_message:
        _auto_title(body.session_id, body.message)

    # CEO is now in a meeting (processing the user's message)
    if tenant_id:
        await _emit_agent_status(tenant_id, "ceo", "running",
                                 current_task="In meeting with user",
                                 action="meeting_with_user")

    # Inject the sub-agent capabilities cheat sheet ONLY on the first message
    # of a session. Subsequent turns rely on the CEO already knowing its team
    # from earlier in the conversation. This used to fire every turn — the old
    # comment claimed it was first-message-only but the code didn't actually
    # check is_first_message. Saves ~1k tokens per non-first chat call.
    # On follow-up turns we leave a single line so the CEO doesn't forget the
    # roster entirely if the conversation history scrolls off.
    if is_first_message:
        sub_agent_context = "\n".join(
            f"- {name}: {content[:200].replace(chr(10), ' ')}"
            for name, content in _AGENT_MDS.items()
        )
    else:
        sub_agent_context = (
            "Your team: content_writer, email_marketer, social_manager, ad_strategist, media."
        )

    # Load tenant config once — reused for business context + integration checks.
    # tenant_id was already set above from body.tenant_id; don't reassign here.
    business_context = ""
    tc = None
    if tenant_id:
        try:
            tc = get_tenant_config(tenant_id)
            if tc.agent_brief:
                # ~150 tokens (pre-generated compact summary)
                business_context = f"\n## Business Context\n{tc.agent_brief}\nPositioning: {tc.gtm_playbook.positioning}\nChannels: {', '.join(tc.channels)}\n"
            else:
                # Fallback — compact fields only
                business_context = f"""
## Business Context
{tc.business_name}: {tc.product.name} — {tc.product.description}
Audience: {', '.join(tc.icp.target_titles) if tc.icp.target_titles else 'N/A'}
Positioning: {tc.gtm_playbook.positioning}
Voice: {tc.brand_voice.tone}
Channels: {', '.join(tc.channels)}
"""
        except Exception as e:
            # Log loudly so we know when the CEO is replying without
            # business context (would otherwise be a silent generic-advice bug)
            logging.getLogger("aria.ceo_chat").warning(
                "[ceo-chat] get_tenant_config(%s) failed: %s -- CEO will reply without business context",
                tenant_id, e,
            )

    # Check connected integrations for this tenant (reuse tc from above).
    # Compact one-line notes only — the CEO doesn't need a paragraph per
    # integration. Saves ~300 tokens per chat call.
    integration_lines = []
    if tenant_id and tc:
        try:
            if tc.integrations.google_access_token or tc.integrations.google_refresh_token:
                integration_lines.append(
                    f"Gmail connected ({tc.owner_email}) — to send mail, delegate to email_marketer "
                    f'with a task starting "SEND:" and include the full recipient email.'
                )
            if tc.integrations.twitter_access_token or tc.integrations.twitter_refresh_token:
                handle = tc.integrations.twitter_username or "user"
                integration_lines.append(
                    f"X/Twitter connected (@{handle}) — for social posts, delegate to social_manager. "
                    f"Output goes to Inbox for approval; never auto-publish."
                )
        except Exception as e:
            logging.getLogger("aria.ceo_chat").debug(
                "[ceo-chat] integration check failed for %s: %s", tenant_id, e,
            )
    integration_notes = ("\n" + "\n".join(integration_lines)) if integration_lines else ""

    # ── Recent activity injection ────────────────────────────────────
    # The CEO's killer failure mode: "I delegated an email, now I want
    # to schedule it" requires the CEO to know the inbox row's ID, but
    # the delegation is async — by the time the user follows up, the
    # ID exists in the DB but not in the CEO's context window. Fix:
    # on every chat turn, pre-fetch the last 5 inbox rows for this
    # tenant from the past 30 minutes and inline them into the system
    # prompt with their ids. Now "schedule THAT email" resolves without
    # the CEO needing to call read_inbox at all — the id is literally
    # visible in its own context.
    recent_activity = ""
    if tenant_id:
        try:
            _ra_sb = _get_supabase()
            _ra_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            _ra_rows = (
                _ra_sb.table("inbox_items")
                .select("id, agent, type, title, status, created_at, email_draft")
                .eq("tenant_id", tenant_id)
                .gte("created_at", _ra_cutoff)
                .order("created_at", desc=True)
                .limit(5)
                .execute()
            )
            if _ra_rows.data:
                _ra_lines = [
                    "\n## Recent Inbox Activity (last 30 min)",
                    "These are the items your team just produced. When the user says "
                    "\"it / that / the email / the draft / the last one\", pick the id "
                    "from this list — DO NOT call read_inbox again.",
                ]
                for r in _ra_rows.data:
                    title = (r.get("title") or r.get("type") or "Item")[:80]
                    agent = r.get("agent") or "—"
                    rtype = r.get("type") or "—"
                    status = r.get("status") or "—"
                    # Include recipient for email rows — helps the CEO
                    # match "schedule the Hanz email" to the right id.
                    recipient = ""
                    draft = r.get("email_draft") or {}
                    if isinstance(draft, dict) and draft.get("to"):
                        recipient = f" → {draft['to']}"
                    _ra_lines.append(
                        f"- id: `{r['id']}` · {title}{recipient} · {rtype} · {status} · from {agent}"
                    )
                recent_activity = "\n".join(_ra_lines) + "\n"
        except Exception as e:
            logging.getLogger("aria.ceo_chat").debug(
                "[ceo-chat] recent_activity fetch failed for %s: %s", tenant_id, e,
            )

    # ── Stagnation Monitor: stale items awaiting user action ──────────
    # Drafts that have been sitting in needs_review / draft_pending_approval
    # / ready for >24h and aren't currently snoozed. The CEO references
    # these on the first message of a session ("Hey, your LinkedIn draft
    # from yesterday is still waiting...") so buried tasks don't get
    # forgotten when newer work piles on top. Per spec we only nudge when
    # the user is already active in the app — this injection only fires
    # when they actually open a chat session.
    stale_items_block = ""
    if tenant_id and is_first_message:
        try:
            from backend.services.projects import find_stale_items, format_stale_for_ceo_prompt
            _stale_rows = await asyncio.to_thread(find_stale_items, tenant_id, limit=8)
            stale_items_block = format_stale_for_ceo_prompt(_stale_rows)
        except Exception as e:
            logging.getLogger("aria.ceo_chat").debug(
                "[ceo-chat] stale_items fetch failed for %s: %s", tenant_id, e,
            )

    # ── CRM context injection (only when message references contacts/deals/companies) ──
    crm_context = ""
    # Tightened heuristic: only inject CRM context when the message clearly
    # references CRM ENTITIES. Uses module-level _CRM_NOUN_RE / _CRM_VERB_RE
    # with word-boundary matching so "ideal" doesn't match "deal", "leader"
    # doesn't match "lead", and "calling" doesn't match "call". Saves
    # ~1.5k tokens per non-CRM chat call.
    _msg_lower = body.message.lower()
    _crm_match = (
        any(phrase in _msg_lower for phrase in _CRM_TRIGGER_PHRASES)
        or (_CRM_NOUN_RE.search(_msg_lower) and _CRM_VERB_RE.search(_msg_lower))
    )
    if tenant_id and _crm_match:
        try:
            _crm_sb = _get_supabase()
            # Fetch compact summaries — minimal tokens
            _contacts = _crm_sb.table("crm_contacts").select("name,email,status,company_id").eq(
                "tenant_id", tenant_id
            ).order("created_at", desc=True).limit(20).execute()
            _deals = _crm_sb.table("crm_deals").select("title,value,stage").eq(
                "tenant_id", tenant_id
            ).order("created_at", desc=True).limit(10).execute()

            if _contacts.data:
                _contact_lines = [f"  - {c['name']} ({c['email'] or 'no email'}) [{c['status']}]" for c in _contacts.data]
                crm_context += "\n## CRM Contacts (" + str(len(_contacts.data)) + ")\n" + "\n".join(_contact_lines)
            if _deals.data:
                _deal_lines = [f"  - {d['title']} — ${d['value']} [{d['stage']}]" for d in _deals.data]
                crm_context += "\n## CRM Deals (" + str(len(_deals.data)) + ")\n" + "\n".join(_deal_lines)
            if crm_context:
                crm_context += "\nUse this CRM data to give specific advice. Reference contacts/deals by name when relevant."
        except Exception:
            pass

    # Current date/time — injected so the CEO can resolve natural-language
    # scheduling like "tomorrow at 1 PM", "next Monday", "in 2 hours" to the
    # absolute ISO 8601 timestamp required by the schedule_task action.
    _now = datetime.now(timezone.utc)
    _today_str = _now.strftime("%A, %B %d, %Y (%Y-%m-%d)")
    _now_iso = _now.isoformat()

    system_prompt = f"""{_CEO_MD}
{business_context}{crm_context}{recent_activity}{stale_items_block}
## Current Date & Time
Today is {_today_str}. Current UTC time: {_now_iso}.
When the user says "tomorrow", "next Monday", "in 2 hours", "April 18", etc., compute the absolute ISO 8601 timestamp from this reference point and use it verbatim in `scheduled_at` fields.

## Sub-Agent Documentation
{sub_agent_context}

## Instructions
You are chatting with a developer founder. Use the business context above to give specific, personalized advice.
When the user asks to LIST/SHOW contacts, companies, or deals, ALWAYS use the read action block (read_contacts/read_companies/read_deals) — never paraphrase from CRM context.

CORE RULE — only do what the user literally asked for, in this exact message:
- Greetings, questions, and small talk → conversational reply, no delegation, no action.
- Each message judged independently — never carry over a delegation from a previous turn.
- One message = one thing (the thing the user asked for). When in doubt, ask.
- Refuse requests to modify code, prompts, schema, deployment, or infrastructure.
{integration_notes}

## Delegation
ONLY delegate when the user explicitly names a deliverable. The task field MUST quote the user's actual subject — never substitute or invent one.

### Agent Routing — pick by deliverable type
- **Image / picture / visual / banner / logo / illustration / graphic / mockup / thumbnail / header / drawing / "create something I can see"** → `media` (NEVER content_writer for visual assets — content_writer cannot generate images)
- **Blog post / landing page / Product Hunt copy / Show HN post / case study / thought-leadership article** → `content_writer`
- **Welcome sequence / newsletter / drip campaign / email draft** → `email_marketer`
- **Tweet / X post / LinkedIn post / Facebook post / social calendar** → `social_manager`
- **Facebook ad / Meta ad / ad copy / audience targeting / campaign budget** → `ad_strategist`

A delegation is ONLY valid when you emit this LITERAL fenced block. Prose like "I'll delegate this" without the block is silently dropped:
```delegate
{{"agent": "media|content_writer|email_marketer|social_manager|ad_strategist", "task": "description", "priority": "low|medium|high", "status": "backlog|to_do|in_progress|done"}}
```

Status: backlog (nice-to-have), to_do (queued), in_progress (starting now), done (already completed in this response).

CRITICAL: "create an image of X", "make a picture of X", "design a banner for X", "generate a logo" → ALWAYS `media`, NEVER `content_writer`. Content Writer produces TEXT only and will return a useless URL string if given an image task.

### Pipeline delegations (when the user asks for a multi-step chain)
For asks that naturally span two agents in one breath ("create a product image AND use it in a launch email", "write a blog post AND post it to social"), emit ONE delegate block with a `then` field. The dispatcher runs step 1 immediately, waits 90 seconds (configurable via `delay_seconds` on the follow-up), then runs step 2 — by which time the upstream agent's output is in the inbox and the downstream agent will find it automatically via asset_lookup (images, blog posts, email hooks).

```delegate
{{"agent": "media", "task": "product hero image: ...", "then": {{"agent": "email_marketer", "task": "launch email with the hero image", "delay_seconds": 90}}}}
```

Valid pipeline patterns:
- `media` → `email_marketer` (image-in-email)
- `media` → `social_manager` (image-in-post)
- `media` → `ad_strategist` (image-in-ad)
- `content_writer` → `email_marketer` (blog digest email)
- `content_writer` → `social_manager` (blog → thread)
- Campaign bundles (up to 6 chained steps): `media` → `content_writer` → `email_marketer` → `social_manager`, etc. When the user asks for a "launch", "campaign", or "full bundle", emit a multi-step pipeline with `delay_seconds: 60-120` between steps so each agent's output is indexed before the next runs.

This is the ONLY way to emit more than one agent in a single turn — two separate `delegate` blocks in the same reply is still a bug (it triggers the "accidentally fired two agents" alert). Use `then` for intentional chains; otherwise stick to one block.

### Referencing prior work (source_inbox_item_ids)
When the user refers to something already in the Inbox — "the banner from earlier", "my last email to Hanz", "combine the banner and the blog post we wrote yesterday" — attach the specific id(s) to the delegation via `source_inbox_item_ids`. The backend will fetch each row, extract the image URL / email subject / blog body, and append it to the task description so the sub-agent has the concrete asset alongside its own task.

How to find the id:
1. Check the "Recent Inbox Activity" block FIRST (it lists the last 5 items with ids).
2. If the referenced asset isn't in Recent Activity, call `read_inbox` with `params.search="<keyword>"` to fuzzy-match the title/content.

Example — user: "Write a LinkedIn post using the SMAPS banner from this morning"
Recent Activity shows: `id: 7af3... | type: image | title: SMAPS banner`
```delegate
{{"agent": "social_manager", "task": "LinkedIn post about SMAPS launch", "source_inbox_item_ids": ["7af3..."]}}
```

Example — user: "Turn the blog we wrote last week and the SMAPS banner into a Facebook ad"
Call `read_inbox` with `params.search="SMAPS"` to get the banner id, and `params.search="blog"` (or scan Recent Activity) for the blog id. Then:
```delegate
{{"agent": "ad_strategist", "task": "Facebook ad combining the blog talking points with the banner as hero image", "source_inbox_item_ids": ["7af3...", "blog-9c2..."]}}
```

Pass up to 5 ids per delegation. Omit the field entirely when the user is asking for fresh generation with no back-reference — the agent's own short-window lookups cover the "just made it, use it now" case.

### One Delegate Per Message — HARD RULE
Each user message gets EXACTLY ONE delegate block, never two. Do NOT chain delegations like "media for the image AND content_writer for a caption". If the user asked for ONLY an image, delegate ONLY to media. Bonus content the user did not ask for (captions, blog copy, social posts about the image) is forbidden — never auto-add a content_writer/social_manager delegate alongside a media one.

Concrete example of the violation — DO NOT DO THIS:
User: "create an image of a cat"
❌ WRONG: `{{"agent": "media", "task": "cat image", "then": {{"agent": "content_writer", "task": "blog post about cats"}}}}`  (user did not ask for a blog post)
✅ CORRECT: `{{"agent": "media", "task": "cat image"}}`

The `then` field is ONLY valid when the user's message contains an explicit compound request with a text companion word: "blog", "post", "email", "caption", "write", "social", "launch", "campaign". Absent those, NO `then` field.

If the user explicitly asks for both ("make an image AND write a caption"), still emit ONE delegate to the agent that produces the primary deliverable they named first; mention the secondary in your prose so the user can ask in a follow-up message if they want it.

If you promise agent action ("delegating", "I'll have X create", "let me get X to"), you MUST include the block in the same response.

## CEO Business Actions
Include an action block when executing business operations:
```action
{{"action": "action_name", "params": {{"key": "value"}}}}
```

Available actions:
{_CEO_ACTION_DESCRIPTIONS}

Action rules:
- Only execute actions the user explicitly requested — never chain or auto-add.
- UPDATE/DELETE/PUBLISH/SEND always require user confirmation before the block runs.
- CREATE can proceed when intent is clear; ask if data is missing.
- The system appends the formatted result automatically — write a brief intro ("Here are your contacts:") and include the block. Do NOT fabricate results.

### Create-AND-schedule in ONE turn (schedule_pending_draft)
When the user says BOTH things in the same message — "create X AND schedule it for Y" — emit TWO blocks in your reply:

1. The normal `delegate` block for the create (media / email_marketer / etc.).
2. An `action` block for `schedule_pending_draft` with `scheduled_at` (ISO 8601) and `agent` (same one you just delegated to).

The backend auto-fires the scheduled_task row the moment the sub-agent's inbox output lands — you do NOT need to wait for a follow-up turn from the user. Works for ALL sub-agents (email_marketer, content_writer, social_manager, ad_strategist, media).

Optional but helpful: pass `task_hint` with a distinctive substring from the user's ask (e.g. "Hanz", "SMAPS", "product launch"). If there are several concurrent drafts, the hint narrows the match to the right one.

Example — user says "create a marketing email for Hanz and schedule it for April 18 at 11 AM":
```delegate
{{"agent": "email_marketer", "task": "SEND: marketing email to Hanz (hdlcruz03@gmail.com) about SMAPS-SIS", "priority": "medium"}}
```
```action
{{"action": "schedule_pending_draft", "params": {{"agent": "email_marketer", "scheduled_at": "2026-04-18T11:00:00+00:00", "task_hint": "Hanz"}}}}
```

Response to the user: "Got it — I'll have the Email Marketer draft the email now and lock in April 18 at 11 AM. It'll schedule automatically the moment the draft lands. No need to remind me."

### Scheduling workflow (schedule_task / reschedule_task)
The user may ask "schedule that email for tomorrow at 1 PM", "remind me next Monday", "send this Friday 9 AM", etc.

**HARD RULE:** A scheduling request MUST be answered with a `schedule_task` action block. A prose-only reply like "Got it, I'll schedule it" will NOT create the calendar entry — the DB write only happens when the action block runs. Never confirm a schedule in words without also emitting the block in the same reply.

Example — user says "schedule the latest email for 10 AM April 18" and Recent Inbox Activity shows `id: 7af3... | title: Marketing email to Hanz (SMAPS-SIS)`:
```action
{{"action": "schedule_task", "params": {{"task_type": "send_email", "title": "Marketing email to Hanz (SMAPS-SIS)", "scheduled_at": "2026-04-18T10:00:00+00:00", "payload": {{"inbox_item_id": "7af3..."}}}}}}
```
Response: "Locked in — the Hanz email will send April 18 at 10:00 AM."

Steps:
1. Resolve the natural-language time using the "Current Date & Time" block above. Output format MUST be ISO 8601 with timezone (e.g. `2026-04-18T13:00:00+00:00`). Never use placeholders.
2. **Check the "Recent Inbox Activity" block FIRST.** If the user said "it / that / the email / the last one / the draft / the latest", pick the most recent matching row's id and schedule it immediately. You should NOT call `read_inbox` when the answer is already in your context — the activity list above is the source of truth for everything produced in the last 30 minutes.
3. Only call `read_inbox` when:
   (a) The Recent Activity block is empty (nothing happened in the last 30 min), OR
   (b) The user referenced something specific that isn't in the recent list ("the Hanz email from yesterday"). For targeted lookups use `read_inbox` with `params.search = "<name or topic>"` — it fuzzy-matches against title and content so "the Hanz one" finds the row with Hanz in it without needing the full title.
4. If exactly ONE candidate matches (either in Recent Activity or in the read_inbox result), assume that's what the user meant and schedule it. Don't ask. Disambiguation is only needed when >=2 plausible candidates exist.
5. task_type values: `send_email` (payload needs inbox_item_id), `publish_post` (payload needs inbox_item_id + platform), `reminder` (payload needs inbox_item_id + title + body).
6. If the Recent Activity block AND `read_inbox` both come back empty, the draft is likely still being written. Say (warmly, in your own words): "I'm just waiting for the draft to land in the Inbox. I'll schedule it the second it arrives — want me to go ahead and lock in the time of April 18, 11 AM so it fires the moment it's ready?" Then emit `schedule_pending_draft` with the time + agent so the backend auto-fires when the draft arrives. Take ownership; never blame a sub-agent.
7. Never fabricate an id. If read_inbox returns 2+ plausible candidates, name them briefly ("the Checking-in email to Hanz or the SMAPS-SIS demo?") and let the user pick.

### Voice + language (founder ↔ CEO)
You are speaking to a founder about their marketing team. Keep the tone peer-to-peer, warm, and concrete. DO NOT use these words in user-facing replies, ever:
- "tenant", "tenant_id", "records", "rows", "query", "lookup", "endpoint", "null", "fallback", "filter", "500ms", "Supabase", "Paperclip", "orchestrator", "payload", "cascade", "the database"
- "The Email Marketer hasn't finished" / any phrasing that blames a sub-agent. The agents are your team — speak for them.

When something goes wrong internally, rephrase it as your own temporary hiccup and offer to keep trying. Examples:
- ✅ "Give me a sec — I'm pulling up the latest drafts."
- ✅ "I'm having a little trouble accessing the latest drafts right now. Let me try again for you."
- ✅ "I can't see that draft in your Inbox yet. Want me to ask the Email Marketer to write it now, or should I keep checking?"
- ❌ "The lookup came back empty."
- ❌ "The tenant has records but the filter returned nothing."
- ❌ "Try again in a moment."

### Cross-agent: images in emails
If the user asks for an email WITH an image/photo/banner/visual, the
Email Marketer automatically attaches the most recent Media Agent image
(if one was generated in the last 30 minutes for this tenant). So:

- "Create a product launch email with a hero image" → if you generated an
  image in the last turn, delegate ONLY to email_marketer with "include
  image" in the task text. The email_marketer will find the image and
  inline it at the top of the HTML body.
- "Create an email with an image" (no prior image exists) → delegate to
  `media` FIRST to generate the image. Tell the user: "I'll create the
  image first, then you can ask me to put it in an email." Do NOT emit a
  second delegate in the same turn — one delegate per message.
- If the user pastes an image URL into chat, include it verbatim in the
  email_marketer task ("...with image: https://.../hero.png") and the
  agent will embed that exact URL.

### Email reply workflow (draft_email_reply)
When the user asks you to REPLY to an existing email ("reply to X's email", "write back to Y saying ...", "respond to the last email from Z"):

1. Call `read_email_threads` first to locate the thread. Match on contact email or, if the user references "the last reply", use the thread whose `status` is `needs_review` or has the most recent `last_message_at`.
2. Emit `draft_email_reply` with `params.thread_id` set to the matched thread's id. If the user gave specific instructions ("tell them we can meet Friday", "decline politely"), pass them as `params.custom_instructions`.
3. The draft goes to the Inbox as `draft_pending_approval`. Tell the user where to find it and that approving it will send on the ORIGINAL Gmail thread (not a new conversation).
4. Never skip step 1 — you cannot guess `thread_id`. If `read_email_threads` returns nothing matching, tell the user instead of making one up.
5. Reply requests NEVER go through the `delegate` block. `draft_email_reply` is a business action, not a sub-agent delegation.

Token efficiency:
- If the user asks to send/post content that ALREADY EXISTS in the Inbox, reference it and delegate with "USE EXISTING:" prefix instead of regenerating.
- Never auto-publish — all content goes to Inbox for approval.

Keep responses concise and actionable. You are their Chief Marketing Strategist."""

    # Build conversation for Claude — prior turns are summarised, NOT included
    # verbatim. The model is autoregressive: when it sees its own prior outputs
    # it will copy them verbatim, which causes 'CEO returns the same GTM
    # strategy review on every message' and 'CEO uses the previous turn's
    # subject for a new unrelated request'. By replacing each prior CEO turn
    # with a short tag like '[CEO previously delegated to media]', the model
    # still knows there was a back-and-forth (so follow-ups like "yes do that"
    # work) but has nothing concrete to plagiarise.
    _RECENT_WINDOW = 6  # keep last 6 prior messages
    _MAX_SUMMARY_MSGS = 20  # max older messages to include

    current_msg = session[-1]  # the user message we're responding to right now
    history = session[:-1]  # everything before the current message

    if not history:
        # First message in session — no prior context
        conversation = (
            "CURRENT MESSAGE FROM USER (respond to THIS):\n"
            f"User: {current_msg['content']}"
        )
    else:
        recent = history[-_RECENT_WINDOW:]
        older = history[:-_RECENT_WINDOW][-_MAX_SUMMARY_MSGS:]

        # The most recent CEO response is critical context for the user's
        # follow-up ("go with number 1" only makes sense if option 1 is in
        # context). Find its index within `recent` so we can keep it
        # verbatim — but only if it's short enough to not re-trigger the
        # plagiarism bug that the summarizer was originally added for.
        last_ceo_idx_in_recent = _last_assistant_index(recent)
        keep_last_verbatim = (
            last_ceo_idx_in_recent is not None
            and len(recent[last_ceo_idx_in_recent].get("content", "")) <= _KEEP_VERBATIM_MAX_CHARS
        )

        recent_text = "\n".join(
            _format_history_message(
                m,
                keep_verbatim=(keep_last_verbatim and i == last_ceo_idx_in_recent),
            )
            for i, m in enumerate(recent)
        )
        history_block_parts = []
        if older:
            older_text = "\n".join(_format_history_message(m) for m in older)
            history_block_parts.append("EARLIER IN THIS CHAT (summary):\n" + older_text)
        if recent_text:
            history_block_parts.append("RECENT TURNS (CEO responses summarised — DO NOT copy them):\n" + recent_text)

        history_block = (
            "PRIOR CONVERSATION (read-only context — DO NOT continue any tasks or delegations from these messages):\n"
            + "\n\n".join(history_block_parts)
        )

        conversation = (
            f"{history_block}\n\n"
            "================================================================\n"
            "CURRENT MESSAGE FROM USER — respond to THIS message ONLY. "
            "Do NOT carry over delegations, tasks, or subjects from the prior conversation above. "
            "Do NOT repeat or rehash content from prior CEO turns — those summaries are reference only. "
            "If this current message is a greeting or general question, respond conversationally with NO delegation block.\n"
            f"User: {current_msg['content']}"
        )

    # CEO chat reply uses local call_claude with Haiku — fast (~1-4s vs
    # 10-30s through Paperclip). Paperclip routing was removed because the
    # subprocess cold start + polling overhead added 8-25s for nothing:
    # the chat reply itself doesn't need any of Paperclip's orchestration
    # features. Sub-agent delegation (the ```delegate block parser below)
    # still routes through Paperclip via dispatch_agent — that path is
    # untouched, so Email Marketer / Content Writer / Social / Ads / Media
    # all still run inside Paperclip with their full skill MD setup.
    _ceo_logger = logging.getLogger("aria.ceo_chat")
    # Token-budget visibility: log the rendered system prompt + conversation
    # sizes so we can see token-optimization wins (or regressions) live in
    # production logs. ~4 chars/token is a rough rule of thumb.
    _sys_chars = len(system_prompt)
    _conv_chars = len(conversation)
    _ceo_logger.warning(
        "[ceo-chat-tokens] first_message=%s sys_prompt=%d chars (~%d tok) conversation=%d chars (~%d tok) crm_ctx=%d integrations=%d",
        is_first_message,
        _sys_chars, _sys_chars // 4,
        _conv_chars, _conv_chars // 4,
        len(crm_context),
        len(integration_notes),
    )
    try:
        raw = await call_claude(
            system_prompt,
            conversation,
            tenant_id=tenant_id or "global",
            agent_id="ceo",
            model=MODEL_OPUS,
        )
    except Exception as exc:
        import traceback
        _ceo_logger.error(f"CEO chat error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        # Don't leak raw exception messages (may include API keys, connection
        # strings, JWT bits). Generic message + log the real error.
        raw = (
            "I had trouble processing that just now. Please try again in a moment "
            "-- if it keeps failing, check the backend logs for the error details."
        )

    # Check for forbidden requests. The check is intentionally narrow:
    # we only override the CEO's reply if (a) the user message clearly
    # asks for a forbidden action AND (b) the CEO's response doesn't
    # already contain a refusal phrase. The double gate prevents the
    # naive substring match from nuking legitimate replies that happen
    # to mention sensitive words ("don't touch the database schema").
    from backend.ceo_actions import is_forbidden_request, REFUSAL_MESSAGE
    if is_forbidden_request(body.message):
        refusal_markers = ("can't", "cannot", "don't have access", "i'm not able", "i won't")
        raw_lower = raw.lower()
        if not any(marker in raw_lower for marker in refusal_markers):
            raw = REFUSAL_MESSAGE

    # Parse delegation blocks.
    #
    # Pipeline support: a delegate block can carry an optional `then`
    # field pointing at a follow-up delegation. The CEO uses this for
    # media -> email / media -> social / content_writer -> email kinds
    # of chains in a single turn — the prompt's "one delegate per
    # message" rule treats a pipeline as ONE intentional delegation
    # even though it produces multiple sub-agent runs.
    #
    # Shape the CEO emits for a chain:
    #   {"agent":"media","task":"...", "then":{"agent":"email_marketer","task":"...","delay_seconds":90}}
    #
    # We flatten that into sequential entries on the `delegations` list,
    # each tagged with `_delay_seconds` (cumulative) so the dispatcher
    # below knows when each step should fire. Later steps rely on the
    # earlier steps' outputs being already indexed — asset_lookup's
    # get_latest_image_url / get_recent_blog_post / get_recent_email_hook
    # will find them once the inbox row has landed.
    _VALID_AGENTS = ("content_writer", "email_marketer", "social_manager", "ad_strategist", "media")
    delegations = []
    clean_response = raw
    if "```delegate" in raw:
        for block in _DELEGATE_BLOCK_RE.findall(raw):
            d = _parse_codeblock_json(block, "delegate")
            if not d or d.get("agent") not in _VALID_AGENTS:
                continue
            # Unroll the .then chain. Cap at 6 steps so a malformed
            # response can't produce an arbitrarily long pipeline — 6 is
            # the high end of a realistic campaign bundle (hero image +
            # blog + landing page + launch email + 2-3 social posts).
            chain: list[dict] = []
            current = d
            for _ in range(6):
                chain.append({k: v for k, v in current.items() if k != "then"})
                nxt = current.get("then")
                if not isinstance(nxt, dict) or nxt.get("agent") not in _VALID_AGENTS:
                    break
                current = nxt
            # Guard: trim auto-chained follow-ups when the user asked for
            # a single deliverable. The CEO prompt forbids chaining
            # content_writer/social_manager onto a media delegation when
            # the user only asked for an image, but the model violates
            # that rule fairly often — adding a caption, blog post, or
            # social variant the user never requested. Detect image-only
            # intent in the user message and drop chain[1:] in that case.
            if len(chain) > 1 and chain[0].get("agent") == "media":
                msg_lower = (body.message or "").lower()
                _IMAGE_WORDS = ("image", "picture", "photo", "banner", "logo",
                                "illustration", "graphic", "mockup", "thumbnail",
                                "header", "visual", "drawing", "artwork", "png",
                                "jpg", "jpeg")
                _TEXT_COMPANIONS = ("blog", "post", "email", "caption", "tweet",
                                    "social", "write ", "draft ", "newsletter",
                                    " ad ", "launch", "campaign", "bundle",
                                    " also", " plus ", " then ", "description",
                                    " copy ", "content", "article")
                has_image = any(w in msg_lower for w in _IMAGE_WORDS)
                has_companion = any(w in msg_lower for w in _TEXT_COMPANIONS)
                if has_image and not has_companion:
                    dropped = [s.get("agent") for s in chain[1:]]
                    logging.getLogger("aria.ceo_chat").warning(
                        "[delegate-guard] trimming media->%s chain for image-only ask: %r",
                        "+".join(dropped), body.message[:120],
                    )
                    chain = chain[:1]
            # Pipeline image-flag: when any earlier step in the chain is
            # `media`, tag subsequent steps so the downstream agents
            # pull the freshly-generated image even if the CEO's task
            # text didn't explicitly say "image". Without this, a chain
            # like `media -> social_manager` only attaches the image
            # when the social task happened to contain the word "image".
            saw_media = False
            cumulative = 0
            for i, step in enumerate(chain):
                if i > 0:
                    cumulative += int(step.get("delay_seconds") or 90)
                step["_delay_seconds"] = cumulative
                if saw_media and step.get("agent") != "media":
                    step["_pipeline_has_media_image"] = True
                if step.get("agent") == "media":
                    saw_media = True
                delegations.append(step)
        clean_response = _DELEGATE_BLOCK_RE.sub("", raw).strip()

    # Defensive: if the model promised delegation in prose but forgot the block,
    # log loudly so we can see when the prompt isn't being followed.
    if not delegations:
        prose_promises = ("delegating", "i'll delegate", "i will delegate", "let me delegate",
                          "i'll have", "i will have", "having our", "media designer to create",
                          "media designer to generate")
        raw_lower = raw.lower()
        if any(phrase in raw_lower for phrase in prose_promises):
            logging.getLogger("aria.ceo_chat").warning(
                "CEO promised delegation in prose but emitted no ```delegate block. Raw response: %s",
                raw[:500],
            )

    # Parse CEO action blocks
    ceo_actions = []
    if "```action" in clean_response:
        for block in _ACTION_BLOCK_RE.findall(clean_response):
            a = _parse_codeblock_json(block, "action")
            if a and a.get("action"):
                ceo_actions.append(a)
        clean_response = _ACTION_BLOCK_RE.sub("", clean_response).strip()

    # Execute non-confirmation actions immediately; queue confirmations for frontend
    action_results = []
    pending_confirmations = []
    if ceo_actions and tenant_id:
        from backend.ceo_actions import execute_action, ACTION_REGISTRY
        for a in ceo_actions:
            action_name = a.get("action", "")
            params = a.get("params", {})
            # Auto-inject session_id for actions that need it — the CEO
            # doesn't know its own session_id, so we stamp it in at
            # dispatch time. Currently only schedule_pending_draft uses
            # it (to scope pending schedules to this specific chat).
            if action_name == "schedule_pending_draft" and body.session_id:
                params = {**params, "session_id": body.session_id}
            try:
                result = await execute_action(tenant_id, action_name, params, confirmed=False)
            except Exception as exec_exc:
                # Don't let an action handler crash kill the whole chat
                # response after the CEO has already replied. Log it,
                # surface a sanitized error to the user, and keep going.
                logging.getLogger("aria.ceo_chat.actions").error(
                    "[ceo-action] %s raised: %s", action_name, exec_exc, exc_info=True,
                )
                action_results.append({
                    "status": "error",
                    "action": action_name,
                    "message": f"Action {action_name!r} failed -- check backend logs.",
                })
                continue
            if result.get("status") == "needs_confirmation":
                pending_confirmations.append(result)
            else:
                action_results.append(result)

                # Calendar sync: when the CEO's action actually inserted
                # a scheduled_tasks row, fire the socket event so the
                # Calendar page refetches immediately. Handles the direct
                # `schedule_task` create path; the pending-schedule
                # watcher fires its own emit from
                # _watch_and_fire_pending_schedule.
                if action_name == "schedule_task" and isinstance(result, dict):
                    task_payload = result.get("result", {}).get("task") if "result" in result else result.get("task")
                    if task_payload:
                        await _emit_scheduled_task_created(tenant_id, task_payload)

    # Append formatted action results to the response so data appears in chat
    for ar in action_results:
        if ar.get("status") not in ("executed", "error"):
            continue
        action_name = ar.get("action", "")
        data = ar.get("result", {}) if ar["status"] == "executed" else {"error": ar.get("message", "Unknown error")}
        formatted = _format_action_result(action_name, data)
        if formatted:
            clean_response = clean_response.rstrip() + "\n\n" + formatted

    # Persist delegations on the in-memory turn too, not just in the DB.
    # The /history endpoint prefers the in-memory cache for speed, so if
    # we only wrote delegations to Postgres the user would see the
    # delegation chips on the initial reply but lose them on refresh.
    session.append({
        "role": "assistant",
        "content": clean_response,
        "delegations": delegations or [],
    })

    # Persist assistant message to DB
    _save_chat_message(body.session_id, tenant_id, "assistant", clean_response, delegations)

    # No delegations — CEO meeting is over, return to idle
    if not delegations and tenant_id:
        await _emit_agent_status(tenant_id, "ceo", "idle",
                                 action="chat_response_sent")

    # Save delegations as tasks, emit status events, and execute in background.
    #
    # Delegations tagged with `_delay_seconds > 0` are pipeline follow-up
    # steps — we defer their dispatch (task row insert, status emit, and
    # agent run) until the delay expires so the upstream agent has time
    # to land its output in the inbox first. This is what makes
    # "media -> email" work as a single-turn chain: the email step runs
    # after the media step's image row is already queryable.
    saved_tasks: list[dict] = []

    for d in delegations:
        delay = int(d.get("_delay_seconds") or 0)
        if delay > 0:
            # Pipeline follow-up — run the whole body in the background
            # after the delay so the HTTP response doesn't block. The
            # `saved_tasks` list doesn't catch the follow-up's task row
            # (it fires after the HTTP response), which is fine — the
            # Kanban UI picks it up via the socket event anyway.
            _safe_background(
                _execute_delegation(tenant_id, body.session_id, d, delay, None),
                label=f"pipeline-{d.get('agent')}-{delay}s",
            )
        else:
            await _execute_delegation(tenant_id, body.session_id, d, 0, saved_tasks)

    response_data = {
        "response": clean_response,
        "delegations": delegations,
        "tasks": saved_tasks,
        "session_id": body.session_id,
    }

    # Include action results and pending confirmations
    if action_results:
        response_data["action_results"] = action_results
    if pending_confirmations:
        response_data["pending_confirmations"] = pending_confirmations

    return response_data
