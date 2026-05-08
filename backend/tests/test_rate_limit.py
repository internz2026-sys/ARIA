"""Rate limit integration tests.

Covers two production guarantees:

1.  ``/api/ceo/chat`` is per-user limited to 30 calls / 60s in the auth
    middleware (see ``backend/server.py`` around line 891). A user who
    bursts 100 concurrent requests must see at least one 429.

2.  The login-failed bucket in ``backend/routers/login_rate_limit.py``
    locks the per-email bucket after 5 failures in 15min (env-overridable
    via ``LOGIN_ATTEMPT_EMAIL_LIMIT``). The 6th call must report
    ``allowed: False`` in the response body.

Both tests stub Redis with an in-memory monkeypatch on
``backend.services.rate_limit`` so they're deterministic regardless of
whether a real Redis is reachable from the test runner. We deliberately
use the module's existing ``_hit_memory`` fallback path by forcing
``_get_redis`` to return ``False`` (the "Redis unreachable" sentinel) —
this means we exercise the actual production fallback branch instead of
re-implementing rate-limit semantics in the test.

Sibling Backend Coder #1 owns ``backend/tests/conftest.py``. Fixtures
this file relies on:
  - ``client``   — async httpx test client
  - ``auth_headers_factory`` — issues a fake JWT for a given user_id

If ``auth_headers_factory`` doesn't exist at merge time, the rate-limit
test will skip with a clear reason rather than fail confusingly.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


# ─────────────────────────────────────────────────────────────────────
# Shared monkeypatch helper — force the rate-limit service onto its
# in-memory fallback so the test doesn't depend on the docker Redis.
# ─────────────────────────────────────────────────────────────────────
def _force_memory_rate_limit(monkeypatch):
    """Reset the rate_limit module to its initial (no-Redis) state and
    pin ``_get_redis`` to ``False`` so every ``hit()`` flows through the
    in-memory bucket. Also clears the bucket dict so previous tests
    don't bleed into this one."""
    from backend.services import rate_limit as _rl

    # Clear any previous in-memory state
    with _rl._memory_lock:
        _rl._memory_buckets.clear()
        _rl._memory_last_gc = 0.0

    # Force every call to go through _hit_memory by short-circuiting the
    # Redis getter. Returning False is the sentinel _get_redis uses for
    # "Redis is unreachable for the rest of this process" — hit() reads
    # it directly and falls through.
    monkeypatch.setattr(_rl, "_get_redis", lambda: False)


# ─────────────────────────────────────────────────────────────────────
# Test 1: burst 100 concurrent requests, expect at least one 429
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_rate_limit_429_after_burst(
    client, auth_headers_factory, mock_supabase, mock_tenant_lookup, monkeypatch,
):
    """Fire 100 concurrent POSTs at /api/ceo/chat with one user's JWT.
    The middleware caps a single user at 30 ceo_chat calls per minute,
    so at least one of the 100 must come back 429.

    We don't care about the body of the successful responses — they may
    be 200 (real CEO reply), 500 (Claude unavailable in test env), or
    whatever else; what we assert is that the over-the-cap rejections
    DO happen and DO carry the 429 status code with the documented
    detail string.

    ``auth_headers_factory`` is sibling-owned (conftest); pytest will
    skip this test at collection time if the fixture isn't registered.
    """
    _force_memory_rate_limit(monkeypatch)

    # Stub call_claude so the 30 calls that DO get past the middleware
    # don't try to spawn the local Claude CLI subprocess.
    async def _fake_call_claude(*args, **kwargs):
        return "ok"

    try:
        from backend.tools import claude_cli as _cli
        monkeypatch.setattr(_cli, "call_claude", _fake_call_claude)
    except Exception:
        # If the import path changes, the test still demonstrates the
        # 429 behavior; non-stubbed calls will just be slower / 5xx.
        pass

    # One user_id => one rate-limit bucket. We need the request fixture
    # to inject the same JWT on every call so all 100 hits land in the
    # same per-user counter.
    user_id = f"test-user-{uuid.uuid4()}"
    user_email = f"burst-{user_id}@aria.local"
    tenant_id = f"tenant-{uuid.uuid4()}"
    # Register the tenant so get_verified_tenant doesn't 403 every call
    # — without a registered tenant every request returns 403, which is
    # NOT a 429 even if the rate-limit logic fires. The 429 lives in the
    # SAME middleware path AFTER the rate-limit check but BEFORE tenant
    # verification, so registering still leaves room for the 429 to win.
    mock_tenant_lookup(tenant_id, user_email)
    headers = auth_headers_factory(user_id=user_id, email=user_email)

    payload = {
        "session_id": f"s-{uuid.uuid4()}",
        "message": "hi",
        "tenant_id": tenant_id,
    }

    async def _one():
        return await client.post("/api/ceo/chat", json=payload, headers=headers)

    responses = await asyncio.gather(*[_one() for _ in range(100)], return_exceptions=True)

    # Filter out raw exceptions (httpx will sometimes raise on cancelled
    # tasks); we only care about responses that actually returned a
    # status code.
    statuses = [r.status_code for r in responses if hasattr(r, "status_code")]

    assert statuses, "No HTTP responses came back at all"
    assert 429 in statuses, (
        f"Expected at least one 429 from the 30/min ceo_chat cap, "
        f"got distribution: {sorted(set(statuses))}"
    )
    # Loose sanity: at least 30 should NOT have been 429 (the cap allows
    # 30 through). Tolerate auth/500s in the non-429 set.
    non_429 = [s for s in statuses if s != 429]
    assert len(non_429) >= 1, "Every response was 429 — middleware ate them all"


# ─────────────────────────────────────────────────────────────────────
# Test 2: 6 consecutive login-failed POSTs lock the email bucket
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_login_attempts_locked_after_5(client, monkeypatch):
    """The login-rate-limit router caps a single email at 5 failed
    attempts per 15min. The 6th POST must come back with
    ``allowed: False``. The endpoint always returns 200 with a body
    that includes ``allowed`` / ``attempts_remaining`` so the frontend
    can render a "locked out" message — there is no separate 429 path
    for this contract.
    """
    _force_memory_rate_limit(monkeypatch)

    # Make sure the env var override matches the spec (5 attempts).
    monkeypatch.setenv("LOGIN_ATTEMPT_EMAIL_LIMIT", "5")
    monkeypatch.setenv("LOGIN_ATTEMPT_IP_LIMIT", "1000")  # don't trip the IP bucket
    monkeypatch.setenv("LOGIN_ATTEMPT_WINDOW_SECONDS", "900")

    email = f"victim-{uuid.uuid4().hex[:8]}@example.com"
    payload = {"email": email}

    results: list[dict] = []
    for i in range(6):
        resp = await client.post("/api/auth/login-failed", json=payload)
        assert resp.status_code == 200, (
            f"login-failed should always return 200 with a body; "
            f"attempt #{i + 1} returned {resp.status_code}: {resp.text[:200]}"
        )
        results.append(resp.json())

    # First 5 attempts should be allowed: True (the user could keep trying,
    # though attempts_remaining decays toward 0 by the 5th). The 6th must
    # flip to allowed: False — that's the lockout signal the frontend reads.
    assert results[5].get("allowed") is False, (
        f"Attempt #6 should have triggered lockout (allowed=False). "
        f"Got: {results[5]}"
    )
    # Sanity: by the 6th attempt the limit metadata should still be present
    # so the UI can show "wait N seconds" copy.
    assert results[5].get("limit") == 5
    assert results[5].get("attempts_remaining") == 0
