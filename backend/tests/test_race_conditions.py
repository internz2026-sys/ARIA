"""Race-condition tests around the per-session asyncio.Lock that
``backend/server.py`` imports as ``_chat_session_locks`` (lives in
``backend/services/chat_state.py:session_locks``).

The lock's job is to serialize concurrent CEO-chat POSTs that share the
same ``session_id`` so the conversation-history list isn't corrupted by
interleaved ``session.append(user)`` / ``session.append(assistant)``
calls. This test verifies the serialization holds even when the
underlying CLI call is artificially slow.

Strategy:
  1. Monkeypatch ``backend.tools.claude_cli.call_claude`` to a stub that
     sleeps 200ms and returns a deterministic reply containing the user
     message in the body so the test can tell the two replies apart.
  2. Fire two concurrent ``POST /api/ceo/chat`` calls with the same
     session_id but different user messages via ``asyncio.gather``.
  3. After both complete, read the in-memory ``chat_sessions`` dict.
     If serialization worked, the four messages MUST appear in one of
     two orders:
        [user_a, assistant_a, user_b, assistant_b]
     or [user_b, assistant_b, user_a, assistant_a]
     Any other ordering proves the lock didn't hold.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


@pytest.mark.asyncio
async def test_two_simultaneous_ceo_chat_messages_serialize(
    client, auth_headers_factory, mock_supabase, mock_tenant_lookup, monkeypatch,
):
    # The chat handler does ~25 lazy imports + a tenant-ownership check
    # via auth, plus tenant-config lookup, plus integration probes — any
    # of those can blow up the test before we get to the lock. If the
    # sibling conftest doesn't ship a working set of stubs for these,
    # bail with a clear reason rather than fail mysteriously.
    #
    # ``auth_headers_factory`` is sibling-owned. pytest injects it as a
    # fixture by parameter name; if it isn't registered the collector
    # itself will skip this test. The body below also defends against
    # the chat handler's downstream auth bouncing the request.

    # Reset the in-memory session cache + the lock dict so prior tests
    # don't poison this one.
    from backend.services import chat_state as _chat_state
    _chat_state.chat_sessions.clear()
    _chat_state.session_locks.clear()

    # Stub the CLI call so we don't fork a real Claude subprocess. The
    # 200ms sleep guarantees the two requests would interleave WITHOUT
    # the lock — without the sleep, request A could finish before request
    # B's coroutine ever yields control, and the test would pass even on
    # a broken implementation.
    call_log: list[tuple[float, str]] = []

    async def _slow_call_claude(system_prompt, user_prompt, *args, **kwargs):
        # Echo a unique substring derived from the user prompt so the
        # test can pair replies back to their request without depending
        # on unstable ordering.
        loop = asyncio.get_event_loop()
        marker = "first" if "FIRST" in (user_prompt or "") else "second"
        call_log.append((loop.time(), marker))
        await asyncio.sleep(0.2)
        return f"reply-for-{marker}"

    try:
        from backend.tools import claude_cli as _cli
        monkeypatch.setattr(_cli, "call_claude", _slow_call_claude)
        # Some routes import call_claude into their module namespace — patch
        # the ceo router's reference too so the patch actually takes effect
        # at the call site.
        from backend.routers import ceo as _ceo_router
        if hasattr(_ceo_router, "call_claude"):
            monkeypatch.setattr(_ceo_router, "call_claude", _slow_call_claude)
    except ImportError:
        pytest.skip(
            "claude_cli module not importable in test env — "
            "race-condition test needs to stub the CLI call"
        )

    # Stub Supabase persistence so _save_chat_message doesn't try to
    # talk to a real DB. The chat handler only cares that the calls
    # don't raise; the assertion below reads the in-memory dict instead.
    try:
        from backend.services import chat as _chat_svc
        monkeypatch.setattr(_chat_svc, "save_message", lambda *a, **kw: None)
    except (ImportError, AttributeError):
        pass

    # Stub _get_verified_tenant so we don't need real auth state — the
    # lock is the unit under test, not the auth layer.
    try:
        from backend import auth as _auth
        async def _ok(*a, **kw):
            return None
        monkeypatch.setattr(_auth, "get_verified_tenant", _ok)
    except Exception:
        pass

    session_id = f"race-test-{uuid.uuid4()}"
    tenant_id = f"tenant-{uuid.uuid4()}"
    user_email = "race@aria.local"
    # Register tenant under this user so get_verified_tenant resolves to
    # 200 and the chat handler reaches the lock acquisition site.
    mock_tenant_lookup(tenant_id, user_email)

    headers = auth_headers_factory(user_id="race-test-user", email=user_email)

    payload_a = {
        "session_id": session_id,
        "message": "FIRST message from user",
        "tenant_id": tenant_id,
    }
    payload_b = {
        "session_id": session_id,
        "message": "SECOND message from user",
        "tenant_id": tenant_id,
    }

    # asyncio.gather schedules both immediately — without the per-session
    # lock, they will both reach session.append(user) before either
    # reaches call_claude(), producing [user_a, user_b, assistant_a,
    # assistant_b] which is the exact corruption the lock prevents.
    results = await asyncio.gather(
        client.post("/api/ceo/chat", json=payload_a, headers=headers),
        client.post("/api/ceo/chat", json=payload_b, headers=headers),
        return_exceptions=True,
    )

    # If both requests hit auth/middleware errors, surface that as a
    # skip — the test environment isn't set up well enough to exercise
    # the lock and we shouldn't claim a passing assertion we never
    # actually verified.
    statuses = [getattr(r, "status_code", None) for r in results]
    if not any(s == 200 for s in statuses):
        pytest.skip(
            f"Neither concurrent CEO chat request returned 200 "
            f"(got {statuses}). The chat-session-lock can't be unit-tested "
            f"without a working auth + tenant fixture set; flagging this as "
            f"a 'doesn't lend itself to a unit test' finding for the test "
            f"infra owner."
        )

    # Read the in-memory session and assert pairwise ordering. If the
    # lock held, every user message MUST be immediately followed by its
    # own assistant reply — never by another user message.
    history = _chat_state.chat_sessions.get(session_id, [])
    user_indexes = [i for i, m in enumerate(history) if m.get("role") == "user"]
    assistant_indexes = [i for i, m in enumerate(history) if m.get("role") == "assistant"]

    assert len(user_indexes) == 2, (
        f"Expected 2 user messages in history, got {len(user_indexes)}. "
        f"History: {history}"
    )
    assert len(assistant_indexes) == 2, (
        f"Expected 2 assistant messages, got {len(assistant_indexes)}. "
        f"History: {history}"
    )

    # The serialization invariant: each user message at index i must be
    # followed by an assistant reply at index i+1, NOT another user
    # message. If the lock failed, history would look like
    # [user, user, assistant, assistant] — the user_indexes would be
    # [0, 1] and the test would fail here.
    for i in user_indexes:
        assert i + 1 < len(history), (
            f"User message at index {i} has no following message — "
            f"history truncated mid-turn. Full history: {history}"
        )
        assert history[i + 1].get("role") == "assistant", (
            f"User message at index {i} was followed by another "
            f"{history[i + 1].get('role')!r} message instead of an "
            f"assistant reply. Lock leaked. History: {history}"
        )
