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
Supabase (documented in docs/ARIA_log.md).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

from backend.services.supabase import get_db
from backend.services._postgrest_util import safe_or_value

logger = logging.getLogger("aria.services.profiles")

_VALID_ROLES = ("user", "admin", "super_admin")
_ADMIN_ROLES = ("admin", "super_admin")
_VALID_STATUSES = ("active", "paused", "suspended")
_BLOCKED_STATUSES = ("paused", "suspended")

# Tiny TTL cache so the middleware doesn't query Supabase on every
# admin request. 60s is plenty — role changes are rare and the
# super_admin protection kicks in at write time anyway.
_role_cache: dict[str, tuple[float, str]] = {}
_ROLE_CACHE_TTL = 60.0

# Status cache mirrors the role cache. Same 60s TTL — when an admin
# pauses a user, set_user_status() invalidates this cache immediately
# so the next request from the paused user sees the new status without
# waiting for TTL expiry.
_status_cache: dict[str, tuple[float, str]] = {}
_STATUS_CACHE_TTL = 60.0


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


def is_paused(status: str) -> bool:
    """True for any status that should block expensive actions (paused or
    suspended). The pause-gate middleware uses this — keeping a single
    helper means a future status like 'billing_hold' can be added in one
    place."""
    return status in _BLOCKED_STATUSES


def get_user_status(user_id: str) -> str:
    """Return the account status for a Supabase auth user. Defaults to
    'active'. Same failure semantics as get_user_role — a transient DB
    error returns 'active' so we never accidentally lock a user out."""
    if not user_id:
        return "active"
    cached = _status_cache.get(user_id)
    if cached and cached[0] > _now():
        return cached[1]
    try:
        sb = get_db()
        res = (
            sb.table("profiles")
            .select("status")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        status = (rows[0].get("status") if rows else "active") or "active"
        if status not in _VALID_STATUSES:
            status = "active"
    except Exception as e:
        logger.warning("[profiles] status lookup failed for %s: %s — defaulting to 'active'", user_id, e)
        status = "active"
    _status_cache[user_id] = (_now() + _STATUS_CACHE_TTL, status)
    return status


def invalidate_status_cache(user_id: str | None = None) -> None:
    """Drop cached status(es) — called after a status change so the
    next request sees the new value without waiting for TTL expiry."""
    if user_id:
        _status_cache.pop(user_id, None)
    else:
        _status_cache.clear()


# Tiny TTL cache for the per-user ban check the middleware runs on every
# authenticated request. 30s TTL keeps the request-path cost low (one
# Supabase round-trip per minute per user at worst) while still firing
# the ban within a window any human would tolerate.
_ban_cache: dict[str, tuple[float, bool]] = {}
_BAN_CACHE_TTL = 30.0


def is_user_banned(user_id: str) -> bool:
    """Lightweight check for the auth middleware: is this user banned?

    Returns True iff the profiles row has banned_at set. Cached for 30s
    to keep the middleware hot path cheap — the canonical source of
    truth is still Supabase Auth (which will reject the JWT on its own
    once the access token expires + refresh fails), this is a
    defense-in-depth gate that catches the window before that happens.

    Failure mode: a DB hiccup returns False (NOT banned) so a transient
    Supabase outage doesn't accidentally lock everyone out. The
    auth-layer ban in Supabase remains the canonical enforcer if this
    layer is unreachable.
    """
    if not user_id:
        return False
    cached = _ban_cache.get(user_id)
    if cached and cached[0] > _now():
        return cached[1]
    banned = False
    try:
        sb = get_db()
        res = (
            sb.table("profiles")
            .select("banned_at")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        banned = bool(rows and rows[0].get("banned_at"))
    except Exception as e:
        logger.debug("[profiles] is_user_banned lookup failed for %s: %s", user_id, e)
        banned = False
    _ban_cache[user_id] = (_now() + _BAN_CACHE_TTL, banned)
    return banned


def invalidate_ban_cache(user_id: str | None = None) -> None:
    """Drop cached ban check(s) — called after ban_user / unban_user
    so the next request sees the new state without waiting for TTL."""
    if user_id:
        _ban_cache.pop(user_id, None)
    else:
        _ban_cache.clear()


def set_user_status(*, target_user_id: str, new_status: str, actor_role: str, actor_id: str, reason: str = "") -> dict:
    """Pause / resume / suspend a user. Returns {ok, error?}.

    Guards (server-enforced — UI hints are advisory):
      - new_status must be a valid status
      - actor must be admin or super_admin
      - admin can only pause/resume role='user' targets
      - super_admin can act on anyone except themselves (anti-lockout —
        if you accidentally paused yourself you couldn't unpause)
      - users can never set their own status (handled by the actor=target check)
    """
    if new_status not in _VALID_STATUSES:
        return {"ok": False, "error": f"Invalid status: {new_status}"}
    if not target_user_id:
        return {"ok": False, "error": "target_user_id required"}
    if not is_admin(actor_role):
        return {"ok": False, "error": "Forbidden"}
    if target_user_id == actor_id:
        return {"ok": False, "error": "You can't change your own account status"}

    target_role = get_user_role(target_user_id)
    if not is_super_admin(actor_role) and target_role != "user":
        return {"ok": False, "error": "Only a super_admin can pause another admin"}

    try:
        sb = get_db()
        # Upsert keeps this working even if a profiles row is missing
        # (auth.users older than the profiles table). The role isn't
        # touched — only the status column.
        sb.table("profiles").upsert(
            {"user_id": target_user_id, "status": new_status},
            on_conflict="user_id",
        ).execute()
        invalidate_status_cache(target_user_id)
        logger.warning(
            "[admin] %s (%s) set status=%s on user %s%s",
            actor_id, actor_role, new_status, target_user_id,
            f" (reason: {reason})" if reason else "",
        )
        return {"ok": True, "status": new_status}
    except Exception as e:
        logger.error("[profiles] set_user_status failed: %s", e)
        return {"ok": False, "error": "Database update failed"}


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
            .select("user_id, email, full_name, role, status, banned_at, created_at, updated_at")
            .order("created_at", desc=True)
            .range(offset, offset + max(0, limit - 1))
        )
        if role_filter and role_filter in _VALID_ROLES:
            q = q.eq("role", role_filter)
        if search:
            needle = safe_or_value(f"%{search}%")
            q = q.or_(f"email.ilike.{needle},full_name.ilike.{needle}")
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


def reset_user_password(*, target_user_id: str, actor_role: str, actor_id: str) -> dict:
    """Trigger a password-recovery email for `target_user_id`. Returns
    {ok, email?, action_link?, error?}.

    Uses Supabase's admin generate_link with type='recovery', which:
      1. Issues a signed recovery URL the user can click to set a new password
      2. Emails the URL via the project's SMTP config (if configured)

    The `action_link` is returned to the admin in the response so they
    have a copy-able fallback when the project's SMTP is misconfigured —
    they can paste it into Slack/etc. Don't log the link.

    Permission rules (server-enforced — UI hints elsewhere are advisory):
      - actor must be admin or super_admin
      - admin can reset only role='user' targets (no peer- or super-resets)
      - super_admin can reset anyone except themselves (use the public
        forgot-password flow for self-reset to keep an audit gap)
    """
    if not target_user_id:
        return {"ok": False, "error": "target_user_id required"}
    if not is_admin(actor_role):
        return {"ok": False, "error": "Forbidden"}
    if target_user_id == actor_id:
        return {"ok": False, "error": "Use the public forgot-password flow to reset your own password"}

    target_role = get_user_role(target_user_id)
    if not is_super_admin(actor_role) and target_role != "user":
        return {"ok": False, "error": "Only a super_admin can reset another admin's password"}

    sb = get_db()

    # Need the email — Supabase's generate_link is keyed on email, not
    # user_id. Pull it from auth.users via the admin API.
    try:
        target = sb.auth.admin.get_user_by_id(target_user_id)
        # supabase-py wraps the response in a model; the `.user` attr holds
        # the actual record. Older versions return the dict directly.
        user_obj = getattr(target, "user", None) or target
        target_email = (
            getattr(user_obj, "email", None)
            or (user_obj.get("email") if isinstance(user_obj, dict) else None)
            or ""
        )
    except Exception as e:
        logger.warning("[profiles] target lookup failed for %s: %s", target_user_id, e)
        return {"ok": False, "error": "Target user not found"}

    if not target_email:
        return {"ok": False, "error": "Target user has no email on file"}

    try:
        link_res = sb.auth.admin.generate_link({
            "type": "recovery",
            "email": target_email,
        })
    except Exception as e:
        logger.error("[profiles] generate_link failed for %s: %s", target_user_id, e)
        return {"ok": False, "error": "Could not generate recovery link"}

    # Different supabase-py versions surface the link in slightly
    # different shapes — try both common ones, fall back to None.
    action_link = (
        getattr(link_res, "action_link", None)
        or getattr(getattr(link_res, "properties", None), "action_link", None)
        or (link_res.get("action_link") if isinstance(link_res, dict) else None)
        or (link_res.get("properties", {}).get("action_link")
            if isinstance(link_res, dict) and isinstance(link_res.get("properties"), dict) else None)
    )

    logger.info(
        "[admin] %s (%s) triggered password reset for %s (%s)",
        actor_id, actor_role, target_user_id, target_email,
    )
    return {"ok": True, "email": target_email, "action_link": action_link}


def delete_user(*, target_user_id: str, actor_role: str, actor_id: str) -> dict:
    """Hard-delete a user from Supabase auth and clean up their data.

    Cascade behavior:
      - `auth.users` delete cascades `profiles` (FK with on delete cascade)
      - `onboarding_drafts` rows are removed explicitly (no FK cascade)
      - Tenant-scoped data (inbox_items, agent_logs, etc) is INTENTIONALLY
        left alone — a tenant can have multiple users, and nuking the
        tenant just because one user is being removed could destroy a
        whole company's content. If the deleted user was the only owner
        of a tenant, the orphaned data has to be cleaned up via tenant
        management separately.

    Permission rules (mirror the password-reset rules):
      - actor must be admin or super_admin
      - admin can only delete role='user'
      - super_admin can delete anyone except themselves (anti-lockout —
        if the last super_admin needs to leave, promote another first)
    """
    if not target_user_id:
        return {"ok": False, "error": "target_user_id required"}
    if not is_admin(actor_role):
        return {"ok": False, "error": "Forbidden"}
    if target_user_id == actor_id:
        return {"ok": False, "error": "You can't delete your own account from the admin panel"}

    target_role = get_user_role(target_user_id)
    if not is_super_admin(actor_role) and target_role != "user":
        return {"ok": False, "error": "Only a super_admin can delete another admin"}

    sb = get_db()

    # Capture the email BEFORE deletion so we can log it / surface it to
    # the caller. After the auth.users row is gone the profiles cascade
    # has fired and we can't look it up anymore.
    target_email = ""
    try:
        target = sb.auth.admin.get_user_by_id(target_user_id)
        user_obj = getattr(target, "user", None) or target
        target_email = (
            getattr(user_obj, "email", None)
            or (user_obj.get("email") if isinstance(user_obj, dict) else None)
            or ""
        )
    except Exception:
        # Non-fatal — we'll still attempt the delete.
        pass

    cleanup = {"profiles": False, "onboarding_drafts": 0, "auth_user": False}

    # 1. Best-effort onboarding_drafts cleanup. Done BEFORE auth.users
    #    deletion in case our own profiles row gets cascaded out before
    #    we can reference it (the drafts table keys on user_id text).
    try:
        res = sb.table("onboarding_drafts").delete().eq("user_id", target_user_id).execute()
        cleanup["onboarding_drafts"] = len(res.data or [])
    except Exception as e:
        logger.debug("[profiles] onboarding_drafts cleanup failed (non-fatal): %s", e)

    # 2. Delete the auth user — this cascades the profiles row.
    try:
        sb.auth.admin.delete_user(target_user_id)
        cleanup["auth_user"] = True
        cleanup["profiles"] = True  # cascaded
    except Exception as e:
        logger.error("[profiles] delete_user failed for %s: %s", target_user_id, e)
        return {"ok": False, "error": "Could not delete user from auth"}

    invalidate_role_cache(target_user_id)
    logger.warning(
        "[admin] %s (%s) DELETED user %s (%s) — cleanup: %s",
        actor_id, actor_role, target_user_id, target_email or "no-email", cleanup,
    )
    return {"ok": True, "email": target_email, "cleanup": cleanup}


_DEFAULT_BAN_HOURS = 8760  # one year — Supabase's recommended "indefinite" sentinel
_MAX_BAN_HOURS = 24 * 365 * 10  # 10 years; defensive upper bound on caller input
# Supabase Auth doesn't have a true "indefinite" flag — set the
# ban_duration to 100yr (876,000h) as a sentinel that effectively means
# "forever". The profiles row also gets banned_until=NULL so callers
# can detect indefinite without parsing the huge timestamp.
_INDEFINITE_BAN_HOURS = 24 * 365 * 100  # 876,000h ≈ 100yr


def _parse_until_to_hours(until_value, *, now: "datetime | None" = None) -> "tuple[int | None, datetime | None, str | None]":
    """Translate an ISO-8601 ``until`` timestamp into a positive hour
    duration relative to ``now`` (default: utcnow).

    Returns ``(hours, parsed_dt, error)``:
      * ``hours`` is rounded UP so a ``until`` 30s in the future doesn't
        round to 0 hours and trip the "duration_hours must be positive"
        guard. Capped at ``_MAX_BAN_HOURS``.
      * ``parsed_dt`` is the canonical timezone-aware datetime (UTC if
        the input lacked tzinfo) so the caller can use it verbatim for
        ``banned_until`` instead of recomputing from ``hours`` (which
        loses sub-hour precision).
      * ``error`` is non-None when the input is unparseable / in the
        past; in that case ``hours`` and ``parsed_dt`` are both None.
    """
    if until_value is None or until_value == "":
        return None, None, "until must be a non-empty ISO-8601 datetime"

    if not isinstance(until_value, str):
        return None, None, "until must be a string"

    raw = until_value.strip()
    # Python's fromisoformat accepts "2026-12-31T23:59:59+00:00" but not
    # the "Z" suffix Supabase / Postgres / JS Date all emit. Normalise.
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None, None, "until must be an ISO-8601 datetime"

    if parsed.tzinfo is None:
        # Naive datetime — assume UTC. Stamping a local-time naive
        # datetime as banned_until would either lock the user out
        # longer than intended (server in UTC, value in +10) or shorter
        # (server in UTC, value in -08). UTC-assume keeps the
        # interpretation predictable.
        parsed = parsed.replace(tzinfo=timezone.utc)

    current = now or datetime.now(timezone.utc)
    delta = parsed - current
    seconds = delta.total_seconds()
    if seconds <= 0:
        return None, None, "until must be in the future"

    # Round UP so any positive interval yields ≥1 hour. Supabase's
    # ban_duration is whole hours so we always over-estimate slightly
    # rather than risk a 0h ban that no-ops.
    hours = max(1, int((seconds + 3599) // 3600))
    if hours > _MAX_BAN_HOURS:
        hours = _MAX_BAN_HOURS
    return hours, parsed, None


def ban_user(
    *,
    target_user_id: str,
    actor_role: str,
    actor_id: str,
    duration_hours: "int | None" = _DEFAULT_BAN_HOURS,
    reason: str = "",
    until: "str | None" = None,
    indefinite: bool = False,
) -> dict:
    """Ban a user at the Supabase auth layer (revokes login).

    Three ways to specify the ban length, exactly one must be provided
    (the admin router validates this; this function is defensive too):

      * ``duration_hours=N``   — ban for N hours (existing shape)
      * ``until="<ISO-8601>"`` — ban until a specific UTC timestamp
      * ``indefinite=True``    — ban forever (Supabase has no true
                                  forever flag; we use a 100-year
                                  sentinel and store banned_until=NULL)

    Unlike `set_user_status` (a soft pause that still allows login),
    this calls `auth.admin.update_user_by_id` with a `ban_duration` so
    Supabase rejects new sign-ins and invalidates existing sessions on
    the next refresh.

    We stamp the profiles row with `banned_at = now()`,
    `banned_until = <computed timestamp or NULL>`, and
    `ban_reason = <reason or NULL>` so the public ban-status endpoint
    and admin UI can read everything off the profiles row without
    hitting Supabase Auth Admin on every request.

    Guards mirror set_user_status (and the other admin mutations):
      - actor must be admin or super_admin
      - admin can only ban role='user'; only super_admin may ban another admin
      - users can never ban themselves (anti-lockout)
    """
    if not target_user_id:
        return {"ok": False, "error": "target_user_id required"}
    if not is_admin(actor_role):
        return {"ok": False, "error": "Forbidden"}
    if target_user_id == actor_id:
        return {"ok": False, "error": "You can't ban your own account"}

    # Resolve the three input shapes to a concrete hours + banned_until.
    # `is_indefinite` is the bit that drives:
    #   1. the Supabase ban_duration sentinel (100yr)
    #   2. the banned_until column (NULL instead of 2126-01-01)
    #   3. the response payload (`indefinite: true` for UI clarity)
    banned_at = datetime.now(timezone.utc)
    is_indefinite = False
    explicit_until: "datetime | None" = None

    if indefinite:
        # If the caller mixed indefinite=True with duration/until, ignore
        # the others — indefinite is the strongest signal.
        hours = _INDEFINITE_BAN_HOURS
        is_indefinite = True
    elif until is not None and until != "":
        parsed_hours, parsed_dt, err = _parse_until_to_hours(until, now=banned_at)
        if err is not None or parsed_hours is None:
            return {"ok": False, "error": err or "Invalid until value"}
        hours = parsed_hours
        explicit_until = parsed_dt
    else:
        try:
            hours = int(duration_hours) if duration_hours is not None else _DEFAULT_BAN_HOURS
        except (TypeError, ValueError):
            return {"ok": False, "error": "duration_hours must be an integer"}
        if hours <= 0:
            return {"ok": False, "error": "duration_hours must be positive"}
        if hours > _MAX_BAN_HOURS:
            hours = _MAX_BAN_HOURS

    target_role = get_user_role(target_user_id)
    if not is_super_admin(actor_role) and target_role != "user":
        return {"ok": False, "error": "Only a super_admin can ban another admin"}

    sb = get_db()

    # 1. Auth-layer ban via the Admin API. Supabase accepts a duration
    #    string like "8760h" — this is the gate that actually prevents
    #    login. Failures here are fatal: we don't update the profile
    #    row if auth wasn't actually banned (otherwise the badge lies).
    try:
        sb.auth.admin.update_user_by_id(
            target_user_id,
            {"ban_duration": f"{hours}h"},
        )
    except Exception as e:
        logger.error("[profiles] ban_user (auth update) failed for %s: %s", target_user_id, e)
        return {"ok": False, "error": "Could not ban user at auth layer"}

    # banned_until: prefer the caller-supplied ISO datetime verbatim
    # (more precise than recomputing from hours), fall back to computed
    # offset for the duration_hours / default path. NULL for indefinite.
    if is_indefinite:
        banned_until: "datetime | None" = None
    elif explicit_until is not None:
        banned_until = explicit_until
    else:
        banned_until = banned_at + timedelta(hours=hours)

    # 2. Mirror the ban onto the profiles row so the admin UI + the
    #    public /api/auth/ban-status endpoint can read everything off
    #    one row without a second Auth Admin call. Non-fatal — auth is
    #    the source of truth for whether login works; these columns are
    #    just badge / context hints.
    profile_payload: dict = {
        "user_id": target_user_id,
        "banned_at": banned_at.isoformat(),
        "banned_until": banned_until.isoformat() if banned_until else None,
        "ban_reason": (reason or None),
    }
    try:
        sb.table("profiles").upsert(profile_payload, on_conflict="user_id").execute()
    except Exception as e:
        logger.warning("[profiles] ban_user profile upsert failed (auth ban succeeded): %s", e)

    # The status cache is keyed on the `status` column which we don't
    # touch here, but invalidating role/status caches keeps any future
    # gate that consults them from serving stale data after a ban.
    invalidate_status_cache(target_user_id)
    invalidate_ban_cache(target_user_id)

    logger.warning(
        "[admin] %s (%s) BANNED user %s for %sh%s%s",
        actor_id, actor_role, target_user_id, hours,
        " (indefinite)" if is_indefinite else "",
        f" (reason: {reason})" if reason else "",
    )
    return {
        "ok": True,
        "banned_at": banned_at.isoformat(),
        "banned_until": banned_until.isoformat() if banned_until else None,
        "indefinite": is_indefinite,
        "duration_hours": hours,
        "reason": (reason or None),
    }


def unban_user(*, target_user_id: str, actor_role: str, actor_id: str) -> dict:
    """Lift an auth-layer ban — restores login for the target user.

    Supabase's convention is to set `ban_duration: "none"` to clear an
    existing ban. We also clear `profiles.banned_at` so the badge
    disappears from the admin UI.

    Guards mirror ban_user:
      - actor must be admin or super_admin
      - admin can only unban role='user'; only super_admin may unban another admin
      - users can never unban themselves (no-op anyway — they can't log in)
    """
    if not target_user_id:
        return {"ok": False, "error": "target_user_id required"}
    if not is_admin(actor_role):
        return {"ok": False, "error": "Forbidden"}
    if target_user_id == actor_id:
        return {"ok": False, "error": "You can't unban your own account"}

    target_role = get_user_role(target_user_id)
    if not is_super_admin(actor_role) and target_role != "user":
        return {"ok": False, "error": "Only a super_admin can unban another admin"}

    sb = get_db()

    try:
        sb.auth.admin.update_user_by_id(
            target_user_id,
            {"ban_duration": "none"},
        )
    except Exception as e:
        logger.error("[profiles] unban_user (auth update) failed for %s: %s", target_user_id, e)
        return {"ok": False, "error": "Could not unban user at auth layer"}

    try:
        sb.table("profiles").upsert(
            {
                "user_id": target_user_id,
                "banned_at": None,
                "banned_until": None,
                "ban_reason": None,
            },
            on_conflict="user_id",
        ).execute()
    except Exception as e:
        logger.warning("[profiles] unban_user profile upsert failed (auth unban succeeded): %s", e)

    invalidate_status_cache(target_user_id)
    invalidate_ban_cache(target_user_id)

    logger.warning("[admin] %s (%s) UNBANNED user %s", actor_id, actor_role, target_user_id)
    return {"ok": True}


def get_ban_status(user_id: str) -> dict:
    """Read ban metadata for a user_id off the profiles row.

    Used by ``GET /api/auth/ban-status/{user_id}`` — a public endpoint
    so the /banned page can fetch the reason / timestamps without a
    valid session (banned users don't have one). Returns:

        {
          "banned": true,
          "banned_at": "...",
          "banned_until": "..." | null,   # null when indefinite
          "indefinite": false,
          "reason": "..." | null,
        }

    Or ``{"banned": false}`` for non-banned / unknown users.

    Implementation notes:

      * "Banned" is defined as ``banned_at IS NOT NULL`` on the
        profiles row. We deliberately DO NOT also check ``banned_until
        > now()`` here: if Supabase Auth still has an active ban,
        login won't work, and the user should see the banned page
        until the auth-layer ban also expires. Time-based cleanup
        of expired bans is a separate concern (future cron job).
      * ``indefinite`` is the boolean the frontend toggles its copy on
        ("You are permanently banned" vs "Banned until 2026-12-31").
        It's derived from ``banned_until IS NULL`` on the row — see
        ``ban_user`` which writes NULL specifically to mark
        indefiniteness.
      * Failures fall back to ``{"banned": false}`` — denying ban status
        is safer than 500ing the /banned page, which would loop the
        user back to a generic error screen.
    """
    if not user_id:
        return {"banned": False}
    try:
        sb = get_db()
        res = (
            sb.table("profiles")
            .select("email, banned_at, banned_until, ban_reason")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        logger.warning("[profiles] get_ban_status lookup failed for %s: %s", user_id, e)
        return {"banned": False}

    if not rows:
        return {"banned": False}

    row = rows[0] or {}
    banned_at = row.get("banned_at")
    if not banned_at:
        return {"banned": False}

    banned_until = row.get("banned_until")
    return {
        "banned": True,
        "email": row.get("email"),
        "banned_at": banned_at,
        "banned_until": banned_until,
        "indefinite": banned_until is None,
        "reason": row.get("ban_reason"),
    }


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
