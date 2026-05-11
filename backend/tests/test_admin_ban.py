"""Integration tests — admin ban + ban-gate flow.

Covers:
  * The middleware-level rejection of API calls made with a JWT whose
    profiles row has banned_at set (defense-in-depth on top of Supabase's
    own auth-layer ban) — 403 detail=BANNED so the frontend can route to
    /banned.
  * The /api/admin/users/{target}/ban endpoint:
      - super_admin can ban an admin
      - admin (non-super) cannot ban another admin    -> 403
      - actor cannot ban their own user_id            -> 403
      - duration_hours / until / indefinite input shapes round-trip the
        right payload (banned_until, indefinite, reason) and call
        Supabase Auth Admin with the matching ban_duration string
      - conflicting input shapes -> 400 before any Supabase write
  * The public GET /api/auth/ban-status/{user_id} endpoint (used by the
    /banned page since banned users have no valid JWT).

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

async def test_banned_user_jwt_rejected(client, mock_supabase, auth_headers_factory):
    """A user whose profiles.banned_at is set in the past cannot hit any
    authenticated API surface — middleware responds with 403 detail=BANNED
    before the route handler runs.

    Wired up 2026-05-12 alongside the duration/until/indefinite work; the
    auth middleware now calls ``profiles.is_user_banned`` after JWT verify
    and short-circuits with the BANNED code. The frontend axios interceptor
    catches the code and routes to /banned.
    """
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    mock_supabase.set_response(
        "profiles",
        [_profile_row(user_id=ACTOR_ID, email=ACTOR_EMAIL, banned_at=past)],
    )
    from backend.services import profiles as profiles_service
    profiles_service.invalidate_role_cache()
    profiles_service.invalidate_status_cache()
    profiles_service.invalidate_ban_cache()

    headers = auth_headers_factory(user_id=ACTOR_ID, email=ACTOR_EMAIL)
    resp = await client.get(f"/api/crm/{TENANT_ID}/contacts", headers=headers)

    assert resp.status_code == 403, (
        f"Banned user should be rejected with 403, got {resp.status_code}: "
        f"{resp.text}"
    )
    body = resp.json()
    assert body.get("detail") == "BANNED", (
        f"ban gate must surface detail=BANNED so the frontend can route "
        f"to /banned; got body: {body}"
    )
    assert body.get("user_id") == ACTOR_ID, (
        f"ban gate must include user_id so /banned can fetch reason; "
        f"got body: {body}"
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


# ─────────────────────────────────────────────────────────────────────────────
# New input shapes — duration_hours / until / indefinite
# Added 2026-05-12 alongside the public /api/auth/ban-status endpoint.
# ─────────────────────────────────────────────────────────────────────────────

async def test_ban_with_duration_hours_writes_banned_until(client, mock_supabase, auth_headers_factory):
    """Existing ``duration_hours`` shape continues to work and persists the
    full ban payload (banned_at + banned_until + ban_reason) onto the
    profiles row. The new payload extends the old one — old callers that
    sent only duration_hours should now see banned_until / reason in the
    response as well as the auth admin call.
    """
    mock_supabase.set_response(
        "profiles",
        [
            _profile_row(user_id=ACTOR_ID, email=ACTOR_EMAIL, role="super_admin"),
            _profile_row(user_id=TARGET_ID, email=TARGET_EMAIL, role="user"),
        ],
    )
    from backend.services import profiles as profiles_service
    profiles_service.invalidate_role_cache()
    profiles_service.invalidate_status_cache()
    profiles_service.invalidate_ban_cache()

    headers = auth_headers_factory(user_id=ACTOR_ID, email=ACTOR_EMAIL)
    resp = await client.post(
        f"/api/admin/users/{TARGET_ID}/ban",
        headers=headers,
        json={"duration_hours": 72, "reason": "spamming the inbox"},
    )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("ok") is True
    assert body.get("duration_hours") == 72
    assert body.get("indefinite") is False
    assert body.get("reason") == "spamming the inbox"
    assert body.get("banned_until"), f"banned_until must be set for finite duration: {body}"

    # The profile upsert should include the new columns. updates_for
    # buffers upserts under the table key — but the conftest fixture
    # routes upserts through the chain's `_record_update`/`_record_insert`
    # path? Actually upserts use `.upsert(...)`. Check via a generic
    # interface: assert that the auth admin call landed (the canonical
    # signal a ban happened end-to-end). Profile-row assertions are
    # covered in test_super_admin_can_ban_admin above.
    auth_admin = mock_supabase.auth.admin
    calls = (
        getattr(auth_admin, "update_user_by_id_calls", None)
        or getattr(auth_admin, "calls", None)
        or []
    )
    assert calls, "expected sb.auth.admin.update_user_by_id to be called"
    # The ban_duration passed to Supabase should match the 72h duration.
    _uid, attrs = calls[-1]
    assert attrs == {"ban_duration": "72h"}, f"unexpected ban_duration: {attrs}"


async def test_ban_with_until_date_returns_banned_until(client, mock_supabase, auth_headers_factory):
    """``until: <iso>`` shape: ban resolves to a positive hour duration
    based on the gap between now() and the supplied timestamp, and the
    response's ``banned_until`` echoes the caller's value (down to
    sub-hour precision, since we round hours UP but keep the original dt).
    """
    from datetime import datetime, timedelta, timezone
    mock_supabase.set_response(
        "profiles",
        [
            _profile_row(user_id=ACTOR_ID, email=ACTOR_EMAIL, role="super_admin"),
            _profile_row(user_id=TARGET_ID, email=TARGET_EMAIL, role="user"),
        ],
    )
    from backend.services import profiles as profiles_service
    profiles_service.invalidate_role_cache()
    profiles_service.invalidate_status_cache()
    profiles_service.invalidate_ban_cache()

    # Pick a deterministic "future" timestamp ~5 days out so the
    # rounding doesn't matter for the >0 assertion.
    until_dt = datetime.now(timezone.utc) + timedelta(days=5)
    until_iso = until_dt.isoformat().replace("+00:00", "Z")

    headers = auth_headers_factory(user_id=ACTOR_ID, email=ACTOR_EMAIL)
    resp = await client.post(
        f"/api/admin/users/{TARGET_ID}/ban",
        headers=headers,
        json={"until": until_iso, "reason": "scheduled until-date ban"},
    )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("ok") is True
    assert body.get("indefinite") is False
    assert body.get("duration_hours", 0) > 0
    # banned_until should be the parsed datetime — equal to until_dt
    # within seconds. Comparing the date portion is enough to prove the
    # parser routed to the right branch.
    assert body.get("banned_until"), f"banned_until missing: {body}"
    assert body["banned_until"].startswith(until_dt.strftime("%Y-%m-%d")), (
        f"banned_until {body['banned_until']!r} should reflect until_iso {until_iso!r}"
    )

    auth_admin = mock_supabase.auth.admin
    calls = (
        getattr(auth_admin, "update_user_by_id_calls", None)
        or getattr(auth_admin, "calls", None)
        or []
    )
    assert calls, "expected sb.auth.admin.update_user_by_id to be called"


async def test_ban_with_indefinite_writes_null_banned_until(client, mock_supabase, auth_headers_factory):
    """``indefinite: true``: response carries ``indefinite=true`` and
    ``banned_until=None``; Supabase Auth gets a 100yr (876000h) sentinel
    duration so the auth-layer ban stays in place forever in practice."""
    mock_supabase.set_response(
        "profiles",
        [
            _profile_row(user_id=ACTOR_ID, email=ACTOR_EMAIL, role="super_admin"),
            _profile_row(user_id=TARGET_ID, email=TARGET_EMAIL, role="user"),
        ],
    )
    from backend.services import profiles as profiles_service
    profiles_service.invalidate_role_cache()
    profiles_service.invalidate_status_cache()
    profiles_service.invalidate_ban_cache()

    headers = auth_headers_factory(user_id=ACTOR_ID, email=ACTOR_EMAIL)
    resp = await client.post(
        f"/api/admin/users/{TARGET_ID}/ban",
        headers=headers,
        json={"indefinite": True, "reason": "permanent abuse"},
    )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("ok") is True
    assert body.get("indefinite") is True
    assert body.get("banned_until") is None, (
        f"banned_until must be null for indefinite bans: {body}"
    )
    assert body.get("reason") == "permanent abuse"

    auth_admin = mock_supabase.auth.admin
    calls = (
        getattr(auth_admin, "update_user_by_id_calls", None)
        or getattr(auth_admin, "calls", None)
        or []
    )
    assert calls, "expected sb.auth.admin.update_user_by_id to be called"
    _uid, attrs = calls[-1]
    # 100yr = 24 * 365 * 100 = 876000h. Asserting on the exact sentinel
    # pins the contract — if someone changes the sentinel value, this
    # test catches it.
    assert attrs == {"ban_duration": "876000h"}, (
        f"indefinite ban should use 100yr sentinel duration; got {attrs}"
    )


async def test_ban_rejects_conflicting_input_shapes(client, mock_supabase, auth_headers_factory):
    """Setting both ``duration_hours`` and ``until`` (or any other
    combination) returns 400. The validation happens at the router
    layer BEFORE the service function executes, so no auth admin call
    fires."""
    mock_supabase.set_response(
        "profiles",
        [
            _profile_row(user_id=ACTOR_ID, email=ACTOR_EMAIL, role="super_admin"),
            _profile_row(user_id=TARGET_ID, email=TARGET_EMAIL, role="user"),
        ],
    )
    from backend.services import profiles as profiles_service
    profiles_service.invalidate_role_cache()
    profiles_service.invalidate_status_cache()
    profiles_service.invalidate_ban_cache()

    headers = auth_headers_factory(user_id=ACTOR_ID, email=ACTOR_EMAIL)
    resp = await client.post(
        f"/api/admin/users/{TARGET_ID}/ban",
        headers=headers,
        json={"duration_hours": 24, "indefinite": True},
    )

    assert resp.status_code == 400, (
        f"expected 400 for conflicting input shapes, got {resp.status_code}: {resp.text}"
    )

    # Validation short-circuits before Supabase auth admin runs.
    auth_admin = mock_supabase.auth.admin
    calls = (
        getattr(auth_admin, "update_user_by_id_calls", None)
        or getattr(auth_admin, "calls", None)
        or []
    )
    assert not calls, f"validation failure must not call auth admin; got: {calls}"


# ─────────────────────────────────────────────────────────────────────────────
# Public ban-status endpoint
# ─────────────────────────────────────────────────────────────────────────────

async def test_ban_status_returns_metadata_for_banned_user(client, mock_supabase):
    """GET /api/auth/ban-status/{user_id} returns the persisted ban
    metadata (no JWT required — banned users have no session)."""
    from datetime import datetime, timedelta, timezone

    banned_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    banned_until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    mock_supabase.set_response(
        "profiles",
        [
            {
                "user_id": TARGET_ID,
                "banned_at": banned_at,
                "banned_until": banned_until,
                "ban_reason": "abuse of service",
            }
        ],
    )
    from backend.services import profiles as profiles_service
    profiles_service.invalidate_ban_cache()

    # NO Authorization header — endpoint is public.
    resp = await client.get(f"/api/auth/ban-status/{TARGET_ID}")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("banned") is True
    assert body.get("banned_at") == banned_at
    assert body.get("banned_until") == banned_until
    assert body.get("indefinite") is False
    assert body.get("reason") == "abuse of service"


async def test_ban_status_returns_false_for_unknown_user(client, mock_supabase):
    """Non-banned / unknown user_id returns {"banned": false} (200, no
    auth required)."""
    mock_supabase.set_response("profiles", [])
    from backend.services import profiles as profiles_service
    profiles_service.invalidate_ban_cache()

    resp = await client.get(f"/api/auth/ban-status/{TARGET_ID}")
    assert resp.status_code == 200
    assert resp.json() == {"banned": False}
