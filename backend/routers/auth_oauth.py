"""OAuth init + callback routes for Twitter/X, LinkedIn, and Google (Gmail).

These endpoints sit under `/api/auth/` which the middleware bypasses for
legitimate OAuth callback URLs. Each `/connect/{tenant_id}` route therefore
performs a manual JWT-query-param verification (the browser top-level
navigation can't send Authorization headers).

Name choice: `auth_oauth.py` rather than `auth.py` to avoid colliding with
`backend/auth.py` (the JWT helpers).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from backend.config.loader import get_tenant_config, save_tenant_config

logger = logging.getLogger("aria.server")

router = APIRouter()


# ── Shared helpers ──────────────────────────────────────────────────────
import html as _html


def _safe_oauth_error(message: str) -> str:
    """Return a safe HTML page that shows an error and closes the popup. Escapes user input to prevent XSS."""
    safe_msg = _html.escape(str(message))
    return f"""<html><body><p style="font-family:sans-serif;padding:20px;">
    <strong>Authentication failed</strong><br><br>{safe_msg}<br><br>
    You can close this window.</p>
    <script>if(window.opener)window.opener.postMessage('auth_error','*');setTimeout(function(){{window.close()}},3000);</script>
    </body></html>"""


def _get_backend_base_url(request: Request) -> str:
    """Get the public-facing backend base URL, preferring BACKEND_URL env var."""
    explicit = os.getenv("BACKEND_URL", "").rstrip("/")
    if explicit:
        return explicit
    # Fallback: derive from request, but force https if behind proxy
    base = str(request.base_url).rstrip("/")
    if "railway.app" in base or "vercel" in base or "render.com" in base:
        base = base.replace("http://", "https://")
    return base


# ─── Twitter / X OAuth 2.0 ───

# FRONTEND NOTE: callers must pass ?access_token=<supabase_jwt> as a query
# param because top-level browser navigation (window.location.href) can't
# send the Authorization header. The dashboard's "Connect Twitter" button
# needs to be updated to include the token in the URL.
@router.get("/api/auth/twitter/connect/{tenant_id}")
async def twitter_connect(tenant_id: str, request: Request):
    """Start Twitter OAuth 2.0 PKCE flow — redirects user to X login."""
    # Manual auth check — this endpoint sits under /api/auth/ which the
    # middleware bypasses for legitimate OAuth callback URLs, so without
    # this guard ANY caller who knows a victim's tenant_id could complete
    # OAuth with their own Twitter account and have the callback bind the
    # attacker's tokens to the victim's tenant (full account hijack).
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

    from backend.tools import twitter_tool
    base_url = _get_backend_base_url(request)
    redirect_uri = f"{base_url}/api/auth/twitter/callback"
    auth_url = twitter_tool.get_auth_url(tenant_id, redirect_uri)
    from starlette.responses import RedirectResponse
    return RedirectResponse(auth_url)


@router.get("/api/auth/twitter/callback")
async def twitter_callback(code: str = "", state: str = "", error: str = "", request: Request = None):
    """Handle Twitter OAuth callback — exchange code for tokens and store."""
    from starlette.responses import HTMLResponse
    if error:
        return HTMLResponse(_safe_oauth_error(f"Twitter auth failed: {error}"))

    from backend.tools import twitter_tool
    from backend.config.loader import get_tenant_config, save_tenant_config

    base_url = _get_backend_base_url(request)
    redirect_uri = f"{base_url}/api/auth/twitter/callback"

    try:
        tokens = await twitter_tool.exchange_code(code, state, redirect_uri)
    except Exception as e:
        return HTMLResponse(_safe_oauth_error(f"Auth failed: {e}"))

    tenant_id = tokens["tenant_id"]
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    # Get username
    profile = await twitter_tool.get_me(access_token)
    username = profile.get("username", "")

    # Store tokens in tenant config
    config = get_tenant_config(tenant_id)
    config.integrations.twitter_access_token = access_token
    config.integrations.twitter_refresh_token = refresh_token
    config.integrations.twitter_username = username
    save_tenant_config(config)

    logger.info("Twitter connected for tenant %s (@%s)", tenant_id, username)

    return HTMLResponse(
        "<html><body>"
        "<h3 style='color:green'>Twitter connected successfully!</h3>"
        "<p>You can close this window.</p>"
        "<script>"
        "try { window.opener && window.opener.postMessage('twitter_connected', '*'); } catch(e) {}"
        "setTimeout(()=>window.close(),2000);"
        "</script></body></html>"
    )


# ─── LinkedIn OAuth 2.0 ───

# In-memory state store for LinkedIn OAuth (state → tenant_id)
# state → (tenant_id, timestamp) — entries expire after 10 minutes
_linkedin_pending_auth: dict[str, tuple[str, float]] = {}


# FRONTEND NOTE: callers must pass ?access_token=<supabase_jwt> as a query
# param because top-level browser navigation (window.location.href) can't
# send the Authorization header. The dashboard's "Connect LinkedIn" button
# needs to be updated to include the token in the URL.
@router.get("/api/auth/linkedin/connect/{tenant_id}")
async def linkedin_connect(tenant_id: str, request: Request):
    """Start LinkedIn OAuth 2.0 flow — redirects user to LinkedIn login."""
    # Manual auth check — this endpoint sits under /api/auth/ which the
    # middleware bypasses for legitimate OAuth callback URLs, so without
    # this guard ANY caller who knows a victim's tenant_id could complete
    # OAuth with their own LinkedIn account and have the callback bind the
    # attacker's tokens to the victim's tenant (full account hijack).
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

    import secrets
    from starlette.responses import RedirectResponse
    from backend.tools import linkedin_tool

    base_url = _get_backend_base_url(request)
    redirect_uri = f"{base_url}/api/auth/linkedin/callback"
    state = secrets.token_urlsafe(32)
    import time as _time
    # Evict expired entries (>10 min old)
    _now = _time.time()
    expired = [k for k, (_, ts) in _linkedin_pending_auth.items() if _now - ts > 600]
    for k in expired:
        del _linkedin_pending_auth[k]
    _linkedin_pending_auth[state] = (tenant_id, _now)
    auth_url = linkedin_tool.get_auth_url(redirect_uri, state)
    return RedirectResponse(auth_url)


@router.get("/api/auth/linkedin/callback")
async def linkedin_callback(code: str = "", state: str = "", error: str = "", request: Request = None):
    """Handle LinkedIn OAuth callback — exchange code for tokens and store."""
    from starlette.responses import HTMLResponse
    if error:
        return HTMLResponse(_safe_oauth_error(f"LinkedIn auth failed: {error}"))

    entry = _linkedin_pending_auth.pop(state, None)
    tenant_id = entry[0] if entry else None
    if not tenant_id:
        return HTMLResponse(_safe_oauth_error("Invalid or expired OAuth state"))

    from backend.tools import linkedin_tool

    base_url = _get_backend_base_url(request)
    redirect_uri = f"{base_url}/api/auth/linkedin/callback"

    try:
        tokens = await linkedin_tool.exchange_code(code, redirect_uri)
    except Exception as e:
        return HTMLResponse(_safe_oauth_error(f"Auth failed: {e}"))

    access_token = tokens["access_token"]

    # Get profile to store name and LinkedIn URN
    profile = await linkedin_tool.get_profile(access_token)
    if profile.get("error"):
        err = profile.get("error", "unknown")
        return HTMLResponse(_safe_oauth_error(f"Failed to get profile: {err}"))

    linkedin_name = profile.get("name", "")
    linkedin_sub = profile.get("sub", "")  # This is the member ID

    # Store tokens in tenant config
    config = get_tenant_config(tenant_id)
    config.integrations.linkedin_access_token = access_token
    config.integrations.linkedin_member_urn = f"urn:li:person:{linkedin_sub}"
    config.integrations.linkedin_name = linkedin_name
    save_tenant_config(config)

    logger.info("LinkedIn connected for tenant %s (%s)", tenant_id, linkedin_name)

    return HTMLResponse(
        "<html><body>"
        "<h3 style='color:green'>LinkedIn connected successfully!</h3>"
        "<p>You can close this window.</p>"
        "<script>"
        "try { window.opener && window.opener.postMessage('linkedin_connected', '*'); } catch(e) {}"
        "setTimeout(()=>window.close(),2000);"
        "</script></body></html>"
    )


# ─── Google OAuth Connect (dedicated flow, independent of Supabase) ───

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Send-only scope — keeps OAuth in Google's "Sensitive" tier (no CASA
# Tier-2 audit required). Inbound replies are handled via reply-to
# routing through an ARIA-owned mailbox + Postmark/SendGrid webhook
# (see docs/email-inbound-routing.md). Adding `gmail.readonly` back
# would push the app into "Restricted" tier and require a $5K-15K
# CASA security assessment plus annual re-audit.
GOOGLE_GMAIL_SCOPES = "https://www.googleapis.com/auth/gmail.send"


# FRONTEND NOTE: callers must pass ?access_token=<supabase_jwt> as a query
# param because top-level browser navigation (window.location.href) can't
# send the Authorization header. The dashboard's "Connect Gmail" button
# needs to be updated to include the token in the URL.
@router.get("/api/auth/google/connect/{tenant_id}")
async def google_connect(tenant_id: str, request: Request):
    """Redirect user to Google OAuth consent screen for Gmail access."""
    # Manual auth check — this endpoint sits under /api/auth/ which the
    # middleware bypasses for legitimate OAuth callback URLs, so without
    # this guard ANY caller who knows a victim's tenant_id could complete
    # OAuth with their own Google account and have the callback bind the
    # attacker's tokens to the victim's tenant (full Gmail hijack).
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

    from starlette.responses import RedirectResponse, HTMLResponse
    from urllib.parse import urlencode

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    if not client_id:
        return HTMLResponse("<h3>GOOGLE_CLIENT_ID not configured</h3>", status_code=500)

    # Build redirect URI
    base_url = os.environ.get("API_URL", "").rstrip("/")
    if not base_url:
        proto = request.headers.get("x-forwarded-proto", "https")
        host = request.headers.get("host", "localhost:8000")
        base_url = f"{proto}://{host}"
    redirect_uri = f"{base_url}/api/auth/google/callback"

    # login_hint policy: prefer the email of the *previously connected*
    # Google account (stored in integrations.google_email after a
    # successful OAuth). Reason: the user's ARIA signup email may be a
    # Google Workspace for Education / Business account whose admin has
    # third-party Gmail OAuth disabled. Pinning the OAuth flow to that
    # email reproduces "Access blocked: Authorization Error / Error 400:
    # invalid_request" before the picker even renders. By contrast, the
    # email that *was* successfully connected before is by definition an
    # account that permits Gmail OAuth — so reusing it for Reconnect is
    # always safe and correct. Falls back to no hint on first connect
    # (so `prompt=select_account` shows Google's native picker and the
    # user can pick any signed-in account).
    hint_email: Optional[str] = None
    try:
        existing_config = get_tenant_config(tenant_id)
        if existing_config and existing_config.integrations.google_email:
            hint_email = existing_config.integrations.google_email
    except Exception:
        # Tenant not found or config load failed — fall through to no
        # hint, which is the correct behavior for first-time connects.
        pass

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_GMAIL_SCOPES,
        "access_type": "offline",
        # `consent` re-prompts for permissions (so we always get a fresh
        # refresh_token); `select_account` forces the account picker so
        # Google can't auto-pick a stale browser-cached account.
        "prompt": "consent select_account",
        "state": tenant_id,
    }
    if hint_email:
        params["login_hint"] = hint_email
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(auth_url)


@router.get("/api/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Handle Google OAuth callback — exchange code for tokens and save."""
    from starlette.responses import HTMLResponse

    if error or not code:
        return HTMLResponse(
            f"<h3>Gmail connection failed</h3><p>{_safe_oauth_error(error or 'No code')}</p>"
            "<script>setTimeout(()=>window.close(),3000)</script>",
            status_code=400,
        )

    tenant_id = state
    if not tenant_id:
        return HTMLResponse(
            "<h3>Missing tenant ID</h3><script>setTimeout(()=>window.close(),3000)</script>",
            status_code=400,
        )

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    base_url = os.environ.get("API_URL", "").rstrip("/")
    if not base_url:
        proto = request.headers.get("x-forwarded-proto", "https")
        host = request.headers.get("host", "localhost:8000")
        base_url = f"{proto}://{host}"
    redirect_uri = f"{base_url}/api/auth/google/callback"

    # Exchange code for tokens
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        })

    if resp.status_code != 200:
        err_data = resp.json() if resp.status_code < 500 else {}
        err_msg = err_data.get("error_description", err_data.get("error", "Token exchange failed"))
        return HTMLResponse(
            f"<h3>Gmail connection failed</h3><p>{_safe_oauth_error(err_msg)}</p>"
            "<script>setTimeout(()=>window.close(),3000)</script>",
            status_code=400,
        )

    tokens = resp.json()
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")

    if not access_token:
        return HTMLResponse(
            "<h3>No access token received</h3>"
            "<script>setTimeout(()=>window.close(),3000)</script>",
            status_code=400,
        )

    # Look up which Google account actually got connected so we can
    # store it. Two reasons to bother: (1) the ARIA signup email and the
    # connected Gmail email can legitimately differ — common case is a
    # user signing up to ARIA with a school/work Workspace account but
    # connecting a personal Gmail for sending, and (2) on Reconnect we
    # want to login_hint at *this* email (the one we already know works
    # with third-party Gmail OAuth), not the ARIA signup email which
    # may be a blocked Workspace account.
    connected_email = ""
    try:
        async with httpx.AsyncClient() as client:
            ui_resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=5.0,
            )
        if ui_resp.status_code == 200:
            connected_email = (ui_resp.json() or {}).get("email", "") or ""
    except Exception:
        # Userinfo lookup is best-effort; failing it shouldn't break
        # the OAuth flow itself. Reconnect just won't have a hint.
        pass

    # Save tokens to tenant config
    try:
        config = get_tenant_config(tenant_id)
        config.integrations.google_access_token = access_token
        if refresh_token:
            config.integrations.google_refresh_token = refresh_token
        if connected_email:
            config.integrations.google_email = connected_email
        save_tenant_config(config)
    except Exception as e:
        return HTMLResponse(
            f"<h3>Failed to save tokens</h3><p>{_safe_oauth_error(str(e))}</p>"
            "<script>setTimeout(()=>window.close(),3000)</script>",
            status_code=500,
        )

    return HTMLResponse(
        "<h3 style='color:green'>Gmail connected successfully!</h3>"
        "<p>You can close this window.</p>"
        "<script>"
        "try { window.opener && window.opener.postMessage('gmail_connected', '*'); } catch(e) {}"
        "setTimeout(()=>window.close(),2000);"
        "</script>"
    )
