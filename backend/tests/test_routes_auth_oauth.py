"""Safety-net route tests — OAuth init / callback endpoints.

The OAuth init endpoints sit under `/api/auth/` which is in
`_PUBLIC_PREFIXES` (the middleware bypasses JWT for OAuth callback URLs).
Each init endpoint therefore has to manually verify the caller's JWT via
the `?access_token=` query param AND verify tenant ownership — without
those manual checks, ANY caller who knew a victim's tenant_id could
complete OAuth with their own provider account and hijack the tenant.

Routes covered (all manual-auth):
  - GET    /api/auth/twitter/connect/{tenant_id}
  - GET    /api/auth/linkedin/connect/{tenant_id}
  - GET    /api/auth/google/connect/{tenant_id}
  - POST   /api/integrations/{tenant_id}/google-tokens   (manual auth too —
                                                          middleware bypasses
                                                          /google-tokens)

Each gets at minimum:
  - missing ?access_token → 401
  - invalid ?access_token → 401
  - valid ?access_token for a DIFFERENT tenant_id → 403 "Access denied"

The Twitter/LinkedIn/Google CALLBACK endpoints are intentionally not
auth-tested here — they're invoked by the provider with a `code` query
param and need no JWT. Their security model is the OAuth state token.
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


# ─── GET /api/auth/twitter/connect/{tenant_id} ───────────────────────────

async def test_twitter_connect_missing_access_token_401(client, route_setup):
    """No ?access_token= query param → 401 (manual auth check)."""
    resp = await client.get(
        f"/api/auth/twitter/connect/{route_setup.tenant_id}",
        follow_redirects=False,
    )
    assert resp.status_code == 401, resp.text


async def test_twitter_connect_invalid_token_401(client, route_setup):
    resp = await client.get(
        f"/api/auth/twitter/connect/{route_setup.tenant_id}"
        f"?access_token=not-a-jwt",
        follow_redirects=False,
    )
    assert resp.status_code == 401, resp.text


async def test_twitter_connect_cross_tenant_403(client, route_setup, auth_headers_factory):
    """Valid JWT for user B against user A's tenant → 403."""
    # We need just the raw token (not the Bearer header).
    import os, time
    from jose import jwt as _jwt
    secret = os.environ["SUPABASE_JWT_SECRET"]
    other_token = _jwt.encode(
        {
            "sub": route_setup.other_id,
            "email": route_setup.other_email,
            "role": "authenticated",
            "aud": "authenticated",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        secret,
        algorithm="HS256",
    )
    resp = await client.get(
        f"/api/auth/twitter/connect/{route_setup.tenant_id}"
        f"?access_token={other_token}",
        follow_redirects=False,
    )
    assert resp.status_code == 403, resp.text


# ─── GET /api/auth/linkedin/connect/{tenant_id} ──────────────────────────

async def test_linkedin_connect_missing_access_token_401(client, route_setup):
    resp = await client.get(
        f"/api/auth/linkedin/connect/{route_setup.tenant_id}",
        follow_redirects=False,
    )
    assert resp.status_code == 401, resp.text


async def test_linkedin_connect_invalid_token_401(client, route_setup):
    resp = await client.get(
        f"/api/auth/linkedin/connect/{route_setup.tenant_id}"
        f"?access_token=garbage",
        follow_redirects=False,
    )
    assert resp.status_code == 401, resp.text


async def test_linkedin_connect_cross_tenant_403(client, route_setup):
    import os, time
    from jose import jwt as _jwt
    secret = os.environ["SUPABASE_JWT_SECRET"]
    other_token = _jwt.encode(
        {
            "sub": route_setup.other_id,
            "email": route_setup.other_email,
            "role": "authenticated",
            "aud": "authenticated",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        secret,
        algorithm="HS256",
    )
    resp = await client.get(
        f"/api/auth/linkedin/connect/{route_setup.tenant_id}"
        f"?access_token={other_token}",
        follow_redirects=False,
    )
    assert resp.status_code == 403, resp.text


# ─── GET /api/auth/google/connect/{tenant_id} ────────────────────────────

async def test_google_connect_missing_access_token_401(client, route_setup):
    resp = await client.get(
        f"/api/auth/google/connect/{route_setup.tenant_id}",
        follow_redirects=False,
    )
    assert resp.status_code == 401, resp.text


async def test_google_connect_invalid_token_401(client, route_setup):
    resp = await client.get(
        f"/api/auth/google/connect/{route_setup.tenant_id}"
        f"?access_token=garbage",
        follow_redirects=False,
    )
    assert resp.status_code == 401, resp.text


async def test_google_connect_cross_tenant_403(client, route_setup):
    import os, time
    from jose import jwt as _jwt
    secret = os.environ["SUPABASE_JWT_SECRET"]
    other_token = _jwt.encode(
        {
            "sub": route_setup.other_id,
            "email": route_setup.other_email,
            "role": "authenticated",
            "aud": "authenticated",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        secret,
        algorithm="HS256",
    )
    resp = await client.get(
        f"/api/auth/google/connect/{route_setup.tenant_id}"
        f"?access_token={other_token}",
        follow_redirects=False,
    )
    assert resp.status_code == 403, resp.text


# ─── POST /api/integrations/{tenant_id}/google-tokens ────────────────────
# Manual-auth pattern — same threat model as the OAuth init endpoints.
# The middleware bypasses /google-tokens because the path is special-cased
# (path.endswith("/google-tokens")), so the handler MUST check ?access_token=.

async def test_google_tokens_missing_access_token_401(client, route_setup):
    resp = await client.post(
        f"/api/integrations/{route_setup.tenant_id}/google-tokens",
        json={"google_access_token": "x"},
    )
    assert resp.status_code == 401, resp.text


async def test_google_tokens_invalid_token_401(client, route_setup):
    resp = await client.post(
        f"/api/integrations/{route_setup.tenant_id}/google-tokens"
        f"?access_token=garbage",
        json={"google_access_token": "x"},
    )
    assert resp.status_code == 401, resp.text


async def test_google_tokens_cross_tenant_403(client, route_setup):
    import os, time
    from jose import jwt as _jwt
    secret = os.environ["SUPABASE_JWT_SECRET"]
    other_token = _jwt.encode(
        {
            "sub": route_setup.other_id,
            "email": route_setup.other_email,
            "role": "authenticated",
            "aud": "authenticated",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        secret,
        algorithm="HS256",
    )
    resp = await client.post(
        f"/api/integrations/{route_setup.tenant_id}/google-tokens"
        f"?access_token={other_token}",
        json={"google_access_token": "x"},
    )
    assert resp.status_code == 403, resp.text


# ─── /api/tenant/by-email/{email} — email-claim-match endpoint ──────────

async def test_tenant_by_email_no_auth_401(client):
    resp = await client.get("/api/tenant/by-email/anyone@example.com")
    assert resp.status_code == 401, resp.text


async def test_tenant_by_email_mismatched_claim_403(client, route_setup):
    """JWT email != requested email → 403. This is the audit fix that
    removed the public-oracle behaviour."""
    resp = await client.get(
        "/api/tenant/by-email/somebody-else@example.com",
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_tenant_by_email_matching_claim_passes_bouncer(client, route_setup):
    """Requesting your own email passes the JWT-email match — handler may
    200 (registered) or 404 (not in DB), both indicate auth-pass."""
    # Pre-seed the tenant_configs query result so the handler returns 200.
    route_setup.mock_supabase.set_response("tenant_configs", [
        {"tenant_id": route_setup.tenant_id, "owner_email": route_setup.owner_email},
    ])
    resp = await client.get(
        f"/api/tenant/by-email/{route_setup.owner_email}",
        headers=route_setup.owner_headers,
    )
    assert resp.status_code in (200, 404), resp.text
