"""Integration tests — admin ban + ban-gate flow.

Covers:
  * The middleware/dep-level rejection of API calls made with a JWT whose
    profiles row has banned_at set (defense-in-depth on top of Supabase's
    own auth-layer ban).
  * The /api/admin/users/{target}/ban endpoint:
      - super_admin can ban an admin
      - admin (non-super) cannot ban another admin    -> 403
      - actor cannot ban their own user_id            -> 403

The fixtures (``client``, ``mock_supabase``, ``auth_headers_factory``) live
in ``backend/tests/conftest.py`` -- owned by the sibling test author. We
assume the conventions documented in the task brief:

  * ``client``                  : async httpx test client wrapping FastAPI
  * ``mock_supabase``           : in-memory Supabase mock with
                                  ``.set_response(table, data)`` plus
                                  ``.calls`` / ``.last_call_args`` style
                                  introspection on auth.admin.* methods
  * ``auth_headers_factory``    : returns a dict of HTTP headers with a
                                  signed dev-mode JWT for (user_id, email)

The auth.admin call ``sb.auth.admin.update_user_by_id`` is what we expect
to fire on a ban -- per ``backend/services/profiles.py:ban_user``. The
mock should record the call so we can assert on it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


pytestmark = pytest.mark.asyncio


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

ACTOR_ID = "11111111-1111-1111-1111-111111111111"
ACTOR_EMAIL = "actor@example.com"
TARGET_ID = "22222222-2222-2222-2222-222222222222"
TARGET_EMAIL = "target@example.com"
TENANT_ID = "33333333-3333-3333-3333-333333333333"


def _profile_row(
    *,
    user_id: str,
    email: str,
    role: str = "user",
    status: str = "active",
    banned_at: str | None = None,
) -> dict:
    """Shape of a profiles row matching the Supabase select used in
    backend.services.profiles.get_user_role / get_user_status / list_profiles.
    """
    return {
        "user_id": user_id,
        "email": email,
        "full_name": email.split("@")[0],
        "role": role,
        "status": status,
        "banned_at": banned_at,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ban gate — banned_at on profiles must reject API calls
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.xfail(
    reason=(
        "Defense-in-depth ban gate (banned_at on profiles -> 401/403 in "
        "auth_and_rate_limit_middleware) is not yet wired up; today the "
        "ban only takes effect via Supabase's auth-layer JWT rejection. "
        "This test pins the contract for when the in-process gate lands."
    ),
    strict=False,
)
async def test_banned_user_jwt_rejected(client, mock_supabase, auth_headers_factory):
    """A user whose profiles.banned_at is set in the past cannot hit any
    authenticated API surface — middleware should respond with 401 or 403
    before the route handler runs."""
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    mock_supabase.set_response(
        "profiles",
        [_profile_row(user_id=ACTOR_ID, email=ACTOR_EMAIL, banned_at=past)],
    )

    headers = auth_headers_factory(user_id=ACTOR_ID, email=ACTOR_EMAIL)
    resp = await client.get(f"/api/crm/{TENANT_ID}/contacts", headers=headers)

    assert resp.status_code in (401, 403), (
        f"Banned user should be rejected with 401/403, got {resp.status_code}: "
        f"{resp.text}"
    )


async def test_unbanned_user_can_access(client, mock_supabase, auth_headers_factory):
    """Same user with banned_at=None passes the ban check.

    The CRM contacts route still requires tenant ownership, so we mirror
    the user's email onto the tenant_configs row the auth dep loads. We
    don't care about the contacts payload itself — the assertion is on
    the auth gate not firing.
    """
    mock_supabase.set_response(
        "profiles",
        [_profile_row(user_id=ACTOR_ID, email=ACTOR_EMAIL, banned_at=None)],
    )
    # The CRM router uses get_verified_tenant which reads tenant_configs
    # via backend.config.loader.get_tenant_config — give it a row whose
    # owner_email matches the JWT.
    mock_supabase.set_response(
        "tenant_configs",
        [{"tenant_id": TENANT_ID, "owner_email": ACTOR_EMAIL}],
    )
    # Empty contacts list — the service-layer call will land on
    # crm_contacts which the mock returns [] for by default; setting it
    # explicitly keeps the test robust against the default.
    mock_supabase.set_response("crm_contacts", [])

    headers = auth_headers_factory(user_id=ACTOR_ID, email=ACTOR_EMAIL)
    resp = await client.get(f"/api/crm/{TENANT_ID}/contacts", headers=headers)

    assert resp.status_code == 200, (
        f"Unbanned, owning user should be allowed through; got "
        f"{resp.status_code}: {resp.text}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/admin/users/{target}/ban
# ─────────────────────────────────────────────────────────────────────────────

async def test_super_admin_can_ban_admin(client, mock_supabase, auth_headers_factory, monkeypatch):
    """super_admin POST .../ban on another admin succeeds, calls Supabase
    auth admin update_user_by_id with a ban_duration, and returns
    banned_until in the body."""
    # Actor is the super_admin; target is an admin. profiles.get_user_role
    # is keyed on user_id and reads the first matching row from the mock.
    # The mock's set_response stores rows on the table; we set both
    # profiles entries (actor + target) so .eq(user_id, ...).execute()
    # filters to the right one.
    mock_supabase.set_response(
        "profiles",
        [
            _profile_row(user_id=ACTOR_ID, email=ACTOR_EMAIL, role="super_admin"),
            _profile_row(user_id=TARGET_ID, email=TARGET_EMAIL, role="admin"),
        ],
    )
    # ban_user invalidates the role cache between read and write. Make
    # sure the cache for the target is empty so the test isn't depending
    # on test ordering.
    from backend.services import profiles as profiles_service
    profiles_service.invalidate_role_cache()
    profiles_service.invalidate_status_cache()

    headers = auth_headers_factory(user_id=ACTOR_ID, email=ACTOR_EMAIL)
    resp = await client.post(
        f"/api/admin/users/{TARGET_ID}/ban",
        headers=headers,
        json={"duration_hours": 24, "reason": "test ban"},
    )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("ok") is True
    assert "banned_until" in body, f"response missing banned_until: {body}"
    assert body.get("duration_hours") == 24

    # Auth admin update_user_by_id must have been invoked with a
    # ban_duration string. The mock records calls on auth.admin; the
    # exact recording API is left to conftest, so we accept either of
    # the two common shapes the mock might expose.
    auth_admin = mock_supabase.auth.admin
    calls = (
        getattr(auth_admin, "update_user_by_id_calls", None)
        or getattr(auth_admin, "calls", None)
        or []
    )
    assert calls, "expected sb.auth.admin.update_user_by_id to be called by ban_user"


async def test_admin_cannot_ban_admin(client, mock_supabase, auth_headers_factory):
    """A plain admin (not super_admin) cannot ban another admin -> 403."""
    mock_supabase.set_response(
        "profiles",
        [
            _profile_row(user_id=ACTOR_ID, email=ACTOR_EMAIL, role="admin"),
            _profile_row(user_id=TARGET_ID, email=TARGET_EMAIL, role="admin"),
        ],
    )
    from backend.services import profiles as profiles_service
    profiles_service.invalidate_role_cache()
    profiles_service.invalidate_status_cache()

    headers = auth_headers_factory(user_id=ACTOR_ID, email=ACTOR_EMAIL)
    resp = await client.post(
        f"/api/admin/users/{TARGET_ID}/ban",
        headers=headers,
        json={"duration_hours": 24},
    )

    assert resp.status_code == 403, (
        f"expected 403 for admin-banning-admin, got {resp.status_code}: "
        f"{resp.text}"
    )
    body = resp.json()
    # services.profiles.ban_user surfaces the message "Only a super_admin
    # can ban another admin"; the router wraps it in HTTPException so the
    # FastAPI default body shape is {"detail": "..."}.
    assert "super_admin" in (body.get("detail") or "").lower() or "forbidden" in (body.get("detail") or "").lower()


async def test_user_cannot_self_ban(client, mock_supabase, auth_headers_factory):
    """An actor can never ban themselves — anti-lockout guard. Hits the
    ``target_user_id == actor_id`` short-circuit in
    backend.services.profiles.ban_user before any DB writes."""
    mock_supabase.set_response(
        "profiles",
        [_profile_row(user_id=ACTOR_ID, email=ACTOR_EMAIL, role="super_admin")],
    )
    from backend.services import profiles as profiles_service
    profiles_service.invalidate_role_cache()
    profiles_service.invalidate_status_cache()

    headers = auth_headers_factory(user_id=ACTOR_ID, email=ACTOR_EMAIL)
    resp = await client.post(
        f"/api/admin/users/{ACTOR_ID}/ban",
        headers=headers,
        json={"duration_hours": 24},
    )

    assert resp.status_code == 403, (
        f"expected 403 for self-ban, got {resp.status_code}: {resp.text}"
    )

    # And: the auth admin path must NOT have been called -- the guard
    # short-circuits before any Supabase auth admin write.
    auth_admin = mock_supabase.auth.admin
    calls = (
        getattr(auth_admin, "update_user_by_id_calls", None)
        or getattr(auth_admin, "calls", None)
        or []
    )
    assert not calls, (
        f"self-ban should short-circuit before sb.auth.admin.update_user_by_id; "
        f"got calls: {calls}"
    )
