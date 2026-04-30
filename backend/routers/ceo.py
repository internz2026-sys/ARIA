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
