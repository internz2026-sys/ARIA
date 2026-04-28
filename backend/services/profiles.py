"""User profiles + role-based access control.

Owns reads/writes to the `profiles` table — the single source of truth
for which Supabase auth users have admin / super_admin privileges.

Schema (run once in Supabase SQL Editor — see backend/migrations/profiles.sql):

    create table public.profiles (
      user_id uuid primary key references auth.users(id) on delete cascade,
      email text,
      full_name text,
      role text not null default 'user' check (role in ('user','admin','super_admin')),
      created_at timestamptz default now(),
      updated_at timestamptz default now()
    );

The role column is the gate every /api/admin/* route checks. Roles are
NEVER inferred from email allow-lists or env vars — always from this
table. To bootstrap the first super_admin, run a one-shot upsert in
Supabase (documented in ARIA_log.md).
"""
from __future__ import annotations

import logging
import time
from typing import Iterable

from backend.services.supabase import get_db

logger = logging.getLogger("aria.services.profiles")

_VALID_ROLES = ("user", "admin", "super_admin")
_ADMIN_ROLES = ("admin", "super_admin")

# Tiny TTL cache so the middleware doesn't query Supabase on every
# admin request. 60s is plenty — role changes are rare and the
# super_admin protection kicks in at write time anyway.
_role_cache: dict[str, tuple[float, str]] = {}
_ROLE_CACHE_TTL = 60.0


def _now() -> float:
    return time.time()


def get_user_role(user_id: str) -> str:
    """Return the role for a Supabase auth user. Defaults to 'user'.

    Misses (no profiles row) are treated as 'user' so middleware doesn't
    have to handle a None case. Failures fall back to 'user' so a
    transient DB hiccup never accidentally grants admin access.
    """
    if not user_id:
        return "user"
    cached = _role_cache.get(user_id)
    if cached and cached[0] > _now():
        return cached[1]
    try:
        sb = get_db()
        res = (
            sb.table("profiles")
            .select("role")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        role = (rows[0].get("role") if rows else "user") or "user"
        if role not in _VALID_ROLES:
            role = "user"
    except Exception as e:
        logger.warning("[profiles] role lookup failed for %s: %s — defaulting to 'user'", user_id, e)
        role = "user"
    _role_cache[user_id] = (_now() + _ROLE_CACHE_TTL, role)
    return role


def invalidate_role_cache(user_id: str | None = None) -> None:
    """Drop cached role(s) — called after a role change so the next
    request sees the new value without waiting for TTL expiry."""
    if user_id:
        _role_cache.pop(user_id, None)
    else:
        _role_cache.clear()


def is_admin(role: str) -> bool:
    return role in _ADMIN_ROLES


def is_super_admin(role: str) -> bool:
    return role == "super_admin"


def ensure_profile(user_id: str, *, email: str = "", full_name: str = "") -> None:
    """Idempotent upsert that creates a default 'user' profile row.

    Called on first admin lookup so we never have a Supabase auth user
    floating around without a profiles row. Safe to call repeatedly —
    the email/full_name update is a no-op when unchanged.
    """
    if not user_id:
        return
    try:
        sb = get_db()
        sb.table("profiles").upsert(
            {
                "user_id": user_id,
                "email": email or None,
                "full_name": full_name or None,
            },
            on_conflict="user_id",
        ).execute()
    except Exception as e:
        logger.debug("[profiles] ensure_profile failed (likely benign): %s", e)


def list_profiles(
    *,
    search: str = "",
    role_filter: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Return profiles for the admin user table. Search hits email +
    full_name (ILIKE). Role filter narrows by exact match."""
    try:
        sb = get_db()
        q = (
            sb.table("profiles")
            .select("user_id, email, full_name, role, created_at, updated_at")
            .order("created_at", desc=True)
            .range(offset, offset + max(0, limit - 1))
        )
        if role_filter and role_filter in _VALID_ROLES:
            q = q.eq("role", role_filter)
        if search:
            esc = search.replace(",", " ").replace("(", "").replace(")", "")
            q = q.or_(f"email.ilike.%{esc}%,full_name.ilike.%{esc}%")
        res = q.execute()
        return list(res.data or [])
    except Exception as e:
        logger.warning("[profiles] list failed: %s", e)
        return []


def set_user_role(*, target_user_id: str, new_role: str, actor_role: str, actor_id: str) -> dict:
    """Change a user's role. Returns {ok, error?}.

    Guards (enforced server-side, not just UI):
      - new_role must be a valid role
      - only super_admin can grant or revoke super_admin
      - admin can promote user <-> admin but cannot touch super_admin rows
      - users can never demote themselves (prevents lockouts)
    """
    if new_role not in _VALID_ROLES:
        return {"ok": False, "error": f"Invalid role: {new_role}"}
    if not target_user_id:
        return {"ok": False, "error": "target_user_id required"}
    if target_user_id == actor_id and new_role != actor_role:
        return {"ok": False, "error": "You can't change your own role"}

    current_role = get_user_role(target_user_id)

    if not is_admin(actor_role):
        return {"ok": False, "error": "Forbidden"}

    if not is_super_admin(actor_role):
        # admins can only juggle user <-> admin; super_admin rows are off-limits
        if current_role == "super_admin" or new_role == "super_admin":
            return {"ok": False, "error": "Only a super_admin can set or remove super_admin"}

    try:
        sb = get_db()
        # Upsert so the call works whether or not a profiles row exists
        # already (Supabase auth user could pre-date the profiles table).
        sb.table("profiles").upsert(
            {"user_id": target_user_id, "role": new_role},
            on_conflict="user_id",
        ).execute()
        invalidate_role_cache(target_user_id)
        return {"ok": True, "role": new_role, "previous_role": current_role}
    except Exception as e:
        logger.error("[profiles] set_user_role failed: %s", e)
        return {"ok": False, "error": "Database update failed"}


def system_stats() -> dict:
    """High-level stats for the admin dashboard. All best-effort —
    individual count failures fall back to 0 rather than crashing."""
    sb = get_db()
    out: dict = {
        "users_total": 0,
        "users_admin": 0,
        "tenants_total": 0,
        "agent_runs_total": 0,
        "inbox_items_total": 0,
    }

    def _count(table: str, **filters) -> int:
        try:
            q = sb.table(table).select("id", count="exact").limit(1)
            for k, v in filters.items():
                q = q.eq(k, v)
            res = q.execute()
            return int(getattr(res, "count", 0) or 0)
        except Exception as e:
            logger.debug("[profiles] count(%s) failed: %s", table, e)
            return 0

    try:
        users = sb.table("profiles").select("role", count="exact").execute()
        out["users_total"] = int(getattr(users, "count", 0) or 0)
        admins = sb.table("profiles").select("role", count="exact").in_("role", list(_ADMIN_ROLES)).execute()
        out["users_admin"] = int(getattr(admins, "count", 0) or 0)
    except Exception as e:
        logger.debug("[profiles] user counts failed: %s", e)

    out["tenants_total"] = _count("tenant_configs")
    out["agent_runs_total"] = _count("agent_logs")
    out["inbox_items_total"] = _count("inbox_items")
    return out


def list_recent_agent_logs(limit: int = 50) -> list[dict]:
    """Most recent agent runs across all tenants — for the admin
    activity feed. Failures return [] so the dashboard still loads."""
    try:
        sb = get_db()
        res = (
            sb.table("agent_logs")
            .select("id, tenant_id, agent, status, created_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return list(res.data or [])
    except Exception:
        return []
