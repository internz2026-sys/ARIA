"""Admin Router — /api/admin/* endpoints.

Every route here requires the caller's profile to have role='admin' or
'super_admin'. The role check happens in the global auth middleware
(server.py) by looking up backend.services.profiles.get_user_role for
the JWT's `sub` claim. By the time a handler in this file runs, the
caller is already verified to be at least 'admin' — but mutations that
specifically need super_admin re-check via require_super_admin() before
executing.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from backend.services import profiles as profiles_service

logger = logging.getLogger("aria.routers.admin")

router = APIRouter(prefix="/api/admin", tags=["Admin"])


def _actor_from_request(request: Request) -> tuple[str, str]:
    """Pull (user_id, role) off request.state.user / request.state.role.

    Set by the auth middleware on /api/admin/* paths after it has
    verified the caller is at least 'admin'. Falling back to a 403
    keeps a misconfigured middleware from accidentally exposing the
    admin surface area.
    """
    user = getattr(request.state, "user", None) or {}
    role = getattr(request.state, "role", None) or "user"
    user_id = (user.get("sub") if isinstance(user, dict) else "") or ""
    if not user_id or role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id, role


def _require_super_admin(actor_role: str) -> None:
    if actor_role != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin only")


@router.get("/me")
async def admin_me(request: Request):
    """Echo back the calling admin's role so the frontend can light up
    super_admin-only controls without a second round-trip."""
    user_id, role = _actor_from_request(request)
    return {"user_id": user_id, "role": role}


@router.get("/users")
async def admin_list_users(
    request: Request,
    search: str = "",
    role: str = "",
    limit: int = 100,
    offset: int = 0,
):
    """List user profiles. Optional `search` (email/name ILIKE) and
    `role` (exact match) filters."""
    _actor_from_request(request)
    rows = profiles_service.list_profiles(
        search=search,
        role_filter=role,
        limit=min(limit, 500),
        offset=max(offset, 0),
    )
    return {"users": rows, "count": len(rows)}


@router.patch("/users/{target_user_id}/role")
async def admin_set_role(target_user_id: str, request: Request):
    """Change a user's role. Body: { "role": "user" | "admin" | "super_admin" }.

    Server-side guards (also in profiles_service.set_user_role):
      - Only super_admin can grant or revoke super_admin
      - Users can't change their own role (anti-lockout)
    """
    actor_id, actor_role = _actor_from_request(request)
    body = await request.json() or {}
    new_role = (body.get("role") or "").strip()

    result = profiles_service.set_user_role(
        target_user_id=target_user_id,
        new_role=new_role,
        actor_role=actor_role,
        actor_id=actor_id,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=403, detail=result.get("error") or "Forbidden")
    return result


@router.put("/users/{target_user_id}/status")
async def admin_set_status(target_user_id: str, request: Request):
    """Pause / resume / suspend a user's account.
    Body: { "status": "active" | "paused" | "suspended", "reason": "..." (optional) }.

    A 'paused' user is blocked from POST /api/ceo/chat and POST
    /api/agents/.../run by the auth middleware (server.py), but can
    still read their dashboard / inbox / history. 'suspended' uses the
    same gate today; the separation is reserved for future automated
    enforcement (billing, abuse) where we'll want different messaging.

    Server-side guards (also in profiles_service.set_user_status):
      - Only super_admin can pause/resume another admin
      - Users can never change their own status (anti-lockout)
    """
    actor_id, actor_role = _actor_from_request(request)
    body = await request.json() or {}
    new_status = (body.get("status") or "").strip()
    reason = (body.get("reason") or "").strip()

    result = profiles_service.set_user_status(
        target_user_id=target_user_id,
        new_status=new_status,
        actor_role=actor_role,
        actor_id=actor_id,
        reason=reason,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=403, detail=result.get("error") or "Forbidden")
    return result


@router.post("/users/{target_user_id}/ban")
async def admin_ban_user(target_user_id: str, request: Request):
    """Auth-layer ban — revokes login at Supabase. Body:
    { "duration_hours": int (default 8760), "reason": "..." (optional) }.

    Distinct from /status (pause/suspend), which is a soft middleware
    gate that still allows login. This calls Supabase Auth Admin so the
    user can't sign in or refresh an existing session.

    Server-side guards (also in profiles_service.ban_user):
      - admin can only ban role='user'
      - only super_admin can ban another admin
      - users can never ban themselves (anti-lockout)
    """
    actor_id, actor_role = _actor_from_request(request)
    body = await request.json() or {}
    duration_hours = body.get("duration_hours", 8760)
    reason = (body.get("reason") or "").strip()

    result = profiles_service.ban_user(
        target_user_id=target_user_id,
        actor_role=actor_role,
        actor_id=actor_id,
        duration_hours=duration_hours,
        reason=reason,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=403, detail=result.get("error") or "Forbidden")
    return result


@router.post("/users/{target_user_id}/unban")
async def admin_unban_user(target_user_id: str, request: Request):
    """Lift an auth-layer ban — restores login for the target user.

    Server-side guards (also in profiles_service.unban_user):
      - admin can only unban role='user'
      - only super_admin can unban another admin
      - users can never unban themselves
    """
    actor_id, actor_role = _actor_from_request(request)
    result = profiles_service.unban_user(
        target_user_id=target_user_id,
        actor_role=actor_role,
        actor_id=actor_id,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=403, detail=result.get("error") or "Forbidden")
    return result


@router.post("/users/{target_user_id}/reset-password")
async def admin_reset_password(target_user_id: str, request: Request):
    """Trigger a password recovery email for the target user. Supabase
    issues a recovery link and (if SMTP is configured on the project)
    emails it. The link is also returned in the response so the admin
    can copy-paste it as a fallback when email delivery is unreliable.

    Server-side guards (also in profiles_service.reset_user_password):
      - admin can only reset role='user' targets
      - super_admin can reset anyone except themselves
    """
    actor_id, actor_role = _actor_from_request(request)
    result = profiles_service.reset_user_password(
        target_user_id=target_user_id,
        actor_role=actor_role,
        actor_id=actor_id,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=403, detail=result.get("error") or "Forbidden")
    return result


@router.delete("/users/{target_user_id}")
async def admin_delete_user(target_user_id: str, request: Request):
    """Hard-delete the target user from Supabase auth + cascading
    profiles row + onboarding_drafts. Tenant-scoped data is left
    alone (see service docstring for rationale).

    Server-side guards (also in profiles_service.delete_user):
      - admin can only delete role='user'
      - super_admin can delete anyone except themselves
    """
    actor_id, actor_role = _actor_from_request(request)
    result = profiles_service.delete_user(
        target_user_id=target_user_id,
        actor_role=actor_role,
        actor_id=actor_id,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=403, detail=result.get("error") or "Forbidden")
    return result


@router.get("/stats")
async def admin_stats(request: Request):
    """High-level counts for the dashboard cards."""
    _actor_from_request(request)
    return profiles_service.system_stats()


@router.get("/agent-logs")
async def admin_agent_logs(request: Request, limit: int = 50):
    """Most recent agent runs across all tenants (admin activity feed)."""
    _actor_from_request(request)
    return {"logs": profiles_service.list_recent_agent_logs(limit=min(limit, 200))}
