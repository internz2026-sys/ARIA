"""Safety-net route tests — Integration status + management endpoints.

Pins URL + auth contracts for the per-tenant integration status / connect /
disconnect routes about to move out of server.py. The OAuth INIT endpoints
(GET /api/auth/{provider}/connect/{tenant_id}) live in test_routes_auth_oauth.py;
this file covers the JWT-protected per-tenant management.

Routes covered:
  - GET    /api/integrations/{tenant_id}/twitter-status
  - GET    /api/integrations/{tenant_id}/linkedin-status
  - GET    /api/integrations/{tenant_id}/whatsapp-status
  - GET    /api/integrations/{tenant_id}/gmail-status
  - POST   /api/integrations/{tenant_id}/gmail-disconnect
  - POST   /api/integrations/{tenant_id}/twitter-disconnect
  - POST   /api/integrations/{tenant_id}/linkedin-disconnect
  - GET    /api/linkedin/{tenant_id}/organizations
  - POST   /api/linkedin/{tenant_id}/set-target
  - POST   /api/whatsapp/{tenant_id}/connect
  - POST   /api/whatsapp/{tenant_id}/disconnect

Each route gets at minimum: happy-path 200 (or appropriate 2xx) with owner
JWT, cross-tenant 403, and 401 with no JWT.
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


# ─── GET /api/integrations/{tenant_id}/twitter-status ────────────────────

async def test_twitter_status_owner_happy_path(client, route_setup):
    resp = await client.get(
        f"/api/integrations/{route_setup.tenant_id}/twitter-status",
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "connected" in body


async def test_twitter_status_cross_tenant_403(client, route_setup):
    resp = await client.get(
        f"/api/integrations/{route_setup.tenant_id}/twitter-status",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_twitter_status_no_auth_401(client, route_setup):
    resp = await client.get(
        f"/api/integrations/{route_setup.tenant_id}/twitter-status",
    )
    assert resp.status_code == 401, resp.text


# ─── GET /api/integrations/{tenant_id}/linkedin-status ───────────────────

async def test_linkedin_status_owner_happy_path(client, route_setup):
    resp = await client.get(
        f"/api/integrations/{route_setup.tenant_id}/linkedin-status",
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 200, resp.text


async def test_linkedin_status_cross_tenant_403(client, route_setup):
    resp = await client.get(
        f"/api/integrations/{route_setup.tenant_id}/linkedin-status",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_linkedin_status_no_auth_401(client, route_setup):
    resp = await client.get(
        f"/api/integrations/{route_setup.tenant_id}/linkedin-status",
    )
    assert resp.status_code == 401, resp.text


# ─── GET /api/integrations/{tenant_id}/whatsapp-status ───────────────────

async def test_whatsapp_status_owner_happy_path(client, route_setup):
    resp = await client.get(
        f"/api/integrations/{route_setup.tenant_id}/whatsapp-status",
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 200, resp.text


async def test_whatsapp_status_cross_tenant_403(client, route_setup):
    resp = await client.get(
        f"/api/integrations/{route_setup.tenant_id}/whatsapp-status",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_whatsapp_status_no_auth_401(client, route_setup):
    resp = await client.get(
        f"/api/integrations/{route_setup.tenant_id}/whatsapp-status",
    )
    assert resp.status_code == 401, resp.text


# ─── GET /api/integrations/{tenant_id}/gmail-status ──────────────────────

async def test_gmail_status_cross_tenant_403(client, route_setup):
    resp = await client.get(
        f"/api/integrations/{route_setup.tenant_id}/gmail-status",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_gmail_status_no_auth_401(client, route_setup):
    resp = await client.get(
        f"/api/integrations/{route_setup.tenant_id}/gmail-status",
    )
    assert resp.status_code == 401, resp.text


# ─── POST /api/integrations/{tenant_id}/gmail-disconnect ─────────────────

async def test_gmail_disconnect_owner_happy_path(client, route_setup):
    resp = await client.post(
        f"/api/integrations/{route_setup.tenant_id}/gmail-disconnect",
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status") == "disconnected"


async def test_gmail_disconnect_cross_tenant_403(client, route_setup):
    resp = await client.post(
        f"/api/integrations/{route_setup.tenant_id}/gmail-disconnect",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_gmail_disconnect_no_auth_401(client, route_setup):
    resp = await client.post(
        f"/api/integrations/{route_setup.tenant_id}/gmail-disconnect",
    )
    assert resp.status_code == 401, resp.text


# ─── POST /api/integrations/{tenant_id}/twitter-disconnect ───────────────

async def test_twitter_disconnect_owner_happy_path(client, route_setup):
    resp = await client.post(
        f"/api/integrations/{route_setup.tenant_id}/twitter-disconnect",
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 200, resp.text


async def test_twitter_disconnect_cross_tenant_403(client, route_setup):
    resp = await client.post(
        f"/api/integrations/{route_setup.tenant_id}/twitter-disconnect",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_twitter_disconnect_no_auth_401(client, route_setup):
    resp = await client.post(
        f"/api/integrations/{route_setup.tenant_id}/twitter-disconnect",
    )
    assert resp.status_code == 401, resp.text


# ─── POST /api/integrations/{tenant_id}/linkedin-disconnect ──────────────

async def test_linkedin_disconnect_owner_happy_path(client, route_setup):
    resp = await client.post(
        f"/api/integrations/{route_setup.tenant_id}/linkedin-disconnect",
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 200, resp.text


async def test_linkedin_disconnect_cross_tenant_403(client, route_setup):
    resp = await client.post(
        f"/api/integrations/{route_setup.tenant_id}/linkedin-disconnect",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_linkedin_disconnect_no_auth_401(client, route_setup):
    resp = await client.post(
        f"/api/integrations/{route_setup.tenant_id}/linkedin-disconnect",
    )
    assert resp.status_code == 401, resp.text


# ─── POST /api/linkedin/{tenant_id}/set-target ───────────────────────────

async def test_linkedin_set_target_cross_tenant_403(client, route_setup):
    resp = await client.post(
        f"/api/linkedin/{route_setup.tenant_id}/set-target",
        json={"org_urn": "", "org_name": ""},
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_linkedin_set_target_no_auth_401(client, route_setup):
    resp = await client.post(
        f"/api/linkedin/{route_setup.tenant_id}/set-target",
        json={"org_urn": ""},
    )
    assert resp.status_code == 401, resp.text


async def test_linkedin_set_target_owner_happy_path(client, route_setup):
    resp = await client.post(
        f"/api/linkedin/{route_setup.tenant_id}/set-target",
        json={"org_urn": "", "org_name": ""},
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 200, resp.text


# ─── GET /api/linkedin/{tenant_id}/organizations ─────────────────────────

async def test_linkedin_orgs_cross_tenant_403(client, route_setup):
    resp = await client.get(
        f"/api/linkedin/{route_setup.tenant_id}/organizations",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_linkedin_orgs_no_auth_401(client, route_setup):
    resp = await client.get(
        f"/api/linkedin/{route_setup.tenant_id}/organizations",
    )
    assert resp.status_code == 401, resp.text


# ─── POST /api/whatsapp/{tenant_id}/connect ──────────────────────────────

async def test_whatsapp_connect_schema_422(client, route_setup):
    """Body schema requires access_token + phone_number_id."""
    resp = await client.post(
        f"/api/whatsapp/{route_setup.tenant_id}/connect",
        json={},  # missing required fields
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 422, resp.text


async def test_whatsapp_connect_cross_tenant_403(client, route_setup):
    resp = await client.post(
        f"/api/whatsapp/{route_setup.tenant_id}/connect",
        json={"access_token": "x", "phone_number_id": "y"},
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_whatsapp_connect_no_auth_401(client, route_setup):
    resp = await client.post(
        f"/api/whatsapp/{route_setup.tenant_id}/connect",
        json={"access_token": "x", "phone_number_id": "y"},
    )
    assert resp.status_code == 401, resp.text


# ─── POST /api/whatsapp/{tenant_id}/disconnect ───────────────────────────

async def test_whatsapp_disconnect_owner_happy_path(client, route_setup):
    resp = await client.post(
        f"/api/whatsapp/{route_setup.tenant_id}/disconnect",
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 200, resp.text


async def test_whatsapp_disconnect_cross_tenant_403(client, route_setup):
    resp = await client.post(
        f"/api/whatsapp/{route_setup.tenant_id}/disconnect",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_whatsapp_disconnect_no_auth_401(client, route_setup):
    resp = await client.post(
        f"/api/whatsapp/{route_setup.tenant_id}/disconnect",
    )
    assert resp.status_code == 401, resp.text
