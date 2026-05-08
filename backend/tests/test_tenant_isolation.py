"""Tenant isolation ("Bouncer") integration tests.

Validates that ARIA's per-route tenant ownership check refuses
cross-tenant access. The CRM and Inbox routers were the highest-risk
IDOR surface — both expose tenant_id in the URL and route to multi-
tenant database tables, so a missing Depends(get_verified_tenant) at
either router would leak every other tenant's data to anyone with a
valid JWT.

Each test mints a real HS256 JWT for User A (via auth_headers_factory)
and registers a tenant for User A only (via mock_tenant_lookup). User
B's tenant_id is never registered, so get_verified_tenant's "tenant
exists but caller doesn't own it" branch fires (collapsed to 403 in
auth.py:298-306 to avoid leaking tenant existence).

If any of these tests start returning 200, a router lost its
get_verified_tenant dep — fix the router, don't relax the test.
"""
from __future__ import annotations

import uuid

import pytest


USER_A_ID = "user-a-uuid"
USER_A_EMAIL = "user-a@aria.test"

USER_B_ID = "user-b-uuid"
USER_B_EMAIL = "user-b@aria.test"


@pytest.fixture
def tenant_ids():
    """Stable per-test UUIDs for User A and User B's tenants.

    Use uuid4() so the IDs are different per-test (no cross-test
    contamination via the per-tenant lru_cache in config.loader). The
    cache TTLs out in 10s anyway, but parallel test workers can race
    if we hard-code the same UUIDs.
    """
    return {
        "a": str(uuid.uuid4()),
        "b": str(uuid.uuid4()),
    }


async def test_user_a_cannot_read_user_b_crm_contacts(
    client,
    auth_headers_factory,
    mock_supabase,
    mock_tenant_lookup,
    tenant_ids,
):
    """User A's JWT against User B's CRM endpoint must return 403.

    This is THE canonical IDOR test for the CRM surface. The router-
    level Depends(get_verified_tenant) on backend/routers/crm.py:33-37
    is what stops it; if that dep is removed or the router prefix
    is restructured wrong, this test catches it.
    """
    # Register only User A's tenant. User B's tenant_id is unknown to
    # the (mocked) DB.
    mock_tenant_lookup(tenant_ids["a"], USER_A_EMAIL)

    headers = auth_headers_factory(user_id=USER_A_ID, email=USER_A_EMAIL)

    resp = await client.get(
        f"/api/crm/{tenant_ids['b']}/contacts",
        headers=headers,
    )

    assert resp.status_code == 403, (
        f"Expected 403 cross-tenant deny, got {resp.status_code}: {resp.text}"
    )
    # The body should NOT leak whether the tenant exists. auth.py
    # collapses both "tenant doesn't exist" and "tenant exists but
    # not yours" to the same 'Access denied' string — verify.
    body = resp.json()
    assert body.get("detail") == "Access denied", (
        f"Expected generic 'Access denied', got: {body}"
    )


async def test_user_a_cannot_read_user_b_inbox(
    client,
    auth_headers_factory,
    mock_supabase,
    mock_tenant_lookup,
    tenant_ids,
):
    """User A's JWT against User B's inbox listing must return 403.

    /api/inbox/ is in server.py's _PUBLIC_PREFIXES so the global JWT
    middleware skips it (the create endpoint is hit by Paperclip
    agents directly, no JWT). The bouncer is the per-route
    Depends(get_verified_tenant) on the LIST endpoint
    (backend/routers/inbox.py:109). If someone ever drops that dep
    thinking the public-prefix exemption covers all inbox routes,
    this test catches it.
    """
    mock_tenant_lookup(tenant_ids["a"], USER_A_EMAIL)

    headers = auth_headers_factory(user_id=USER_A_ID, email=USER_A_EMAIL)

    resp = await client.get(
        f"/api/inbox/{tenant_ids['b']}",
        headers=headers,
    )

    assert resp.status_code == 403, (
        f"Expected 403 cross-tenant inbox deny, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("detail") == "Access denied", (
        f"Expected generic 'Access denied', got: {body}"
    )


async def test_user_a_can_read_own_tenant(
    client,
    auth_headers_factory,
    mock_supabase,
    mock_tenant_lookup,
    tenant_ids,
):
    """Sanity: the bouncer doesn't false-positive on legitimate access.

    A test suite that only tests the deny path can pass even when
    every endpoint silently 403s on everyone — including the owner.
    This test asserts that User A reading User A's own CRM endpoint
    succeeds, so a regression in get_verified_tenant's match logic
    (e.g. an off-by-case email comparison, a tenant_id type coercion
    bug) gets caught alongside the deny-path tests.
    """
    mock_tenant_lookup(tenant_ids["a"], USER_A_EMAIL)
    # Configure the contacts table to return one row so the response
    # shape is non-trivial — confirms we got past the bouncer AND into
    # the handler.
    mock_supabase.set_response("crm_contacts", [
        {
            "id": "contact-1",
            "tenant_id": tenant_ids["a"],
            "name": "Test Contact",
            "email": "contact@example.com",
            "status": "lead",
        },
    ])

    headers = auth_headers_factory(user_id=USER_A_ID, email=USER_A_EMAIL)

    resp = await client.get(
        f"/api/crm/{tenant_ids['a']}/contacts",
        headers=headers,
    )

    assert resp.status_code == 200, (
        f"Owner access denied — get_verified_tenant regression. "
        f"Status {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    # The handler returns {"contacts": [...], "total": N}. Don't pin
    # the exact data (the mock's count behavior varies by chain
    # invocation); just confirm the schema is intact.
    assert "contacts" in body, f"Unexpected response shape: {body}"


async def test_unauthenticated_request_is_rejected(
    client,
    mock_supabase,
    mock_tenant_lookup,
    tenant_ids,
):
    """No Authorization header → 401, not 403.

    Belt-and-braces: if the auth middleware breaks and starts letting
    headerless requests through to get_verified_tenant, the dep would
    return 401 from get_current_user. Either way we should never see
    a 200, but the status code distinguishes which layer caught it.
    """
    mock_tenant_lookup(tenant_ids["a"], USER_A_EMAIL)

    resp = await client.get(f"/api/crm/{tenant_ids['a']}/contacts")

    assert resp.status_code in (401, 403), (
        f"Unauthenticated request reached the handler! Status {resp.status_code}: {resp.text}"
    )


async def test_invalid_jwt_is_rejected(
    client,
    mock_supabase,
    mock_tenant_lookup,
    tenant_ids,
):
    """A garbage JWT must 401 — proves the test secret is being enforced.

    If conftest's env-var bootstrap silently fails (e.g. the
    lru_cache on _get_jwt_secret returned an empty string before we
    set the secret), the middleware would fall through to dev-mode
    and accept anything. This test fails LOUDLY in that case.
    """
    mock_tenant_lookup(tenant_ids["a"], USER_A_EMAIL)

    headers = {"Authorization": "Bearer not-a-real-jwt"}
    resp = await client.get(
        f"/api/crm/{tenant_ids['a']}/contacts",
        headers=headers,
    )

    assert resp.status_code == 401, (
        f"Expected 401 for malformed JWT, got {resp.status_code}: {resp.text}. "
        f"If this is 200, the JWT secret is unset and dev-mode bypass is active."
    )
