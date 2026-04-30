"""In-memory state for CEO chat sessions.

Slice 4a of the multi-batch refactor. Lifts the chat-session dict +
per-session lock dict + LRU helpers out of server.py (where they were
sandwiched between unrelated handlers) into a dedicated module.

Behavior is identical — just the file location moved. server.py
imports these symbols and re-binds them under the original
underscore-prefixed names so every existing call site keeps working
without further edits.

Why these belong together:
  - chat_sessions caches the conversation history per session_id so
    `_ceo_chat_impl` can build the prompt without re-fetching from the
    DB on every turn.
  - session_locks holds an asyncio.Lock per session_id so two
    concurrent ceo_chat requests for the same session_id can't
    interleave their session.append(user) / session.append(assistant)
    calls and corrupt the conversation history. Lock lifecycle
    follows chat_sessions exactly — when a session is evicted from
    the cache, its lock is dropped too.
  - evict_old_sessions enforces the cache cap. Called from
    `_ceo_chat_impl` at the top of every chat turn.

Both dicts are bounded; the cap (MAX_CACHED_SESSIONS) was chosen so
typical multi-tenant load fits in memory without paging old chats
out mid-conversation. Adjust here, not at every call site.
"""
from __future__ import annotations

import asyncio


# ── Cache cap ─────────────────────────────────────────────────────────────
# Roughly N tenants × 1-2 active chats each. Higher than necessary on
# purpose — eviction shouldn't fight an active conversation. Bumped if
# memory pressure becomes an issue.
MAX_CACHED_SESSIONS = 100


# ── In-memory chat history ────────────────────────────────────────────────
# Keyed by session_id. Each value is the conversation list of
# {"role": "user"|"assistant", "content": "...", "delegations": [...]}
# entries. Loaded from chat_messages on first access via the history
# endpoint, then mutated in-place per turn.
chat_sessions: dict[str, list[dict]] = {}


# ── Per-session locks ─────────────────────────────────────────────────────
# Keyed by session_id. asyncio.Lock instances are created lazily by
# get_session_lock() so we don't allocate locks for sessions that
# never receive a chat turn.
session_locks: dict[str, "asyncio.Lock"] = {}


def get_session_lock(session_id: str) -> "asyncio.Lock":
    """Return the asyncio.Lock for this session_id, creating one if
    needed. Safe to call from any coroutine on the main event loop —
    dict.setdefault is atomic in CPython for the GIL-protected
    modify-or-create case.
    """
    lock = session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        session_locks[session_id] = lock
    return lock


def evict_old_sessions() -> None:
    """Drop the oldest sessions if the cache exceeds MAX_CACHED_SESSIONS.

    Insertion-order eviction (Python dicts preserve insertion order
    since 3.7). True LRU would require an OrderedDict + move_to_end on
    every access — overkill for this workload. The DB is the
    authoritative store for chat_messages anyway; eviction just means
    the next history fetch for that session reads from Supabase
    instead of the in-memory cache.
    """
    if len(chat_sessions) > MAX_CACHED_SESSIONS:
        excess = len(chat_sessions) - MAX_CACHED_SESSIONS
        for key in list(chat_sessions.keys())[:excess]:
            del chat_sessions[key]
            # Drop the lock too — the session is gone from cache, so
            # any new chat turn for it will create a fresh lock.
            session_locks.pop(key, None)
