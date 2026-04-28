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
