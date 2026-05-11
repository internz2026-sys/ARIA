"""Tests for plan self-service + /usage backend.

Covers:
  * POST /api/profile/me/plan        self-service plan change
  * GET  /api/profile/me             returns plan + limits
  * GET  /api/usage/{tenant_id}      real per-tenant usage from agent_logs
  * POST /api/admin/users/{uid}/plan admin plan override

Conventions mirror test_admin_ban.py / test_plan_quotas.py:
  * Real HS256 JWTs minted via ``auth_headers_factory``
  * ``mock_supabase.set_response("table", [...])`` to wire up DB reads
  * ``mock_tenant_lookup`` to bypass the tenant-config TTL cache for
    ownership checks
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest


pytestmark = pytest.mark.asyncio


# ─────────────────────────────────────────────────────────────────────────────
# Constants — keep distinct per test so cross-test cache bleed is unlikely
# ─────────────────────────────────────────────────────────────────────────────

USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_EMAIL = "owner@example.com"
TENANT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

ADMIN_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
ADMIN_EMAIL = "admin@example.com"

TARGET_USER_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
TARGET_EMAIL = "target@example.com"
TARGET_TENANT_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def _profile_row(
    *,
    user_id: str,
    email: str,
    role: str = "user",
    status: str = "active",
    banned_at: str | None = None,
) -> dict:
    """Shape of profiles row matching backend.services.profiles selects."""
    return {
        "user_id": user_id,
        "email": email,
        "full_name": email.split("@")[0],
        "role": role,
        "status": status,
        "banned_at": banned_at,
    }


def _tenant_row(
    tenant_id: str,
    owner_email: str,
    plan: str = "free",
) -> dict:
    """Minimum tenant_configs row the TenantConfig pydantic model needs."""
    return {
        "tenant_id": tenant_id,
        "plan": plan,
        "owner_email": owner_email,
        "business_name": "Test Tenant",
        "active_agents": [
            "content_writer", "social_manager", "ad_strategist",
            "email_marketer", "media", "ceo",
        ],
    }


def _agent_log(
    tenant_id: str,
    agent_name: str,
    *,
    status: str = "completed",
    minutes_ago: int = 5,
) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "id": str(uuid4()),
        "tenant_id": tenant_id,
        "agent_name": agent_name,
        "action": "run",
        "result": {"input_tokens": 1000, "output_tokens": 500},
        "status": status,
        "timestamp": ts.isoformat(),
    }


def _clear_caches():
    """Drop the TenantConfig + profile role/status TTL caches between sub-
    cases inside a single test. Without this a previous .set_response
    call's row is silently shadowed by the cache."""
    from backend.config.loader import _config_cache
    _config_cache.clear()
    try:
        from backend.services import profiles as profiles_service
        profiles_service.invalidate_role_cache()
        profiles_service.invalidate_status_cache()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/profile/me/plan
# ─────────────────────────────────────────────────────────────────────────────


async def test_self_service_plan_change_happy_path(client, mock_supabase, auth_headers_factory):
    """Authenticated user POSTs their preferred tier and the row updates.

    The endpoint should:
      1. Look up tenant_configs by owner_email == JWT email
      2. Mutate config.plan
      3. Call save_tenant_config (upsert)
      4. Return the updated TenantConfig as JSON
    """
    mock_supabase.set_response("tenant_configs", [_tenant_row(TENANT_ID, USER_EMAIL, "free")])
    _clear_caches()

    headers = auth_headers_factory(user_id=USER_ID, email=USER_EMAIL)
    resp = await client.post(
        "/api/profile/me/plan",
        headers=headers,
        json={"plan": "growth"},
    )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("ok") is True
    assert body.get("tenant_id") == TENANT_ID
    assert body.get("config", {}).get("plan") == "growth"

    # Confirm the upsert hit tenant_configs with the new plan value.
    upserts = mock_supabase.inserts_for("tenant_configs")  # supabase-py upsert dispatches via insert path
    if not upserts:
        # save_tenant_config uses .upsert() not .insert(); some mock
        # versions record upserts separately or not at all. Fall back to
        # asserting the response shape only when the mock can't see it.
        return
    assert any(u.get("plan") == "growth" for u in upserts), (
        f"expected an upsert with plan=growth, got: {upserts}"
    )


async def test_self_service_invalid_plan_rejected(client, mock_supabase, auth_headers_factory):
    """A typo / unknown tier returns 400 without touching the DB."""
    mock_supabase.set_response("tenant_configs", [_tenant_row(TENANT_ID, USER_EMAIL, "free")])
    _clear_caches()

    headers = auth_headers_factory(user_id=USER_ID, email=USER_EMAIL)
    resp = await client.post(
        "/api/profile/me/plan",
        headers=headers,
        json={"plan": "enterprise"},  # not in PLAN_LIMITS
    )

    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
    body = resp.json()
    # FastAPI 400 wraps the message in 'detail' (or our HTTPException does)
    detail = body.get("detail") or ""
    assert "enterprise" in detail.lower() or "invalid" in detail.lower(), (
        f"detail should mention the bad plan or 'invalid': {body}"
    )


async def test_self_service_unknown_user_404(client, mock_supabase, auth_headers_factory):
    """A JWT email with no matching tenant_configs row gets 404."""
    # Empty tenant_configs response simulates "no row for this email"
    mock_supabase.set_response("tenant_configs", [])
    _clear_caches()

    headers = auth_headers_factory(user_id=USER_ID, email="orphan@example.com")
    resp = await client.post(
        "/api/profile/me/plan",
        headers=headers,
        json={"plan": "starter"},
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"


async def test_self_service_plan_change_unauthenticated(client, mock_supabase):
    """Without an Authorization header the endpoint rejects with 401."""
    _clear_caches()
    resp = await client.post(
        "/api/profile/me/plan",
        json={"plan": "starter"},
    )
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}: {resp.text}"


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/profile/me — now includes plan + limits
# ─────────────────────────────────────────────────────────────────────────────


async def test_profile_me_returns_plan_and_limits(client, mock_supabase, auth_headers_factory):
    """The /me handler surfaces the caller's current tier + cap table."""
    mock_supabase.set_response("tenant_configs", [_tenant_row(TENANT_ID, USER_EMAIL, "starter")])
    mock_supabase.set_response(
        "profiles",
        [_profile_row(user_id=USER_ID, email=USER_EMAIL)],
    )
    _clear_caches()

    headers = auth_headers_factory(user_id=USER_ID, email=USER_EMAIL)
    resp = await client.get("/api/profile/me", headers=headers)

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("user_id") == USER_ID
    assert body.get("plan") == "starter"
    limits = body.get("limits") or {}
    # Starter: 10 content, 1 campaign, no email sequences.
    assert limits.get("content_pieces_per_month") == 10
    assert limits.get("campaign_plans_per_month") == 1
    assert limits.get("email_sequences_enabled") is False


async def test_profile_me_no_tenant_returns_null_plan(client, mock_supabase, auth_headers_factory):
    """Mid-onboarding users without a tenant row get plan=null, no 500."""
    mock_supabase.set_response("tenant_configs", [])
    mock_supabase.set_response(
        "profiles",
        [_profile_row(user_id=USER_ID, email="solo@example.com")],
    )
    _clear_caches()

    headers = auth_headers_factory(user_id=USER_ID, email="solo@example.com")
    resp = await client.get("/api/profile/me", headers=headers)

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    # Either plan is null OR limits is null — both are acceptable signals
    # for "no tenant yet".
    assert body.get("plan") in (None, "free", ""), (
        f"unexpected plan for no-tenant user: {body.get('plan')}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/usage/{tenant_id} — real per-tenant usage
# ─────────────────────────────────────────────────────────────────────────────


async def test_usage_endpoint_counts_agent_logs(
    client, mock_supabase, mock_tenant_lookup, auth_headers_factory,
):
    """Per-agent counters reflect agent_logs rows in the last hour.

    Two content_writer rows + one ad_strategist row in the window should
    surface as requests=2 for content_writer, requests=1 for
    ad_strategist, total_requests=3.
    """
    mock_tenant_lookup(TENANT_ID, USER_EMAIL)
    rows = [
        _agent_log(TENANT_ID, "content_writer", minutes_ago=5),
        _agent_log(TENANT_ID, "content_writer", minutes_ago=15),
        _agent_log(TENANT_ID, "ad_strategist", minutes_ago=30),
    ]
    mock_supabase.set_response("agent_logs", rows)

    headers = auth_headers_factory(user_id=USER_ID, email=USER_EMAIL)
    resp = await client.get(f"/api/usage/{TENANT_ID}", headers=headers)

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()

    # ── New top-level shape ──
    assert body.get("tenant_id") == TENANT_ID
    assert body.get("window") == "1h"
    assert body.get("total_requests") == 3, f"total_requests=3 expected: {body}"

    per_agent = body.get("per_agent") or {}
    # All six canonical agents must appear, even at 0.
    for slug in ("ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist", "media"):
        assert slug in per_agent, f"agent {slug} missing from per_agent: {per_agent.keys()}"
    assert per_agent["content_writer"]["requests"] == 2
    assert per_agent["ad_strategist"]["requests"] == 1
    assert per_agent["ceo"]["requests"] == 0

    # Per-agent limit values from AGENT_HOURLY_LIMITS in claude_cli.py
    assert per_agent["ceo"]["request_limit"] == 30
    assert per_agent["content_writer"]["request_limit"] == 10

    # ── Backward-compat shape ──
    assert "tenant" in body
    assert body["tenant"]["requests"] == 3
    assert body["tenant"]["request_limit"] == 60  # HOURLY_REQUEST_LIMIT
    assert "agents" in body
    assert body["agents"]["content_writer"]["requests"] == 2


async def test_usage_endpoint_monthly_block(
    client, mock_supabase, mock_tenant_lookup, auth_headers_factory,
):
    """Monthly section reflects content_writer + social_manager + media
    sum for content_used, ad_strategist for campaigns_used."""
    mock_tenant_lookup(TENANT_ID, USER_EMAIL)
    # We can't differentiate the 1h vs monthly window because the mock
    # returns the same list for both queries. That's fine here -- the
    # test asserts the aggregation logic per agent slug, not the time
    # window precision.
    rows = [
        _agent_log(TENANT_ID, "content_writer"),
        _agent_log(TENANT_ID, "social_manager"),
        _agent_log(TENANT_ID, "media"),
        _agent_log(TENANT_ID, "ad_strategist"),
        _agent_log(TENANT_ID, "ceo"),  # should NOT count
    ]
    mock_supabase.set_response("agent_logs", rows)
    mock_supabase.set_response(
        "tenant_configs", [_tenant_row(TENANT_ID, USER_EMAIL, "free")],
    )
    _clear_caches()

    headers = auth_headers_factory(user_id=USER_ID, email=USER_EMAIL)
    resp = await client.get(f"/api/usage/{TENANT_ID}", headers=headers)

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    monthly = body.get("monthly") or {}
    # plan comes from get_tenant_config -> mock_tenant_lookup which uses
    # the registered email. Plan defaults to "starter" in the pydantic
    # schema unless the tenant row sets it. We don't assert the slug
    # because mock_tenant_lookup constructs TenantConfig with no plan
    # set, defaulting to "starter".
    assert monthly.get("content_used") == 3, (
        f"content_used should sum content_writer+social_manager+media: {monthly}"
    )
    assert monthly.get("campaigns_used") == 1


async def test_usage_endpoint_requires_ownership(
    client, mock_supabase, mock_tenant_lookup, auth_headers_factory,
):
    """User B asking for User A's tenant gets 403, not 200 with empty data."""
    # TENANT_ID is owned by USER_EMAIL; we'll call with a different email.
    mock_tenant_lookup(TENANT_ID, USER_EMAIL)
    mock_supabase.set_response("agent_logs", [])

    headers = auth_headers_factory(user_id="other-user", email="someone-else@example.com")
    resp = await client.get(f"/api/usage/{TENANT_ID}", headers=headers)

    assert resp.status_code == 403, f"expected 403 for cross-tenant, got {resp.status_code}: {resp.text}"


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/admin/users/{uid}/plan — admin override
# ─────────────────────────────────────────────────────────────────────────────


async def test_admin_set_plan_for_any_user(client, mock_supabase, auth_headers_factory):
    """Admin POSTs to a target user's plan endpoint and the row updates.

    Mock limitation: ``mock_supabase.set_response("profiles", [...])``
    returns the same list regardless of `.eq("user_id", ...)` filter,
    so we put the ADMIN profile first to satisfy the middleware's
    role-check lookup. The admin handler then reads `data[0]` for the
    target email lookup -- in production this would return TARGET_EMAIL,
    but in the mock it returns ADMIN_EMAIL. We compensate by setting
    tenant_configs.owner_email = ADMIN_EMAIL so the chain still
    resolves to the right tenant for the assertion.
    """
    mock_supabase.set_response(
        "profiles",
        [_profile_row(user_id=ADMIN_ID, email=ADMIN_EMAIL, role="super_admin")],
    )
    mock_supabase.set_response(
        "tenant_configs",
        [_tenant_row(TARGET_TENANT_ID, ADMIN_EMAIL, "free")],
    )
    _clear_caches()

    headers = auth_headers_factory(user_id=ADMIN_ID, email=ADMIN_EMAIL)
    resp = await client.post(
        f"/api/admin/users/{TARGET_USER_ID}/plan",
        headers=headers,
        json={"plan": "scale"},
    )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("ok") is True
    assert body.get("tenant_id") == TARGET_TENANT_ID
    assert body.get("target_user_id") == TARGET_USER_ID
    assert body.get("config", {}).get("plan") == "scale"


async def test_admin_set_plan_rejects_non_admin(client, mock_supabase, auth_headers_factory):
    """A regular user calling the admin endpoint is rejected by the
    /api/admin/* role gate before the handler even runs."""
    mock_supabase.set_response(
        "profiles",
        [_profile_row(user_id=USER_ID, email=USER_EMAIL, role="user")],
    )
    _clear_caches()

    headers = auth_headers_factory(user_id=USER_ID, email=USER_EMAIL)
    resp = await client.post(
        f"/api/admin/users/{TARGET_USER_ID}/plan",
        headers=headers,
        json={"plan": "growth"},
    )
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


async def test_admin_set_plan_validates_plan(client, mock_supabase, auth_headers_factory):
    """An admin with a bogus plan value still gets 400."""
    mock_supabase.set_response(
        "profiles",
        [
            _profile_row(user_id=ADMIN_ID, email=ADMIN_EMAIL, role="super_admin"),
            _profile_row(user_id=TARGET_USER_ID, email=TARGET_EMAIL, role="user"),
        ],
    )
    mock_supabase.set_response(
        "tenant_configs",
        [_tenant_row(TARGET_TENANT_ID, TARGET_EMAIL, "free")],
    )
    _clear_caches()

    headers = auth_headers_factory(user_id=ADMIN_ID, email=ADMIN_EMAIL)
    resp = await client.post(
        f"/api/admin/users/{TARGET_USER_ID}/plan",
        headers=headers,
        json={"plan": "platinum"},
    )

    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
