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

# Simple in-memory rate limiter (per IP) with eviction
_rate_limits: dict[str, list[float]] = {}
_last_eviction: float = 0


def check_rate_limit(request: Request, max_requests: int = 60, window_seconds: int = 60):
    """In-memory sliding window rate limiter by IP with periodic eviction."""
    import time
    global _last_eviction

    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    cutoff = now - window_seconds

    # Evict stale IPs every 5 minutes to prevent unbounded growth
    if now - _last_eviction > 300:
        stale_ips = [ip for ip, timestamps in _rate_limits.items() if not timestamps or timestamps[-1] < cutoff]
        for ip in stale_ips:
            del _rate_limits[ip]
        _last_eviction = now

    if client_ip not in _rate_limits:
        _rate_limits[client_ip] = []

    # Clean old entries for this IP
    _rate_limits[client_ip] = [t for t in _rate_limits[client_ip] if t > cutoff]

    if len(_rate_limits[client_ip]) >= max_requests:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")

    _rate_limits[client_ip].append(now)
