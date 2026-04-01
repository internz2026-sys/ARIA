"""Authentication & authorization middleware for ARIA API.

Verifies Supabase JWT tokens and checks tenant ownership.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt

logger = logging.getLogger("aria.auth")

# Supabase JWT secret — this is your project's JWT secret from Supabase dashboard
# Settings > API > JWT Secret
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")


@lru_cache()
def _get_jwt_secret() -> str:
    secret = os.getenv("SUPABASE_JWT_SECRET", "")
    if not secret:
        logger.warning("SUPABASE_JWT_SECRET not set — auth will be disabled")
    return secret


def _extract_token(request: Request) -> str | None:
    """Extract Bearer token from Authorization header."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def verify_jwt(token: str) -> dict:
    """Verify a Supabase JWT and return the payload."""
    secret = _get_jwt_secret()
    if not secret:
        raise HTTPException(status_code=500, detail="Auth not configured")

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
        return payload
    except JWTError as e:
        logger.warning("JWT verification failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def get_current_user(request: Request) -> dict:
    """FastAPI dependency: extract and verify JWT, return user payload.

    Returns dict with keys: sub (user_id), email, role, etc.
    """
    secret = _get_jwt_secret()

    # If JWT secret is not configured, allow unauthenticated access (dev mode)
    if not secret:
        return {"sub": "dev-user", "email": "dev@localhost", "role": "authenticated"}

    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing authorization token")

    return verify_jwt(token)


async def get_verified_tenant(request: Request, tenant_id: str) -> dict:
    """Verify the authenticated user owns the given tenant_id.

    Returns the user payload if authorized.
    Raises 403 if the user doesn't own the tenant.
    """
    user = await get_current_user(request)

    # Dev mode — skip ownership check
    if user.get("sub") == "dev-user":
        return user

    user_email = user.get("email", "")
    user_id = user.get("sub", "")

    if not user_email and not user_id:
        raise HTTPException(status_code=401, detail="Invalid token: no user identity")

    # Check tenant ownership
    try:
        from backend.config.loader import get_tenant_config
        config = get_tenant_config(tenant_id)

        # Verify ownership by email or by matching user_id in tenant_id
        if config.owner_email and config.owner_email == user_email:
            return user

        # Also check if the tenant_id itself matches the user_id (some setups)
        if str(config.tenant_id) == user_id:
            return user

        # If tenant has no owner_email set, allow access (legacy/migration)
        if not config.owner_email:
            logger.warning("Tenant %s has no owner_email — allowing access", tenant_id)
            return user

    except Exception as e:
        logger.error("Tenant ownership check failed: %s", e)
        raise HTTPException(status_code=404, detail="Tenant not found")

    raise HTTPException(status_code=403, detail="You don't have access to this tenant")


# ── Rate limiting helpers ────────────────────────────────────────────────────

# Simple in-memory rate limiter (per IP)
_rate_limits: dict[str, list[float]] = {}


def check_rate_limit(request: Request, max_requests: int = 60, window_seconds: int = 60):
    """Simple in-memory rate limiter by IP. Raises 429 if exceeded."""
    import time
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    cutoff = now - window_seconds

    if client_ip not in _rate_limits:
        _rate_limits[client_ip] = []

    # Clean old entries
    _rate_limits[client_ip] = [t for t in _rate_limits[client_ip] if t > cutoff]

    if len(_rate_limits[client_ip]) >= max_requests:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")

    _rate_limits[client_ip].append(now)
