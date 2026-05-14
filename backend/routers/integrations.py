"""Integration status + disconnect endpoints and the Google OAuth token storage.

Covers Twitter/LinkedIn/WhatsApp/Gmail status reads, the matching disconnect
POSTs, and the JWT-query-param-gated /google-tokens write. OAuth init/callback
URLs live in backend/routers/auth_oauth.py.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.auth import get_verified_tenant
from backend.config.loader import get_tenant_config, save_tenant_config

logger = logging.getLogger("aria.server")

router = APIRouter()


# ─── Status checks ───
@router.get("/api/integrations/{tenant_id}/twitter-status")
async def twitter_status(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Check if Twitter is connected for a tenant."""
    config = get_tenant_config(tenant_id)
    connected = bool(config.integrations.twitter_access_token or config.integrations.twitter_refresh_token)
    return {
        "connected": connected,
        "username": config.integrations.twitter_username or "",
    }


@router.get("/api/integrations/{tenant_id}/linkedin-status")
async def linkedin_status(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Check if LinkedIn is connected for a tenant."""
    config = get_tenant_config(tenant_id)
    connected = bool(config.integrations.linkedin_access_token)
    return {
        "connected": connected,
        "name": config.integrations.linkedin_name or "",
        "org_name": config.integrations.linkedin_org_name or "",
        "posting_to": "company" if config.integrations.linkedin_org_urn else "personal",
    }


@router.get("/api/integrations/{tenant_id}/whatsapp-status")
async def whatsapp_status(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Check if WhatsApp is connected for a tenant."""
    config = get_tenant_config(tenant_id)
    connected = bool(config.integrations.whatsapp_access_token and config.integrations.whatsapp_phone_number_id)
    return {"connected": connected}


@router.get("/api/integrations/{tenant_id}/gmail-status")
async def gmail_status(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Check if Gmail is connected for a tenant.

    Connected = has a valid access_token OR a refresh_token that can mint one.
    """
    try:
        config = get_tenant_config(tenant_id)
        has_access = bool(config.integrations.google_access_token)
        has_refresh = bool(config.integrations.google_refresh_token)

        # If we have a refresh token but no access token, try to refresh now
        if not has_access and has_refresh:
            try:
                from backend.tools import gmail_tool
                new_token = await gmail_tool.refresh_access_token(config.integrations.google_refresh_token)
                config.integrations.google_access_token = new_token
                save_tenant_config(config)
                has_access = True
            except Exception:
                pass  # Refresh failed — still report based on what we have

        connected = has_access or has_refresh
        # Prefer the actual Google account email captured at OAuth time;
        # fall back to owner_email for tenants connected before we
        # started recording it.
        display_email = (
            config.integrations.google_email
            or config.owner_email
        ) if connected else None
        return {"connected": connected, "email": display_email}
    except Exception:
        return {"connected": False, "email": None}


# ─── Disconnects ───
@router.post("/api/integrations/{tenant_id}/gmail-disconnect")
async def gmail_disconnect(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Remove Gmail/Google credentials for a tenant."""
    config = get_tenant_config(tenant_id)
    config.integrations.google_access_token = None
    config.integrations.google_refresh_token = None
    save_tenant_config(config)
    return {"status": "disconnected"}


@router.post("/api/integrations/{tenant_id}/twitter-disconnect")
async def twitter_disconnect(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Remove Twitter/X credentials for a tenant."""
    config = get_tenant_config(tenant_id)
    config.integrations.twitter_access_token = None
    config.integrations.twitter_refresh_token = None
    save_tenant_config(config)
    return {"status": "disconnected"}


@router.post("/api/integrations/{tenant_id}/linkedin-disconnect")
async def linkedin_disconnect(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Remove LinkedIn credentials for a tenant."""
    config = get_tenant_config(tenant_id)
    config.integrations.linkedin_access_token = None
    config.integrations.linkedin_member_urn = None
    config.integrations.linkedin_org_urn = None
    config.integrations.linkedin_org_name = None
    save_tenant_config(config)
    return {"status": "disconnected"}


# ─── Google OAuth Token Storage ───
class GoogleTokens(BaseModel):
    google_access_token: str
    google_refresh_token: str | None = None


# FRONTEND NOTE: callers must pass ?access_token=<supabase_jwt> as a query
# param because the middleware special-cases `path.endswith("/google-tokens")`
# to bypass auth. Without this manual check ANY caller who knows a victim's
# tenant_id could overwrite their Google OAuth tokens with attacker-controlled
# values (full Gmail hijack). The dashboard caller needs to be updated to
# include the token in the URL.
@router.post("/api/integrations/{tenant_id}/google-tokens")
async def save_google_tokens(tenant_id: str, body: GoogleTokens, request: Request):
    """Store Google OAuth tokens for Gmail sending."""
    # Manual auth check — middleware bypasses this path via the
    # `path.endswith("/google-tokens")` special case, so we have to verify
    # session + ownership here. Using a query-param JWT for parity with
    # the OAuth init endpoints; an XHR caller could send Authorization
    # instead but query-param keeps the call sites uniform.
    from backend.auth import verify_jwt
    access_token_jwt = request.query_params.get("access_token", "")
    if not access_token_jwt:
        raise HTTPException(status_code=401, detail="Missing access_token query param")
    try:
        user = verify_jwt(access_token_jwt)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_email = (user.get("email") or user.get("user_metadata", {}).get("email") or "").lower().strip()
    user_sub = user.get("sub", "")
    try:
        _cfg = get_tenant_config(tenant_id)
        _owner = (_cfg.owner_email or "").lower().strip()
        if _owner and _owner != user_email and str(_cfg.tenant_id) != user_sub:
            raise HTTPException(status_code=403, detail="Access denied")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        config = get_tenant_config(tenant_id)
        config.integrations.google_access_token = body.google_access_token
        if body.google_refresh_token:
            config.integrations.google_refresh_token = body.google_refresh_token
        save_tenant_config(config)
        return {"ok": True}
    except Exception as e:
        from backend.services.safe_errors import safe_detail
        raise HTTPException(status_code=400, detail=safe_detail(e, "Google token save failed"))
