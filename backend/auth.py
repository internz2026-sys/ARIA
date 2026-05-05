"""Authentication & authorization middleware for ARIA API.

Verifies Supabase JWT tokens and checks tenant ownership.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

import httpx
from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt

logger = logging.getLogger("aria.auth")

# Supabase JWT keys — supports both HS256 (legacy) and ES256 (ECC)
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
SUPABASE_JWT_ECC_PUBLIC_KEY = os.getenv("SUPABASE_JWT_ECC_PUBLIC_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")

# Cache for JWKS public keys
_jwks_cache: dict | None = None


@lru_cache()
def _get_jwt_secret() -> str:
    secret = os.getenv("SUPABASE_JWT_SECRET", "")
    if not secret and not SUPABASE_URL:
        logger.warning("SUPABASE_JWT_SECRET not set — auth will be disabled")
    return secret


def _get_jwks() -> dict | None:
    """Fetch Supabase JWKS (JSON Web Key Set) for ES256 verification."""
    global _jwks_cache
    if _jwks_cache:
        return _jwks_cache
    if not SUPABASE_URL:
        return None
    try:
        jwks_url = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
        resp = httpx.get(jwks_url, timeout=5)
        if resp.status_code == 200:
            _jwks_cache = resp.json()
            logger.info("Loaded Supabase JWKS for ES256 verification")
            return _jwks_cache
    except Exception as e:
        logger.warning("Failed to fetch JWKS: %s", e)
    return None


def _extract_token(request: Request) -> str | None:
    """Extract Bearer token from Authorization header."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def verify_jwt(token: str) -> dict:
    """Verify a Supabase JWT and return the payload.

    Supports both:
    - HS256 (legacy JWT secret)
    - ES256 (new Supabase JWT signing keys via JWKS)
    """
    # Peek at the token header to determine algorithm
    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token format")

    alg = header.get("alg", "HS256")

    try:
        if alg == "ES256":
            ecc_key = os.getenv("SUPABASE_JWT_ECC_PUBLIC_KEY", "")

            if ecc_key:
                # Use ECC public key from env var (fastest — no network call)
                import json as _json
                try:
                    key_data = _json.loads(ecc_key) if ecc_key.startswith("{") else {"kty": "EC", "crv": "P-256", "x": "", "y": ""}
                    from jose import jwk
                    public_key = jwk.construct(key_data, algorithm="ES256")
                except Exception:
                    # Fall through to JWKS
                    ecc_key = ""

            if not ecc_key:
                # Fallback: fetch from Supabase JWKS endpoint
                jwks = _get_jwks()
                if not jwks:
                    raise HTTPException(status_code=500, detail="JWKS not available for ES256 verification")

                kid = header.get("kid", "")
                key_data = None
                for k in jwks.get("keys", []):
                    if k.get("kid") == kid:
                        key_data = k
                        break

                if not key_data:
                    # Key not found — maybe keys rotated. Clear cache and retry once.
                    global _jwks_cache
                    _jwks_cache = None
                    jwks = _get_jwks()
                    if jwks:
                        for k in jwks.get("keys", []):
                            if k.get("kid") == kid:
                                key_data = k
                                break
                    if not key_data:
                        raise HTTPException(status_code=401, detail="JWT signing key not found")

                from jose import jwk
                public_key = jwk.construct(key_data, algorithm="ES256")

            payload = jwt.decode(
                token,
                public_key,
                algorithms=["ES256"],
                audience="authenticated",
            )
        else:
            # HS256 with shared secret
            secret = _get_jwt_secret()
            if not secret:
                raise HTTPException(status_code=500, detail="Auth not configured")

            payload = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience="authenticated",
            )

        return payload
    except HTTPException:
        raise
    except JWTError as e:
        logger.warning("JWT verification failed (%s): %s", alg, e)
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


async def check_user_active(request: Request) -> dict:
    """FastAPI dependency: ensure the caller's account isn't paused/suspended.

    Use this on per-route handlers where the middleware-level pause gate
    (server.py auth_and_rate_limit_middleware) doesn't apply — e.g. routes
    added by future routers that aren't covered by the prefix-based gate.

    The middleware is the canonical enforcer; this dep is a defense-in-depth
    layer for explicit per-route gating. Returns the JWT user payload on
    success, raises 403 with detail "ACCOUNT_PAUSED" on failure so the
    frontend can detect it and show the banner without a generic 403 toast.
    """
    user = await get_current_user(request)

    # Dev mode — no profiles table to query, allow through
    if user.get("sub") == "dev-user":
        return user

    # Lazy-import to avoid a circular import at module load time
    # (services.profiles indirectly imports services.supabase which is heavy).
    from backend.services.profiles import get_user_status, is_paused

    user_id = user.get("sub") or ""
    status = get_user_status(user_id)
    if is_paused(status):
        raise HTTPException(
            status_code=403,
            detail="ACCOUNT_PAUSED",
        )
    return user


async def get_verified_tenant(request: Request, tenant_id: str) -> dict:
    """Verify the authenticated user owns the given tenant_id.

    Returns the user payload if authorized.
    Raises 403 if the user doesn't own the tenant.
    """
    user = await get_current_user(request)

    # Dev mode — skip ownership check
    if user.get("sub") == "dev-user":
        return user

    # Normalize the user's email same as the middleware does — Supabase
    # stores emails lowercased but JWTs / user_metadata can leak through
    # in mixed case. Strict == was producing spurious 403s for legitimate
    # owners with case-mismatched DB rows.
    user_email = (user.get("email") or user.get("user_metadata", {}).get("email") or "").lower().strip()
    user_id = user.get("sub", "")

    if not user_email and not user_id:
        raise HTTPException(status_code=401, detail="Invalid token: no user identity")

    # Check tenant ownership.
    # Security audit #16: collapse 404 (tenant doesn't exist) and 403
    # (tenant exists but not yours) to a single 403 with the same body.
    # Returning 404 leaked tenant existence — an attacker enumerating
    # UUIDs could distinguish "this UUID is a real tenant" from
    # "this UUID doesn't map to anything." Both are now reported as
    # 403 "Access denied" so the response is identical regardless of
    # whether the tenant exists. The internal log still distinguishes
    # the two cases for the operator.
    try:
        from backend.config.loader import get_tenant_config
        config = get_tenant_config(tenant_id)
        owner_email = (config.owner_email or "").lower().strip()

        # Verify ownership by email (case/whitespace-insensitive)
        if owner_email and owner_email == user_email:
            return user

        # Also check if the tenant_id itself matches the user_id (some setups)
        if str(config.tenant_id) == user_id:
            return user

        # If tenant has no owner_email set, allow access (legacy/migration)
        if not owner_email:
            logger.warning("Tenant %s has no owner_email — allowing access", tenant_id)
            return user

    except HTTPException:
        # Don't swallow our own 401 / 403 / 404 from upstream — let them surface
        raise
    except Exception as e:
        # Tenant lookup failed — could be "doesn't exist" or a transient
        # DB error. Log the real reason; respond with the same 403 the
        # ownership-mismatch path uses so we don't leak existence.
        logger.warning("Tenant lookup failed for %s: %s", tenant_id, e)
        raise HTTPException(status_code=403, detail="Access denied")

    # Tenant exists but caller doesn't own it.
    raise HTTPException(status_code=403, detail="Access denied")


# ── Rate limiting helpers ────────────────────────────────────────────────────


def check_rate_limit(request: Request, max_requests: int = 60, window_seconds: int = 60):
    """Sliding window rate limiter by IP, backed by Redis with in-memory
    fallback when Redis is unreachable.

    Replaces the previous purely in-memory implementation, which was wiped
    on every container restart (giving anyone a free quota right after a
    deploy) and didn't coordinate across replicas. The Redis path uses an
    atomic Lua script so concurrent requests can't both pass the cap on
    a read-then-write race.

    Caller args preserved: server.py middleware passes (max_requests=120,
    window_seconds=60) for the global IP limit.
    """
    from backend.services import rate_limit as _rate_limit
    client_ip = request.client.host if request.client else "unknown"
    allowed, _count = _rate_limit.hit("ip", client_ip, max_requests, window_seconds)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")


def check_user_rate_limit(user_id: str, action: str, max_requests: int, window_seconds: int):
    """Per-user rate limit for expensive operations (chat, agent runs,
    image generation). The IP-level limit alone doesn't stop a logged-in
    user from hammering one endpoint up to the IP cap; this adds a
    per-user backstop that survives proxy rotation.

    `action` is the bucket name — pass distinct values for distinct
    policies (e.g. "ceo_chat" vs "agent_run") so a chat-heavy user
    isn't penalized on agent invocations and vice versa.
    """
    from backend.services import rate_limit as _rate_limit
    if not user_id:
        return  # anonymous; covered by IP limit only
    allowed, _count = _rate_limit.hit(f"user:{action}", user_id, max_requests, window_seconds)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Too many {action} requests. Please wait before retrying.",
        )
