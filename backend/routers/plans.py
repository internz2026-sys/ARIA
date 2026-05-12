"""Plans Router — self-service plan picker (no Stripe gate yet).

Stripe integration is deferred (see project memory
``project_stripe_deferred.md``). Until billing lands, the user toggles
their own pricing tier from the dashboard so they can exercise the new
per-plan caps in ``backend/services/plan_quotas.py``. This router
exposes the two endpoints that flow needs:

  POST /api/profile/me/plan                  self-service plan change
  POST /api/admin/users/{user_id}/plan       admin override (any tenant)

Both endpoints validate ``plan`` against the same allow-list used by
``services.plan_quotas.PLAN_LIMITS`` so a typo can't slip in. The
``self-service`` endpoint resolves the caller's tenant via
``tenant_configs.owner_email`` -- same predicate ``get_verified_tenant``
uses for all per-tenant authorization.

Why a new router instead of folding into ``routers/admin.py``: the
self-service endpoint isn't an admin operation. Keeping it separate
means the auth middleware's ``/api/admin/*`` role gate doesn't
mistakenly block a normal user from changing their own plan.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.auth import get_current_user
from backend.config.loader import (
    _config_cache,
    get_tenant_config,
    save_tenant_config,
)
from backend.services.plan_quotas import PLAN_LIMITS
from backend.services.supabase import get_db

logger = logging.getLogger("aria.routers.plans")


VALID_PLANS = frozenset(PLAN_LIMITS.keys())  # {"free","starter","growth","scale"}


# ── Request body ─────────────────────────────────────────────────────────


class PlanChangeRequest(BaseModel):
    """Body for plan-change endpoints. The frontend posts a single field.

    We do NOT accept a tenant_id in the body for /api/profile/me/plan --
    the tenant is derived from the caller's JWT so a stolen body can't
    target someone else's tenant.
    """

    plan: str


# ── Helpers ──────────────────────────────────────────────────────────────


def _validate_plan(plan: str) -> str:
    """Return the normalized plan slug or raise 400 for an invalid one.

    Lowercases and strips so the frontend doesn't have to be strict
    about casing. The allow-list comes straight from ``PLAN_LIMITS`` so
    adding a new tier is a one-line change in ``plan_quotas`` instead of
    two-spot drift between the table and this validator.
    """
    cleaned = (plan or "").strip().lower()
    if cleaned not in VALID_PLANS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid plan: {plan!r}. Must be one of "
                f"{sorted(VALID_PLANS)}"
            ),
        )
    return cleaned


def _resolve_caller_tenant_id(request: Request, user: dict) -> str:
    """Find the tenant_id owned by the authenticated user.

    Same predicate ``get_verified_tenant`` uses for the inverse direction
    (verify ownership of a given tenant): ``tenant_configs.owner_email``
    matches the JWT email claim. Raises 404 if the caller has no tenant
    yet (e.g. mid-onboarding before save-config has run).

    Returns the tenant_id as a string.
    """
    email = (
        user.get("email")
        or user.get("user_metadata", {}).get("email")
        or ""
    ).lower().strip()
    if not email:
        raise HTTPException(
            status_code=401, detail="Invalid token: no email claim"
        )

    try:
        sb = get_db()
        result = (
            sb.table("tenant_configs")
            .select("tenant_id")
            .eq("owner_email", email)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.warning("tenant lookup failed for %s: %s", email, e)
        raise HTTPException(status_code=500, detail="Tenant lookup failed")

    data = getattr(result, "data", None) or []
    if not data:
        raise HTTPException(
            status_code=404,
            detail=(
                "No tenant found for the authenticated user. "
                "Complete onboarding first."
            ),
        )
    return str(data[0].get("tenant_id"))


def _apply_plan_change(tenant_id: str, plan: str) -> dict:
    """Persist the plan change and return the updated TenantConfig dict.

    Pulls the current config, mutates ``plan``, calls ``save_tenant_config``
    (which upserts the whole row via the existing loader path). Cache
    invalidation is built into ``save_tenant_config`` -- subsequent
    ``get_tenant_config`` calls see the new plan immediately. Also
    explicitly clears the cache as a belt-and-braces measure in case the
    save retry loop dropped the plan column (older Supabase schemas
    without the migration applied would hit the column-strip path and
    leave cache mismatched).
    """
    config = get_tenant_config(tenant_id)
    config.plan = plan
    save_tenant_config(config)
    # Belt-and-braces: drop the TTL-cached entry so the very next read
    # from the same process picks up the new plan even if the save path
    # somehow failed to re-prime the cache.
    _config_cache.pop(str(tenant_id), None)
    # Return a JSON-safe representation. model_dump(mode="json")
    # serializes UUID and datetime to strings.
    return config.model_dump(mode="json")


# ── Routers ──────────────────────────────────────────────────────────────


# Self-service router lives under /api/profile so it falls under the
# generic JWT-only auth gate (no /api/admin role check).
profile_router = APIRouter(prefix="/api/profile", tags=["Plans"])

# Admin override lives under /api/admin so the role check applies.
admin_router = APIRouter(prefix="/api/admin", tags=["Plans"])


@profile_router.post("/me/plan")
async def change_my_plan(body: PlanChangeRequest, request: Request):
    """Self-service plan change for the authenticated user's own tenant.

    Body: ``{"plan": "free" | "starter" | "growth" | "scale"}``.
    No payment gate (Stripe deferred). Returns the updated TenantConfig
    so the frontend can refresh its local cache without a follow-up
    GET /me round-trip.
    """
    plan = _validate_plan(body.plan)
    user = await get_current_user(request)
    tenant_id = _resolve_caller_tenant_id(request, user)
    updated = _apply_plan_change(tenant_id, plan)
    logger.info(
        "[plans] self-service plan change: tenant=%s -> %s", tenant_id, plan
    )
    return {"ok": True, "tenant_id": tenant_id, "config": updated}


@admin_router.post("/users/{target_user_id}/plan")
async def admin_set_plan(target_user_id: str, body: PlanChangeRequest, request: Request):
    """Admin override -- set ANY tenant's plan to a specific tier.

    Used by support to bump a paying customer to scale, or to demote a
    spam-signup back to free. The /api/admin/* prefix is gated by the
    middleware-level role check (admin or super_admin only); we don't
    repeat that gate here, just trust request.state.role like the rest
    of the admin router.

    The ``target_user_id`` is the auth user's id -- we look up their
    tenant via profiles.email -> tenant_configs.owner_email.
    """
    plan = _validate_plan(body.plan)

    # Sanity-check the caller is at least admin. Mirrors the helper in
    # routers/admin.py:_actor_from_request rather than depending on it
    # (avoid cross-router private import). The middleware also enforces
    # this for any /api/admin/* path, but defense-in-depth is cheap and
    # this dep stays correct if the route is ever remounted under a
    # non-admin prefix.
    actor_user = getattr(request.state, "user", None) or {}
    role = getattr(request.state, "role", None) or "user"
    actor_id = (actor_user.get("sub") if isinstance(actor_user, dict) else "") or ""
    if not actor_id or role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    # Find the target user's tenant. profiles.user_id -> profiles.email
    # is the linking column.
    sb = get_db()
    try:
        profile_res = (
            sb.table("profiles")
            .select("email")
            .eq("user_id", target_user_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.warning("admin: profiles lookup failed for %s: %s", target_user_id, e)
        raise HTTPException(status_code=500, detail="Profile lookup failed")

    pdata = getattr(profile_res, "data", None) or []
    if not pdata:
        raise HTTPException(status_code=404, detail="Target user not found")
    target_email = (pdata[0].get("email") or "").lower().strip()

    try:
        tenant_res = (
            sb.table("tenant_configs")
            .select("tenant_id")
            .eq("owner_email", target_email)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.warning("admin: tenant lookup failed for %s: %s", target_email, e)
        raise HTTPException(status_code=500, detail="Tenant lookup failed")

    tdata = getattr(tenant_res, "data", None) or []
    if not tdata:
        raise HTTPException(
            status_code=404, detail="No tenant found for target user",
        )
    tenant_id = str(tdata[0].get("tenant_id"))

    updated = _apply_plan_change(tenant_id, plan)
    logger.info(
        "[plans] admin plan change: tenant=%s -> %s (actor_role=%s, target=%s)",
        tenant_id, plan, role, target_user_id,
    )
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "target_user_id": target_user_id,
        "config": updated,
    }
