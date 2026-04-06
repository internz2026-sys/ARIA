"""ARIA FastAPI Server — webhooks, chat, agent management, dashboard API."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import socketio
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

from backend.auth import get_current_user, get_verified_tenant, check_rate_limit

load_dotenv()

logger = logging.getLogger("aria.server")

import html as _html


def _safe_oauth_error(message: str) -> str:
    """Return a safe HTML page that shows an error and closes the popup. Escapes user input to prevent XSS."""
    safe_msg = _html.escape(str(message))
    return f"""<html><body><p style="font-family:sans-serif;padding:20px;">
    <strong>Authentication failed</strong><br><br>{safe_msg}<br><br>
    You can close this window.</p>
    <script>if(window.opener)window.opener.postMessage('auth_error','*');setTimeout(function(){{window.close()}},3000);</script>
    </body></html>"""


from backend.approval import requires_approval, validate_execution, ACTION_POLICIES
from backend.config.loader import get_tenant_config, save_tenant_config
from backend.services.supabase import get_db as _get_supabase
from backend.onboarding_agent import OnboardingAgent
from backend.orchestrator import (
    dispatch_agent,
    get_agent_status,
    handle_webhook,
    pause_agent_paperclip,
    resume_agent_paperclip,
    run_scheduled_agents,
)
from backend.paperclip_sync import initialize as paperclip_init, is_connected as paperclip_connected

# ── CORS — restrict to known frontend origins ────────────────────────────
_allowed_origins = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
] or [
    "http://localhost:3000",
    "https://aria-alpha-weld.vercel.app",
]

# Socket.IO for real-time events
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")


def _require_confirmation(action: str, confirmed: bool, message: str) -> dict | None:
    """Centralized confirmation gate. Returns a needs_confirmation response if not confirmed,
    or None if confirmed (caller should proceed). Uses the approval policy registry."""
    if confirmed or not requires_approval(action):
        return None
    policy = ACTION_POLICIES.get(action, {})
    return {
        "status": "needs_confirmation",
        "action": action,
        "message": message,
        "confirm_label": "Confirm",
        "destructive": policy.get("risk", "high") in ("high", "critical"),
    }


async def _gmail_sync_loop():
    """Background loop: sync Gmail inbound replies every 2 minutes."""
    from backend.tools.gmail_sync import sync_all_tenants
    _log = logging.getLogger("aria.gmail_sync_loop")
    while True:
        await asyncio.sleep(60)  # 1 minute
        try:
            results = await sync_all_tenants()
            for sr in results:
                tid = sr.get("tenant_id", "")
                if tid:
                    await _emit_sync_events(tid, sr)
            total = sum(r.get("imported", 0) for r in results)
            if total > 0:
                _log.info("Background sync: imported %d replies from %d tenants", total, len(results))
        except Exception as e:
            _log.warning("Background Gmail sync failed: %s", e)


async def _scheduler_executor_loop():
    """Background loop: execute due scheduled tasks every 30 seconds."""
    from backend.services.scheduler import get_due_tasks, execute_task
    _log = logging.getLogger("aria.scheduler_executor")
    while True:
        await asyncio.sleep(30)
        try:
            due = get_due_tasks()
            for task in due:
                try:
                    result = await execute_task(task)
                    tid = task.get("tenant_id", "")
                    if tid:
                        await sio.emit("scheduled_task_executed", {
                            "id": task["id"],
                            "task_type": task["task_type"],
                            "title": task.get("title", ""),
                            "status": "sent" if not result.get("error") else "failed",
                            "result": result,
                        }, room=tid)
                except Exception as e:
                    _log.warning("Failed to execute task %s: %s", task.get("id"), e)
            if due:
                _log.info("Scheduler: processed %d due tasks", len(due))
        except Exception as e:
            _log.warning("Scheduler executor loop failed: %s", e)


async def _paperclip_poller_loop():
    """Background loop: poll Paperclip for completed issues + sync agent statuses."""
    from backend.paperclip_poller import poll_completed_issues, sync_agent_statuses
    _log = logging.getLogger("aria.paperclip_poller")
    while True:
        await asyncio.sleep(5)  # 5s for responsive updates
        try:
            if not paperclip_connected():
                continue
            await poll_completed_issues()
            await sync_agent_statuses(sio)
        except Exception as e:
            _log.warning("Paperclip poller failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: sync agents with Paperclip AI orchestrator + start background loops."""
    await paperclip_init()
    sync_task = asyncio.create_task(_gmail_sync_loop())
    scheduler_task = asyncio.create_task(_scheduler_executor_loop())
    poller_task = asyncio.create_task(_paperclip_poller_loop())
    yield
    sync_task.cancel()
    scheduler_task.cancel()
    poller_task.cancel()


app = FastAPI(title="ARIA API", version="1.0.0", lifespan=lifespan)

# ── Register routers ──────────────────────────────────────────────────────
from backend.routers.crm import router as crm_router
from backend.routers.inbox import router as inbox_router
from backend.routers.campaigns import router as campaigns_router

app.include_router(crm_router)
app.include_router(inbox_router)
app.include_router(campaigns_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Public paths that don't require authentication ────────────────────────
_PUBLIC_PATHS = {
    "/health",
    "/api/onboarding/start",
    "/api/onboarding/message",
    "/api/onboarding/extract-config",
    "/api/onboarding/save-config",
    "/api/onboarding/save-config-direct",
    "/api/whatsapp/webhook",
    "/api/cron/run-scheduled",
}

_PUBLIC_PREFIXES = (
    "/api/auth/",           # OAuth callbacks (Twitter, LinkedIn)
    "/api/webhooks/",       # External webhooks (Stripe, SendGrid)
    "/api/inbox/",          # Inbox item creation (used by Paperclip agents)
    "/api/tenant/by-email/", # Tenant lookup during login (returns only tenant_id)
    "/docs",                # Swagger UI
    "/openapi.json",
)


# ── Auth + rate limiting middleware ───────────────────────────────────────
@app.middleware("http")
async def auth_and_rate_limit_middleware(request: Request, call_next):
    """Authenticate requests and apply rate limiting."""
    path = request.url.path

    # Rate limit all API calls
    if path.startswith("/api/"):
        check_rate_limit(request, max_requests=120, window_seconds=60)

    # Skip auth for public paths, OPTIONS (CORS preflight), and non-API routes
    if (
        request.method == "OPTIONS"
        or path in _PUBLIC_PATHS
        or path.startswith(_PUBLIC_PREFIXES)
        or path.endswith("/google-tokens")  # OAuth token storage during login flow
        or not path.startswith("/api/")
    ):
        return await call_next(request)

    # Verify JWT for all other API routes
    from backend.auth import _get_jwt_secret, _extract_token, verify_jwt

    secret = _get_jwt_secret()
    if not secret:
        # Auth not configured (dev mode) — allow through
        return await call_next(request)

    token = _extract_token(request)
    origin = request.headers.get("origin", "")
    cors_headers = {}
    if origin in _allowed_origins:
        cors_headers = {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
        }

    if not token:
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=401, content={"detail": "Missing authorization token"}, headers=cors_headers)

    try:
        user = verify_jwt(token)
    except HTTPException:
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=401, content={"detail": "Invalid or expired token"}, headers=cors_headers)

    # Tenant ownership check: if path has a tenant_id segment, verify ownership
    # Skip for paths that don't use tenant_id (CEO chat uses session_id instead)
    _SKIP_TENANT_CHECK_PREFIXES = ("/api/ceo/chat/", "/api/onboarding/", "/api/notifications/")
    skip_tenant = any(path.startswith(p) for p in _SKIP_TENANT_CHECK_PREFIXES)

    path_parts = path.strip("/").split("/")
    tenant_id = None
    if not skip_tenant:
        for i, part in enumerate(path_parts):
            # tenant_id is typically the segment after a known prefix
            if i >= 2 and len(part) > 8 and part not in ("run", "pause", "resume", "connect", "disconnect", "send", "sync", "history", "sessions", "counts"):
                # Looks like a tenant_id (UUID or long string)
                tenant_id = part
                break

    if tenant_id:
        user_email = (user.get("email") or user.get("user_metadata", {}).get("email") or "").lower().strip()
        try:
            from backend.config.loader import get_tenant_config
            config = get_tenant_config(tenant_id)
            owner_email = (config.owner_email or "").lower().strip()
            # Allow if: no owner set, emails match (case-insensitive), or user sub matches
            if owner_email and user_email and owner_email != user_email:
                if str(config.tenant_id) != user.get("sub", ""):
                    logger.warning("Tenant ownership denied: jwt_email=%s owner_email=%s tenant=%s", user_email, owner_email, tenant_id)
                    from starlette.responses import JSONResponse
                    return JSONResponse(status_code=403, content={"detail": "Access denied to this tenant"}, headers=cors_headers)
        except Exception:
            pass  # Tenant not found — let the endpoint handle it

    # Attach user info to request state for endpoints that need it
    request.state.user = user
    return await call_next(request)


# Mount Socket.IO with CORS-aware wrapper
_sio_asgi = socketio.ASGIApp(sio, other_asgi_app=app)


async def socket_app(scope, receive, send):
    """ASGI wrapper that ensures CORS headers on all responses."""
    if scope["type"] == "http":
        headers = dict(scope.get("headers", []))
        origin = headers.get(b"origin", b"").decode()
        if origin in _allowed_origins:
            # Intercept OPTIONS preflight
            if scope["method"] == "OPTIONS":
                await send({"type": "http.response.start", "status": 204, "headers": [
                    [b"access-control-allow-origin", origin.encode()],
                    [b"access-control-allow-methods", b"GET, POST, PUT, PATCH, DELETE, OPTIONS"],
                    [b"access-control-allow-headers", b"authorization, content-type"],
                    [b"access-control-allow-credentials", b"true"],
                    [b"access-control-max-age", b"86400"],
                ]})
                await send({"type": "http.response.body", "body": b""})
                return
    await _sio_asgi(scope, receive, send)

# In-memory live status store + persisted to Supabase
_live_agent_status: dict[str, dict[str, dict]] = {}


async def _emit_agent_status(tenant_id: str, agent_id: str, status: str,
                              current_task: str = "", **extra):
    """Update status in memory, persist to DB, and emit Socket.IO event."""
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = {
        "agent_id": agent_id,
        "status": status,
        "current_task": current_task,
        "last_updated": now_ts,
        **extra,
    }
    _live_agent_status.setdefault(tenant_id, {})[agent_id] = payload
    await sio.emit("agent_status_change", payload, room=tenant_id)

    # Persist to Supabase so status survives page navigation
    try:
        sb = _get_supabase()
        sb.table("agent_status").upsert({
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "status": status,
            "current_task": current_task,
            "action": extra.get("action", ""),
            "updated_at": now_ts,
        }, on_conflict="tenant_id,agent_id").execute()
    except Exception:
        pass  # Don't block the flow if DB write fails


# ─── Socket.IO Events ───
@sio.event
async def connect(sid, environ):
    pass


@sio.event
async def join_tenant(sid, data):
    tenant_id = data.get("tenant_id", "")
    if tenant_id:
        await sio.enter_room(sid, tenant_id)


# Active onboarding sessions
onboarding_sessions: dict[str, OnboardingAgent] = {}

# ─── Virtual Office Agent Definitions (matches AGENT_REGISTRY) ───
VIRTUAL_OFFICE_AGENTS = [
    {"agent_id": "ceo", "name": "ARIA CEO", "role": "Chief Marketing Strategist", "model": "opus-4-6", "department": "ceo-office"},
    {"agent_id": "content_writer", "name": "Content Writer", "role": "Content Creation Agent", "model": "sonnet-4-6", "department": "content-studio"},
    {"agent_id": "email_marketer", "name": "Email Marketer", "role": "Email Campaign Agent", "model": "sonnet-4-6", "department": "email-room"},
    {"agent_id": "social_manager", "name": "Social Manager", "role": "Social Media Agent", "model": "sonnet-4-6", "department": "social-hub"},
    {"agent_id": "ad_strategist", "name": "Ad Strategist", "role": "Paid Ads Advisor", "model": "sonnet-4-6", "department": "ads-room"},
]


# ─── Health Check ───
@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


# ─── Twitter / X OAuth 2.0 ───

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


@app.get("/api/auth/twitter/connect/{tenant_id}")
async def twitter_connect(tenant_id: str, request: Request):
    """Start Twitter OAuth 2.0 PKCE flow — redirects user to X login."""
    from backend.tools import twitter_tool
    base_url = _get_backend_base_url(request)
    redirect_uri = f"{base_url}/api/auth/twitter/callback"
    auth_url = twitter_tool.get_auth_url(tenant_id, redirect_uri)
    from starlette.responses import RedirectResponse
    return RedirectResponse(auth_url)


@app.get("/api/auth/twitter/callback")
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


@app.get("/api/integrations/{tenant_id}/twitter-status")
async def twitter_status(tenant_id: str):
    """Check if Twitter is connected for a tenant."""
    config = get_tenant_config(tenant_id)
    connected = bool(config.integrations.twitter_access_token or config.integrations.twitter_refresh_token)
    return {
        "connected": connected,
        "username": config.integrations.twitter_username or "",
    }


# ─── LinkedIn OAuth 2.0 ───

# In-memory state store for LinkedIn OAuth (state → tenant_id)
# state → (tenant_id, timestamp) — entries expire after 10 minutes
_linkedin_pending_auth: dict[str, tuple[str, float]] = {}


@app.get("/api/auth/linkedin/connect/{tenant_id}")
async def linkedin_connect(tenant_id: str, request: Request):
    """Start LinkedIn OAuth 2.0 flow — redirects user to LinkedIn login."""
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


@app.get("/api/auth/linkedin/callback")
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


@app.get("/api/integrations/{tenant_id}/linkedin-status")
async def linkedin_status(tenant_id: str):
    """Check if LinkedIn is connected for a tenant."""
    config = get_tenant_config(tenant_id)
    connected = bool(config.integrations.linkedin_access_token)
    return {
        "connected": connected,
        "name": config.integrations.linkedin_name or "",
        "org_name": config.integrations.linkedin_org_name or "",
        "posting_to": "company" if config.integrations.linkedin_org_urn else "personal",
    }


@app.get("/api/linkedin/{tenant_id}/organizations")
async def linkedin_organizations(tenant_id: str):
    """List company pages the user is admin of."""
    from backend.tools import linkedin_tool

    config = get_tenant_config(tenant_id)
    access_token = config.integrations.linkedin_access_token
    if not access_token:
        raise HTTPException(status_code=400, detail="LinkedIn not connected.")

    orgs = await linkedin_tool.get_admin_organizations(access_token)
    return {
        "organizations": orgs,
        "current_org_urn": config.integrations.linkedin_org_urn or "",
    }


class LinkedInPostTargetRequest(BaseModel):
    org_urn: str = ""   # Empty string = post to personal profile
    org_name: str = ""


@app.post("/api/linkedin/{tenant_id}/set-target")
async def linkedin_set_target(tenant_id: str, body: LinkedInPostTargetRequest):
    """Set whether LinkedIn posts go to personal profile or a company page."""
    config = get_tenant_config(tenant_id)
    config.integrations.linkedin_org_urn = body.org_urn or None
    config.integrations.linkedin_org_name = body.org_name or None
    save_tenant_config(config)

    target = "company" if body.org_urn else "personal"
    logger.info("LinkedIn post target set to %s for tenant %s", target, tenant_id)
    return {"status": "updated", "posting_to": target, "org_name": body.org_name}


@app.post("/api/linkedin/{tenant_id}/post")
async def publish_linkedin_post(tenant_id: str, body: dict):
    """Publish a post to LinkedIn from the tenant's connected account.

    Requires confirmed=true — human must explicitly approve before publishing.
    """
    gate = _require_confirmation("publish_linkedin", body.get("confirmed", False),
                                "Are you sure you want to publish this post to LinkedIn? This action is public and cannot be undone.")
    if gate:
        return gate

    from backend.tools import linkedin_tool

    config = get_tenant_config(tenant_id)
    access_token = config.integrations.linkedin_access_token

    if not access_token:
        raise HTTPException(status_code=400, detail="LinkedIn not connected. Go to Settings > Integrations.")

    # Use company page URN if set, otherwise personal profile
    author_urn = config.integrations.linkedin_org_urn or config.integrations.linkedin_member_urn
    if not author_urn:
        raise HTTPException(status_code=400, detail="LinkedIn not connected. Go to Settings > Integrations.")

    text = body.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="Post text is required")

    result = await linkedin_tool.create_post(access_token, author_urn, text)

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@app.get("/api/integrations/{tenant_id}/whatsapp-status")
async def whatsapp_status(tenant_id: str):
    """Check if WhatsApp is connected for a tenant."""
    config = get_tenant_config(tenant_id)
    connected = bool(config.integrations.whatsapp_access_token and config.integrations.whatsapp_phone_number_id)
    return {"connected": connected}


class TweetRequest(BaseModel):
    text: str
    reply_to: Optional[str] = None


class ThreadRequest(BaseModel):
    tweets: list[str]


@app.post("/api/twitter/{tenant_id}/tweet")
async def publish_tweet(tenant_id: str, body: TweetRequest, confirmed: bool = False):
    """Post a single tweet from the tenant's connected X account.

    Requires confirmed=true — human must explicitly approve before posting.
    """
    gate = _require_confirmation("publish_twitter", confirmed,
                                f"Publish this tweet to X? This will be visible publicly.\n\n\"{body.text[:100]}{'...' if len(body.text) > 100 else ''}\"")
    if gate:
        return gate

    from backend.tools import twitter_tool
    config = get_tenant_config(tenant_id)
    access_token = config.integrations.twitter_access_token
    refresh_token = config.integrations.twitter_refresh_token

    if not access_token and not refresh_token:
        raise HTTPException(status_code=400, detail="Twitter not connected. Go to Settings > Integrations.")

    # Refresh if needed
    if not access_token and refresh_token:
        try:
            tokens = await twitter_tool.refresh_access_token(refresh_token)
            access_token = tokens["access_token"]
            config.integrations.twitter_access_token = access_token
            config.integrations.twitter_refresh_token = tokens.get("refresh_token", refresh_token)
            save_tenant_config(config)
        except Exception:
            raise HTTPException(status_code=400, detail="Twitter token expired. Reconnect in Settings.")

    result = await twitter_tool.post_tweet(access_token, body.text, reply_to=body.reply_to)

    if result.get("error") == "token_expired" and refresh_token:
        # Try refresh once
        try:
            tokens = await twitter_tool.refresh_access_token(refresh_token)
            access_token = tokens["access_token"]
            config.integrations.twitter_access_token = access_token
            config.integrations.twitter_refresh_token = tokens.get("refresh_token", refresh_token)
            save_tenant_config(config)
            result = await twitter_tool.post_tweet(access_token, body.text, reply_to=body.reply_to)
        except Exception:
            raise HTTPException(status_code=400, detail="Twitter token expired. Reconnect in Settings.")

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@app.post("/api/twitter/{tenant_id}/thread")
async def publish_thread(tenant_id: str, body: ThreadRequest, confirmed: bool = False):
    """Post a thread (multiple tweets) from the tenant's connected X account.

    Requires confirmed=true — human must explicitly approve before posting.
    """
    gate = _require_confirmation("publish_twitter", confirmed,
                                f"Publish a {len(body.tweets)}-tweet thread to X? This will be visible publicly.")
    if gate:
        return gate

    from backend.tools import twitter_tool
    config = get_tenant_config(tenant_id)
    access_token = config.integrations.twitter_access_token

    if not access_token:
        raise HTTPException(status_code=400, detail="Twitter not connected.")

    results = await twitter_tool.post_thread(access_token, body.tweets)
    return {"tweets": results}


# ─── Social Post Approval & Publish ───

class SocialApproveRequest(BaseModel):
    inbox_item_id: str


@app.post("/api/social/{tenant_id}/approve-publish")
async def approve_and_publish_social(tenant_id: str, body: SocialApproveRequest):
    """Approve a social post from inbox and publish to connected platforms (Twitter/X)."""
    from backend.tools import twitter_tool

    sb = _get_supabase()

    # Fetch inbox item
    item_result = sb.table("inbox_items").select("*").eq("id", body.inbox_item_id).single().execute()
    item = item_result.data
    if not item:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    if item.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    if item.get("type") != "social_post":
        raise HTTPException(status_code=400, detail="Item is not a social post")

    content = item.get("content", "")

    # Try to parse structured posts from content
    posts = []
    try:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            import json as _json
            data = _json.loads(content[start:end])
            posts = data.get("posts", [])
    except Exception:
        pass
    if not posts:
        try:
            start = content.find("[")
            end = content.rfind("]") + 1
            if start >= 0 and end > start:
                import json as _json
                posts = _json.loads(content[start:end])
        except Exception:
            pass

    # Fallback: treat entire content as a single tweet
    if not posts:
        # Strip JSON wrapper artifacts, use plain text
        clean = content.strip()
        # Remove markdown fences
        if clean.startswith("```"):
            clean = "\n".join(clean.split("\n")[1:])
        if clean.endswith("```"):
            clean = "\n".join(clean.split("\n")[:-1])
        posts = [{"platform": "twitter", "text": clean[:280]}]

    # Get Twitter credentials
    config = get_tenant_config(tenant_id)
    access_token = config.integrations.twitter_access_token
    refresh_token = config.integrations.twitter_refresh_token

    if not access_token and not refresh_token:
        raise HTTPException(status_code=400, detail="Twitter not connected. Go to Settings > Integrations.")

    # Refresh if needed
    if not access_token and refresh_token:
        try:
            tokens = await twitter_tool.refresh_access_token(refresh_token)
            access_token = tokens["access_token"]
            config.integrations.twitter_access_token = access_token
            config.integrations.twitter_refresh_token = tokens.get("refresh_token", refresh_token)
            save_tenant_config(config)
        except Exception:
            raise HTTPException(status_code=400, detail="Twitter token expired. Reconnect in Settings.")

    # Mark as sending
    sb.table("inbox_items").update({
        "status": "sending",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).execute()

    results = []
    for post in posts:
        platform = post.get("platform", "twitter").lower()
        text = post.get("text", "")
        hashtags = post.get("hashtags", [])
        if not text:
            continue

        if hashtags:
            tag_str = " ".join(f"#{t.strip('#')}" for t in hashtags)
            if tag_str not in text:
                text = f"{text}\n\n{tag_str}"

        if platform == "twitter":
            tweet_text = text[:280]
            result = await twitter_tool.post_tweet(access_token, tweet_text)
            # Retry with refresh if expired
            if result.get("error") == "token_expired" and refresh_token:
                try:
                    tokens = await twitter_tool.refresh_access_token(refresh_token)
                    access_token = tokens["access_token"]
                    config.integrations.twitter_access_token = access_token
                    config.integrations.twitter_refresh_token = tokens.get("refresh_token", refresh_token)
                    save_tenant_config(config)
                    result = await twitter_tool.post_tweet(access_token, tweet_text)
                except Exception:
                    result = {"error": "token_refresh_failed"}
            results.append({"platform": "twitter", **result})
        elif platform == "linkedin":
            from backend.tools import linkedin_tool
            li_token = config.integrations.linkedin_access_token
            li_urn = config.integrations.linkedin_member_urn
            if not li_token or not li_urn:
                results.append({"platform": "linkedin", "status": "skipped", "reason": "not_connected"})
            else:
                result = await linkedin_tool.create_post(li_token, li_urn, text[:3000])
                results.append({"platform": "linkedin", **result})
        else:
            results.append({"platform": platform, "status": "skipped", "reason": "not_integrated_yet"})

    # Update inbox item status
    any_success = any(r.get("tweet_id") or r.get("post_id") for r in results)
    new_status = "sent" if any_success else "failed"
    sb.table("inbox_items").update({
        "status": new_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).execute()

    # If all failed, return error detail so the user sees why
    if not any_success and results:
        errors = [r.get("error", "unknown") for r in results if r.get("error")]
        error_msg = "; ".join(errors) if errors else "No posts were published"
        logger.error("Social publish failed for tenant %s: %s", tenant_id, error_msg)
        raise HTTPException(status_code=400, detail=f"Publish failed: {error_msg}")

    return {"status": new_status, "results": results}


# ─── WhatsApp Cloud API ───

@app.get("/api/whatsapp/webhook")
async def whatsapp_webhook_verify(request: Request):
    """Meta webhook verification (GET) — responds to the hub.challenge."""
    from backend.tools.whatsapp_tool import WHATSAPP_VERIFY_TOKEN
    params = request.query_params
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        logger.info("WhatsApp webhook verified")
        return int(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/api/whatsapp/webhook")
async def whatsapp_webhook_receive(request: Request):
    """Receive incoming WhatsApp messages and status updates."""
    body = await request.json()

    # Process each entry
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            statuses = value.get("statuses", [])

            # Handle incoming messages
            for msg in messages:
                from_number = msg.get("from", "")
                msg_type = msg.get("type", "")
                msg_id = msg.get("id", "")
                timestamp = msg.get("timestamp", "")

                text_body = ""
                if msg_type == "text":
                    text_body = msg.get("text", {}).get("body", "")

                logger.info("WhatsApp message from %s: %s", from_number, text_body[:100])

                # Store in inbox for tenant review
                try:
                    sb = _get_supabase()

                    # Find tenant by WhatsApp phone number ID
                    phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
                    tenant_id = await _resolve_whatsapp_tenant(phone_number_id)

                    if tenant_id:
                        sb.table("inbox_items").insert({
                            "id": str(uuid.uuid4()),
                            "tenant_id": tenant_id,
                            "type": "whatsapp_message",
                            "agent": "whatsapp",
                            "title": f"WhatsApp from {from_number}",
                            "content": text_body,
                            "status": "needs_review",
                            "metadata": {
                                "from_number": from_number,
                                "message_id": msg_id,
                                "message_type": msg_type,
                                "timestamp": timestamp,
                                "phone_number_id": phone_number_id,
                            },
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }).execute()
                        logger.info("Stored WhatsApp message in inbox for tenant %s", tenant_id)
                except Exception as e:
                    logger.warning("Failed to store WhatsApp message: %s", e)

            # Handle status updates (sent, delivered, read)
            for status in statuses:
                logger.info("WhatsApp status update: %s → %s",
                            status.get("id", ""), status.get("status", ""))

    return {"status": "ok"}


async def _resolve_whatsapp_tenant(phone_number_id: str) -> str | None:
    """Find the tenant that owns a given WhatsApp phone number ID."""
    if not phone_number_id:
        return None
    try:
        sb = _get_supabase()
        # Search tenant_configs for matching WhatsApp phone number ID
        result = sb.table("tenant_configs").select("tenant_id,config").execute()
        for row in (result.data or []):
            config = row.get("config", {})
            integrations = config.get("integrations", {})
            if integrations.get("whatsapp_phone_number_id") == phone_number_id:
                return row.get("tenant_id")
    except Exception as e:
        logger.warning("Failed to resolve WhatsApp tenant: %s", e)

    # Fallback: if only one tenant exists, use that
    try:
        result = sb.table("tenant_configs").select("tenant_id").limit(1).execute()
        if result.data:
            return result.data[0].get("tenant_id")
    except Exception:
        pass
    return None


class WhatsAppSendRequest(BaseModel):
    to: str
    message: str


@app.post("/api/whatsapp/{tenant_id}/send")
async def whatsapp_send_message(tenant_id: str, body: WhatsAppSendRequest, confirmed: bool = False):
    """Send a WhatsApp message from a tenant's connected number.

    Requires confirmed=true — human must explicitly approve before sending.
    """
    gate = _require_confirmation("send_whatsapp", confirmed,
                                f"Send WhatsApp message to {body.to}? This cannot be undone.")
    if gate:
        return gate

    from backend.tools import whatsapp_tool

    config = get_tenant_config(tenant_id)
    token = config.integrations.whatsapp_access_token
    pid = config.integrations.whatsapp_phone_number_id

    if not token or not pid:
        raise HTTPException(status_code=400, detail="WhatsApp not connected. Add your credentials in Settings.")

    result = await whatsapp_tool.send_message(
        to=body.to,
        text=body.message,
        access_token=token,
        phone_number_id=pid,
    )

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return result


class WhatsAppConnectRequest(BaseModel):
    access_token: str
    phone_number_id: str
    business_account_id: str = ""


@app.post("/api/whatsapp/{tenant_id}/connect")
async def whatsapp_connect(tenant_id: str, body: WhatsAppConnectRequest):
    """Save WhatsApp credentials for a tenant and verify connectivity."""
    from backend.tools import whatsapp_tool

    # Test the connection by fetching business profile
    profile = await whatsapp_tool.get_business_profile(
        access_token=body.access_token,
        phone_number_id=body.phone_number_id,
    )
    if profile.get("error"):
        raise HTTPException(status_code=400, detail=f"Connection test failed: {profile['error']}")

    # Save credentials to tenant config
    config = get_tenant_config(tenant_id)
    config.integrations.whatsapp_access_token = body.access_token
    config.integrations.whatsapp_phone_number_id = body.phone_number_id
    config.integrations.whatsapp_business_account_id = body.business_account_id
    save_tenant_config(config)

    return {"status": "connected", "profile": profile}


@app.post("/api/whatsapp/{tenant_id}/disconnect")
async def whatsapp_disconnect(tenant_id: str):
    """Remove WhatsApp credentials for a tenant."""
    config = get_tenant_config(tenant_id)
    config.integrations.whatsapp_access_token = None
    config.integrations.whatsapp_phone_number_id = None
    config.integrations.whatsapp_business_account_id = None
    save_tenant_config(config)
    return {"status": "disconnected"}


@app.post("/api/integrations/{tenant_id}/gmail-disconnect")
async def gmail_disconnect(tenant_id: str):
    """Remove Gmail/Google credentials for a tenant."""
    config = get_tenant_config(tenant_id)
    config.integrations.google_access_token = None
    config.integrations.google_refresh_token = None
    save_tenant_config(config)
    return {"status": "disconnected"}


@app.post("/api/integrations/{tenant_id}/twitter-disconnect")
async def twitter_disconnect(tenant_id: str):
    """Remove Twitter/X credentials for a tenant."""
    config = get_tenant_config(tenant_id)
    config.integrations.twitter_access_token = None
    config.integrations.twitter_refresh_token = None
    save_tenant_config(config)
    return {"status": "disconnected"}


@app.post("/api/integrations/{tenant_id}/linkedin-disconnect")
async def linkedin_disconnect(tenant_id: str):
    """Remove LinkedIn credentials for a tenant."""
    config = get_tenant_config(tenant_id)
    config.integrations.linkedin_access_token = None
    config.integrations.linkedin_member_urn = None
    config.integrations.linkedin_org_urn = None
    config.integrations.linkedin_org_name = None
    save_tenant_config(config)
    return {"status": "disconnected"}


# ─── Scheduler API ───

from backend.services import scheduler as scheduler_service


class ScheduleTaskRequest(BaseModel):
    task_type: str
    title: str
    scheduled_at: str
    payload: dict = {}
    related_entity_type: str | None = None
    related_entity_id: str | None = None
    timezone: str = "UTC"
    approval_required: bool = False
    created_by: str = "user"


@app.post("/api/schedule/{tenant_id}/tasks")
async def create_scheduled_task(tenant_id: str, body: ScheduleTaskRequest):
    """Create a new scheduled task."""
    result = scheduler_service.create_task(
        tenant_id=tenant_id,
        task_type=body.task_type,
        title=body.title,
        scheduled_at=body.scheduled_at,
        payload=body.payload,
        related_entity_type=body.related_entity_type,
        related_entity_id=body.related_entity_id,
        timezone_str=body.timezone,
        approval_status="pending" if body.approval_required else "none",
        created_by=body.created_by,
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/api/schedule/{tenant_id}/tasks")
async def list_scheduled_tasks(
    tenant_id: str,
    status: str = "",
    task_type: str = "",
    from_date: str = "",
    to_date: str = "",
    page: int = 1,
    page_size: int = 50,
):
    """List scheduled tasks with optional filters."""
    return scheduler_service.list_tasks(tenant_id, status, task_type, from_date, to_date, page, page_size)


@app.get("/api/schedule/{tenant_id}/tasks/{task_id}")
async def get_scheduled_task(tenant_id: str, task_id: str):
    """Get a single scheduled task."""
    task = scheduler_service.get_task(tenant_id, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


class UpdateScheduleRequest(BaseModel):
    scheduled_at: str | None = None
    timezone: str | None = None
    title: str | None = None
    status: str | None = None
    payload: dict | None = None


@app.patch("/api/schedule/{tenant_id}/tasks/{task_id}")
async def update_scheduled_task(tenant_id: str, task_id: str, body: UpdateScheduleRequest):
    """Update a scheduled task (reschedule, change title, etc.)."""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    return scheduler_service.update_task(tenant_id, task_id, updates)


@app.post("/api/schedule/{tenant_id}/tasks/{task_id}/cancel")
async def cancel_scheduled_task(tenant_id: str, task_id: str):
    """Cancel a scheduled task."""
    return scheduler_service.cancel_task(tenant_id, task_id)


@app.post("/api/schedule/{tenant_id}/tasks/{task_id}/approve")
async def approve_scheduled_task(tenant_id: str, task_id: str):
    """Approve a pending scheduled task — moves to 'scheduled' for execution."""
    return scheduler_service.approve_task(tenant_id, task_id)


@app.post("/api/schedule/{tenant_id}/tasks/{task_id}/reject")
async def reject_scheduled_task(tenant_id: str, task_id: str):
    """Reject a pending scheduled task."""
    return scheduler_service.reject_task(tenant_id, task_id)


class RescheduleRequest(BaseModel):
    scheduled_at: str
    timezone: str = ""


@app.post("/api/schedule/{tenant_id}/tasks/{task_id}/reschedule")
async def reschedule_task(tenant_id: str, task_id: str, body: RescheduleRequest):
    """Reschedule a task to a new time."""
    return scheduler_service.reschedule_task(tenant_id, task_id, body.scheduled_at, body.timezone)


@app.post("/api/schedule/{tenant_id}/tasks/{task_id}/execute-now")
async def execute_task_now(tenant_id: str, task_id: str):
    """Execute a scheduled task immediately (bypass schedule)."""
    task = scheduler_service.get_task(tenant_id, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("approval_status") == "pending":
        raise HTTPException(status_code=400, detail="Task requires approval before execution")
    result = await scheduler_service.execute_task(task)
    return {"executed": True, "result": result}


@app.get("/api/schedule/{tenant_id}/calendar")
async def get_calendar(tenant_id: str, start: str = "", end: str = ""):
    """Get scheduled tasks for the calendar view."""
    if not start or not end:
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        start = start or (now - timedelta(days=7)).isoformat()
        end = end or (now + timedelta(days=60)).isoformat()
    return {"tasks": scheduler_service.calendar_tasks(tenant_id, start, end)}


# ─── Usage API ───

@app.get("/api/usage/{tenant_id}")
async def get_usage_dashboard(tenant_id: str):
    """Return usage stats for the dashboard: tenant totals + per-agent breakdown."""
    from backend.tools.claude_cli import (
        get_usage, get_agent_usage_summary,
        HOURLY_REQUEST_LIMIT, HOURLY_TOKEN_LIMIT, AGENT_HOURLY_LIMITS, DEFAULT_AGENT_LIMIT,
    )
    tenant_usage = get_usage(tenant_id)
    agent_usage = get_agent_usage_summary(tenant_id)

    # Ensure all agents appear even if they haven't been used this hour
    for agent_id in ["ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist"]:
        if agent_id not in agent_usage:
            limits = AGENT_HOURLY_LIMITS.get(agent_id, DEFAULT_AGENT_LIMIT)
            agent_usage[agent_id] = {
                "requests": 0, "request_limit": limits["requests"],
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "token_limit": limits["tokens"],
            }

    return {
        "tenant": {
            "requests": tenant_usage.get("requests", 0),
            "request_limit": HOURLY_REQUEST_LIMIT,
            "input_tokens": tenant_usage.get("input_tokens", 0),
            "output_tokens": tenant_usage.get("output_tokens", 0),
            "total_tokens": tenant_usage.get("input_tokens", 0) + tenant_usage.get("output_tokens", 0),
            "token_limit": HOURLY_TOKEN_LIMIT,
        },
        "agents": agent_usage,
        "resets_at": tenant_usage.get("hour", ""),
    }


# ─── Onboarding API ───
class OnboardingMessage(BaseModel):
    session_id: str
    message: str


class OnboardingStart(BaseModel):
    session_id: Optional[str] = None


@app.get("/api/tenant/by-email/{email}")
async def tenant_by_email(email: str):
    """Look up a tenant config by owner email. Returns only the tenant_id (no sensitive data)."""
    try:
        sb = _get_supabase()
        result = sb.table("tenant_configs").select("tenant_id").eq("owner_email", email).limit(1).execute()
        if result.data and len(result.data) > 0:
            return {"tenant_id": result.data[0]["tenant_id"]}
        return {"tenant_id": None}
    except Exception:
        return {"tenant_id": None}


@app.post("/api/onboarding/start")
async def start_onboarding(body: OnboardingStart):
    session_id = body.session_id or str(uuid.uuid4())
    agent = OnboardingAgent()
    greeting = agent.start_conversation()
    onboarding_sessions[session_id] = agent
    return {"session_id": session_id, "message": greeting}


@app.post("/api/onboarding/message")
async def onboarding_message(body: OnboardingMessage):
    agent = onboarding_sessions.get(body.session_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Session not found")
    response = await agent.process_message(body.message)
    return {
        "message": response,
        "is_complete": agent.is_complete(),
        "questions_answered": agent.questions_answered,
        "validated_fields": sorted(agent.validated_fields),
    }


@app.post("/api/onboarding/skip")
async def onboarding_skip(body: OnboardingStart):
    agent = onboarding_sessions.get(body.session_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Session not found")
    skipped = agent.skip_current_topic()
    current = agent.get_current_topic()
    return {
        "skipped_topic": skipped,
        "current_topic": current,
        "questions_answered": agent.questions_answered,
        "is_complete": agent.is_complete(),
        "skipped_topics": agent.skipped_topics,
    }


@app.post("/api/onboarding/extract-config")
async def extract_config(body: OnboardingStart):
    agent = onboarding_sessions.get(body.session_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        config_data = await agent.extract_config()
    except Exception as e:
        logger.error("extract_config failed: %s", e)
        # Return the fallback config so the frontend still works
        config_data = agent._fallback_config_from_messages()
    return {"config": config_data}


class SaveConfig(BaseModel):
    session_id: str
    owner_email: str
    owner_name: str
    active_agents: list[str] | None = None
    existing_tenant_id: str | None = None  # If set, overwrite this tenant


@app.post("/api/onboarding/save-config")
async def save_config(body: SaveConfig):
    from backend.config.brief import generate_agent_brief

    agent = onboarding_sessions.get(body.session_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Session not found")
    tenant_id = body.existing_tenant_id or str(uuid.uuid4())
    config = await agent.build_tenant_config(tenant_id, body.owner_email, body.owner_name, body.active_agents)

    # Generate condensed brief — all agents use this instead of full context
    try:
        config.agent_brief = await generate_agent_brief(config)
    except Exception as e:
        logger.warning("Brief generation failed (will use full context): %s", e)

    save_tenant_config(config)
    del onboarding_sessions[body.session_id]
    return {"tenant_id": tenant_id, "config": config.model_dump(mode="json")}


class SaveConfigDirect(BaseModel):
    """Accept the raw extracted config JSON (cached on the frontend) to save
    directly — no backend session needed."""
    config: dict
    owner_email: str
    owner_name: str
    active_agents: list[str] | None = None
    skipped_topics: list[str] | None = None
    existing_tenant_id: str | None = None  # If set, overwrite this tenant


@app.post("/api/onboarding/save-config-direct")
async def save_config_direct(body: SaveConfigDirect):
    from backend.config.tenant_schema import (
        TenantConfig, ICPConfig, ProductConfig, GTMPlaybook, BrandVoice, GTMProfile,
    )
    from backend.config.brief import generate_agent_brief

    extracted = body.config
    has_skips = bool(body.skipped_topics)
    tenant_id = body.existing_tenant_id or str(uuid.uuid4())

    # Build GTMProfile from the flat gtm_profile extraction.
    gp_raw = extracted.get("gtm_profile", {})
    # Ensure generated fields are always populated
    from backend.onboarding_agent import _ensure_generated_fields
    gp_raw = _ensure_generated_fields(gp_raw)
    gtm_profile = GTMProfile(
        business_name=gp_raw.get("business_name", extracted.get("business_name", "")),
        offer=gp_raw.get("offer", extracted.get("description", "")),
        audience=gp_raw.get("audience", ""),
        problem=gp_raw.get("problem", ""),
        differentiator=gp_raw.get("differentiator", ""),
        positioning_summary=gp_raw.get("positioning_summary", ""),
        primary_channels=gp_raw.get("primary_channels", extracted.get("channels", [])),
        brand_voice=gp_raw.get("brand_voice", extracted.get("brand_voice", {}).get("tone", "")),
        goal_30_days=gp_raw.get("goal_30_days", ""),
        thirty_day_gtm_focus=gp_raw.get("30_day_gtm_focus", ""),
    )

    config = TenantConfig(
        tenant_id=tenant_id,
        business_name=extracted.get("business_name", ""),
        industry=extracted.get("industry", "technology"),
        description=extracted.get("description", ""),
        icp=ICPConfig(**extracted.get("icp", {})),
        product=ProductConfig(**extracted.get("product", {})),
        gtm_playbook=GTMPlaybook(**extracted.get("gtm_playbook", {})),
        brand_voice=BrandVoice(**extracted.get("brand_voice", {})),
        active_agents=body.active_agents or extracted.get("recommended_agents", ["ceo", "content_writer"]),
        channels=extracted.get("channels", []),
        gtm_profile=gtm_profile,
        owner_email=body.owner_email,
        owner_name=body.owner_name,
        plan="starter",
        onboarding_status="completed" if not has_skips else "in_progress",
        skipped_fields=body.skipped_topics or [],
    )

    # Generate condensed brief — all agents use this instead of full context
    try:
        config.agent_brief = await generate_agent_brief(config)
    except Exception as e:
        logger.warning("Brief generation failed (will use full context): %s", e)

    save_tenant_config(config)
    return {"tenant_id": tenant_id, "config": config.model_dump(mode="json")}


# ─── Re-onboarding / Edit Mode ───

@app.get("/api/tenant/{tenant_id}/onboarding-data")
async def get_onboarding_data(tenant_id: str):
    """Return existing onboarding answers mapped to the 8 onboarding fields."""
    try:
        config = get_tenant_config(tenant_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {
        "business_name": config.business_name,
        "offer": config.product.description or config.description or "",
        "target_audience": ", ".join(config.icp.target_titles) if config.icp.target_titles else "",
        "problem_solved": ", ".join(config.icp.pain_points) if config.icp.pain_points else "",
        "differentiator": ", ".join(config.product.differentiators) if config.product.differentiators else "",
        "channels": config.channels or [],
        "brand_voice": config.brand_voice.tone or "",
        "thirty_day_goal": config.gtm_playbook.action_plan_30 or "",
        "product_name": config.product.name or "",
        "industry": config.industry or "technology",
        "active_agents": config.active_agents or [],
        "onboarding_status": config.onboarding_status,
    }


class UpdateOnboarding(BaseModel):
    """Partial update of onboarding fields."""
    business_name: str | None = None
    offer: str | None = None
    target_audience: str | None = None
    problem_solved: str | None = None
    differentiator: str | None = None
    channels: list[str] | None = None
    brand_voice: str | None = None
    thirty_day_goal: str | None = None


@app.post("/api/tenant/{tenant_id}/update-onboarding")
async def update_onboarding(tenant_id: str, body: UpdateOnboarding):
    """Update specific onboarding fields on an existing tenant, then regenerate brief."""
    from backend.config.brief import generate_agent_brief

    try:
        config = get_tenant_config(tenant_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Apply updates only for provided fields
    if body.business_name is not None:
        config.business_name = body.business_name
    if body.offer is not None:
        config.product.description = body.offer
        config.description = body.offer
    if body.target_audience is not None:
        config.icp.target_titles = [t.strip() for t in body.target_audience.split(",") if t.strip()]
    if body.problem_solved is not None:
        config.icp.pain_points = [p.strip() for p in body.problem_solved.split(",") if p.strip()]
    if body.differentiator is not None:
        config.product.differentiators = [d.strip() for d in body.differentiator.split(",") if d.strip()]
    if body.channels is not None:
        config.channels = body.channels
    if body.brand_voice is not None:
        config.brand_voice.tone = body.brand_voice
    if body.thirty_day_goal is not None:
        config.gtm_playbook.action_plan_30 = body.thirty_day_goal

    config.onboarding_status = "completed"
    config.skipped_fields = []

    # Regenerate brief with updated data
    try:
        config.agent_brief = await generate_agent_brief(config)
    except Exception as e:
        logger.warning("Brief regeneration failed: %s", e)

    save_tenant_config(config)
    return {"ok": True, "tenant_id": str(config.tenant_id)}


# ─── Agent Brief (re)generation ───

@app.post("/api/tenants/{tenant_id}/regenerate-brief")
async def regenerate_brief(tenant_id: str):
    """Regenerate the condensed agent brief for an existing tenant.

    Call this after the user updates their business info in settings,
    or to backfill briefs for tenants who onboarded before this feature.
    """
    from backend.config.brief import generate_agent_brief

    config = get_tenant_config(tenant_id)
    config.agent_brief = await generate_agent_brief(config)
    save_tenant_config(config)
    return {"agent_brief": config.agent_brief}


# ─── Google OAuth Token Storage ───
class GoogleTokens(BaseModel):
    google_access_token: str
    google_refresh_token: str | None = None


@app.post("/api/integrations/{tenant_id}/google-tokens")
async def save_google_tokens(tenant_id: str, body: GoogleTokens):
    """Store Google OAuth tokens for Gmail sending."""
    try:
        config = get_tenant_config(tenant_id)
        config.integrations.google_access_token = body.google_access_token
        if body.google_refresh_token:
            config.integrations.google_refresh_token = body.google_refresh_token
        save_tenant_config(config)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── Google OAuth Connect (dedicated flow, independent of Supabase) ───

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_GMAIL_SCOPES = "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly"


@app.get("/api/auth/google/connect/{tenant_id}")
async def google_connect(tenant_id: str, request: Request):
    """Redirect user to Google OAuth consent screen for Gmail access."""
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

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_GMAIL_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": tenant_id,
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(auth_url)


@app.get("/api/auth/google/callback")
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

    # Save tokens to tenant config
    try:
        config = get_tenant_config(tenant_id)
        config.integrations.google_access_token = access_token
        if refresh_token:
            config.integrations.google_refresh_token = refresh_token
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


@app.get("/api/integrations/{tenant_id}/gmail-status")
async def gmail_status(tenant_id: str):
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
        return {"connected": connected, "email": config.owner_email if connected else None}
    except Exception:
        return {"connected": False, "email": None}


# ─── Gmail Send API ───
class GmailSendRequest(BaseModel):
    to: str
    subject: str
    html_body: str


@app.post("/api/email/{tenant_id}/send")
async def send_gmail_email(tenant_id: str, body: GmailSendRequest, confirmed: bool = False):
    """Send an email via the user's authenticated Gmail account.

    Requires confirmed=true — human must explicitly approve before sending.
    """
    gate = _require_confirmation("send_email", confirmed,
                                f"Send email to {body.to}?\n\nSubject: {body.subject}")
    if gate:
        return gate

    from backend.tools import gmail_tool

    config = get_tenant_config(tenant_id)
    access_token = config.integrations.google_access_token
    refresh_token = config.integrations.google_refresh_token

    # Proactively refresh if we have a refresh token but no access token
    if not access_token and refresh_token:
        try:
            access_token = await gmail_tool.refresh_access_token(refresh_token)
            config.integrations.google_access_token = access_token
            save_tenant_config(config)
        except Exception:
            raise HTTPException(status_code=400, detail="Gmail not connected. Please log in with Google to grant email access.")

    if not access_token:
        raise HTTPException(status_code=400, detail="Gmail not connected. Please log in with Google to grant email access.")

    result = await gmail_tool.send_email(
        access_token=access_token,
        to=body.to,
        subject=body.subject,
        html_body=body.html_body,
        from_email=config.owner_email,
    )

    # Token expired — refresh and retry
    if result.get("error") == "token_expired" and refresh_token:
        try:
            new_token = await gmail_tool.refresh_access_token(refresh_token)
            config.integrations.google_access_token = new_token
            save_tenant_config(config)
            result = await gmail_tool.send_email(
                access_token=new_token,
                to=body.to,
                subject=body.subject,
                html_body=body.html_body,
                from_email=config.owner_email,
            )
        except Exception as e:
            config.integrations.google_access_token = None
            if getattr(e, "is_revoked", False):
                config.integrations.google_refresh_token = None
            save_tenant_config(config)
            raise HTTPException(status_code=401, detail="Gmail token expired. Please log in again to reconnect.")

    if result.get("error"):
        detail = result.get("detail", "Gmail API error")
        raise HTTPException(status_code=result.get("status_code", 401), detail=detail)

    return {"status": "sent", "message_id": result.get("message_id", "")}


# ─── Email Draft Approval ───
class EmailApproveRequest(BaseModel):
    inbox_item_id: str


@app.post("/api/email/{tenant_id}/approve-send")
async def approve_and_send_email(tenant_id: str, body: EmailApproveRequest):
    """Approve a pending email draft and send it via Gmail.

    Only sends drafts in 'draft_pending_approval' status.
    Updates the inbox item status through the lifecycle:
    draft_pending_approval → sending → sent / failed.
    """
    from backend.tools import gmail_tool

    sb = _get_supabase()

    # Fetch the inbox item
    item_result = sb.table("inbox_items").select("*").eq("id", body.inbox_item_id).single().execute()
    item = item_result.data
    if not item:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    if item.get("status") != "draft_pending_approval":
        raise HTTPException(status_code=400, detail=f"Item is not a pending draft (status: {item.get('status')})")
    if item.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")

    # Extract email draft metadata from the item
    meta = item.get("email_draft") or {}
    to = meta.get("to", "")
    subject = meta.get("subject", "")
    html_body = meta.get("html_body", "")

    if not to or not subject or not html_body:
        raise HTTPException(status_code=400, detail="Email draft is missing required fields (to, subject, or body)")

    # Mark as sending
    sb.table("inbox_items").update({
        "status": "sending",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).execute()

    # Send via Gmail
    config = get_tenant_config(tenant_id)
    access_token = config.integrations.google_access_token
    refresh_token = config.integrations.google_refresh_token

    # Proactively refresh if we have a refresh token but no access token
    if not access_token and refresh_token:
        try:
            from backend.tools import gmail_tool as _gt
            access_token = await _gt.refresh_access_token(refresh_token)
            config.integrations.google_access_token = access_token
            save_tenant_config(config)
        except Exception:
            pass  # Fall through to the not-connected error

    if not access_token:
        sb.table("inbox_items").update({
            "status": "failed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", body.inbox_item_id).execute()
        raise HTTPException(status_code=400, detail="Gmail not connected. Please log in with Google to grant email access.")

    result = await gmail_tool.send_email(
        access_token=access_token,
        to=to,
        subject=subject,
        html_body=html_body,
        from_email=config.owner_email,
    )

    # Token expired — try refresh
    if result.get("error") == "token_expired" and refresh_token:
        try:
            new_token = await gmail_tool.refresh_access_token(refresh_token)
            config.integrations.google_access_token = new_token
            save_tenant_config(config)
            result = await gmail_tool.send_email(
                access_token=new_token,
                to=to,
                subject=subject,
                html_body=html_body,
                from_email=config.owner_email,
            )
        except Exception as e:
            config.integrations.google_access_token = None
            if getattr(e, "is_revoked", False):
                config.integrations.google_refresh_token = None
            save_tenant_config(config)
            sb.table("inbox_items").update({
                "status": "failed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", body.inbox_item_id).execute()
            raise HTTPException(status_code=401, detail="Gmail token expired. Please reconnect Gmail in Settings.")

    if result.get("error"):
        sb.table("inbox_items").update({
            "status": "failed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", body.inbox_item_id).execute()
        raise HTTPException(status_code=500, detail=f"Email send failed: {result['error']}")

    # Mark as sent
    sb.table("inbox_items").update({
        "status": "sent",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).execute()

    # ── Thread tracking: persist outbound message for future reply matching ──
    gmail_message_id = result.get("message_id", "")
    gmail_thread_id = result.get("thread_id", "")
    thread_db_id = None
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        # Find or create thread
        if gmail_thread_id:
            existing = sb.table("email_threads").select("id").eq(
                "tenant_id", tenant_id
            ).eq("gmail_thread_id", gmail_thread_id).limit(1).execute()
            if existing.data:
                thread_db_id = existing.data[0]["id"]
                sb.table("email_threads").update({
                    "last_message_at": now_iso,
                    "status": "awaiting_reply",
                    "updated_at": now_iso,
                }).eq("id", thread_db_id).execute()

        if not thread_db_id:
            thread_row = {
                "tenant_id": tenant_id,
                "gmail_thread_id": gmail_thread_id or None,
                "contact_email": to,
                "subject": subject,
                "status": "awaiting_reply",
                "last_message_at": now_iso,
                "inbox_item_id": body.inbox_item_id,
            }
            t_result = sb.table("email_threads").insert(thread_row).execute()
            if t_result.data:
                thread_db_id = t_result.data[0]["id"]

        # Save the outbound message record
        if thread_db_id:
            text_body = meta.get("text_body", "")
            preview = meta.get("preview_snippet", "")
            sb.table("email_messages").insert({
                "thread_id": thread_db_id,
                "tenant_id": tenant_id,
                "gmail_message_id": gmail_message_id or None,
                "direction": "outbound",
                "sender": config.owner_email,
                "recipients": to,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body,
                "preview_snippet": preview,
                "message_timestamp": now_iso,
                "approval_status": "sent",
            }).execute()
    except Exception as e:
        logger.warning("Thread tracking failed (email still sent): %s", e)

    await sio.emit("inbox_item_updated", {
        "id": body.inbox_item_id,
        "status": "sent",
    }, room=tenant_id)

    # Notify conversations page that a thread was updated
    await sio.emit("email_thread_updated", {
        "thread_id": gmail_thread_id,
        "status": "awaiting_reply",
    }, room=tenant_id)
    await _notify(
        tenant_id, "email_sent", f"Email sent to {to}",
        body=subject, href="/conversations",
        category="status", priority="normal",
    )

    return {"status": "sent", "message_id": gmail_message_id, "thread_id": gmail_thread_id}


class UpdateDraftRequest(BaseModel):
    inbox_item_id: str
    to: str = ""
    subject: str = ""
    html_body: str = ""


@app.post("/api/email/{tenant_id}/update-draft")
async def update_email_draft(tenant_id: str, body: UpdateDraftRequest):
    """Update an email draft's to, subject, or body before sending."""
    sb = _get_supabase()

    item_result = sb.table("inbox_items").select("*").eq("id", body.inbox_item_id).single().execute()
    item = item_result.data
    if not item:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    if item.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    if item.get("status") not in ("draft_pending_approval", "failed"):
        raise HTTPException(status_code=400, detail="Draft is not editable")

    draft = item.get("email_draft") or {}
    if body.to:
        draft["to"] = body.to
    if body.subject:
        draft["subject"] = body.subject
    if body.html_body:
        draft["html_body"] = body.html_body
        # Update text_body and preview_snippet from the new HTML
        import re
        text = re.sub(r'<[^>]+>', '', body.html_body).strip()
        draft["text_body"] = text
        draft["preview_snippet"] = text[:200]

    sb.table("inbox_items").update({
        "email_draft": draft,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).execute()

    return {"ok": True, "email_draft": draft}


@app.post("/api/email/{tenant_id}/cancel-draft")
async def cancel_email_draft(tenant_id: str, body: EmailApproveRequest):
    """Cancel a pending email draft."""
    sb = _get_supabase()
    sb.table("inbox_items").update({
        "status": "cancelled",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).eq("tenant_id", tenant_id).execute()
    return {"ok": True}


# ─── Email Threads & Sync ───

@app.get("/api/email/{tenant_id}/threads")
async def list_email_threads(tenant_id: str, status: str = ""):
    """List email conversation threads for a tenant."""
    sb = _get_supabase()
    query = sb.table("email_threads").select("*").eq("tenant_id", tenant_id)
    if status:
        query = query.eq("status", status)
    result = query.order("last_message_at", desc=True).execute()
    return {"threads": result.data or []}


@app.get("/api/email/{tenant_id}/threads/{thread_id}")
async def get_email_thread(tenant_id: str, thread_id: str):
    """Get a single thread with all its messages."""
    sb = _get_supabase()
    thread_result = sb.table("email_threads").select("*").eq(
        "id", thread_id
    ).eq("tenant_id", tenant_id).single().execute()
    if not thread_result.data:
        raise HTTPException(status_code=404, detail="Thread not found")

    messages_result = sb.table("email_messages").select("*").eq(
        "thread_id", thread_id
    ).order("message_timestamp", desc=False).execute()

    return {
        "thread": thread_result.data,
        "messages": messages_result.data or [],
    }


@app.post("/api/email/{tenant_id}/threads/{thread_id}/mark-read")
async def mark_thread_read(tenant_id: str, thread_id: str):
    """Mark a thread as read (status → open)."""
    sb = _get_supabase()
    sb.table("email_threads").update({
        "status": "open",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", thread_id).eq("tenant_id", tenant_id).execute()
    return {"ok": True}


class DraftReplyRequest(BaseModel):
    thread_id: str
    custom_instructions: str = ""


@app.post("/api/email/{tenant_id}/draft-reply")
async def generate_draft_reply(tenant_id: str, body: DraftReplyRequest):
    """Generate a suggested reply draft for an email thread.

    Uses the email marketer agent to draft a contextual reply based on the
    thread history. The draft is saved as draft_pending_approval — never sent.
    """
    from backend.tools.claude_cli import call_claude, MODEL_HAIKU

    sb = _get_supabase()

    # Fetch thread and messages
    thread_result = sb.table("email_threads").select("*").eq(
        "id", body.thread_id
    ).eq("tenant_id", tenant_id).single().execute()
    if not thread_result.data:
        raise HTTPException(status_code=404, detail="Thread not found")
    thread = thread_result.data

    messages_result = sb.table("email_messages").select("*").eq(
        "thread_id", body.thread_id
    ).order("message_timestamp", desc=False).execute()
    messages = messages_result.data or []

    if not messages:
        raise HTTPException(status_code=400, detail="No messages in this thread to reply to")

    # Build conversation context
    config = get_tenant_config(tenant_id)
    conversation = ""
    for msg in messages:
        direction = "SENT" if msg["direction"] == "outbound" else "RECEIVED"
        sender = msg.get("sender", "")
        body_text = msg.get("text_body", "") or msg.get("preview_snippet", "")
        conversation += f"\n[{direction}] From: {sender}\nSubject: {msg.get('subject', '')}\n{body_text}\n---\n"

    # Find the latest inbound message to reply to
    latest_inbound = None
    for msg in reversed(messages):
        if msg["direction"] == "inbound":
            latest_inbound = msg
            break
    if not latest_inbound:
        raise HTTPException(status_code=400, detail="No inbound message to reply to")

    instructions = body.custom_instructions or "Write a helpful, professional reply."

    system_prompt = f"""You are the Email Marketer for {config.business_name}.
Brand voice: {config.brand_voice.tone}
Business: {config.description}

Write a reply email based on the conversation thread below.
{instructions}

Output format:
SUBJECT: Re: <original subject>
---
<email body in HTML>

Keep it professional, concise, and on-brand. Do not include placeholder text."""

    user_prompt = f"Thread conversation:\n{conversation}\n\nDraft a reply to the latest inbound message."

    raw = await call_claude(system_prompt, user_prompt, max_tokens=1500, model=MODEL_HAIKU)

    # Parse the draft
    import re as _re
    subject_match = _re.match(r"(?:SUBJECT:\s*)(.+?)(?:\n---\n|\n\n)(.*)", raw, _re.DOTALL | _re.IGNORECASE)
    if subject_match:
        reply_subject = subject_match.group(1).strip()
        reply_body = subject_match.group(2).strip()
    else:
        reply_subject = f"Re: {thread.get('subject', '')}"
        reply_body = raw.strip()

    # Ensure HTML wrapping
    from backend.agents.email_marketer_agent import _wrap_html
    html_body = _wrap_html(reply_body)
    text_body = _re.sub(r'<[^>]+>', '', reply_body).strip()
    preview_snippet = text_body[:200]

    # Save draft message in the thread
    now_iso = datetime.now(timezone.utc).isoformat()
    draft_row = {
        "thread_id": body.thread_id,
        "tenant_id": tenant_id,
        "direction": "outbound",
        "sender": config.owner_email,
        "recipients": thread.get("contact_email", ""),
        "subject": reply_subject,
        "text_body": text_body,
        "html_body": html_body,
        "preview_snippet": preview_snippet,
        "message_timestamp": now_iso,
        "approval_status": "draft_pending_approval",
    }
    msg_result = sb.table("email_messages").insert(draft_row).execute()
    draft_msg = msg_result.data[0] if msg_result.data else {}

    # Also create an inbox item for visibility
    inbox_row = {
        "tenant_id": tenant_id,
        "agent": "email_marketer",
        "type": "email_sequence",
        "title": f"Draft Reply: {reply_subject}",
        "content": preview_snippet,
        "status": "draft_pending_approval",
        "priority": "high",
        "email_draft": {
            "to": thread.get("contact_email", ""),
            "subject": reply_subject,
            "html_body": html_body,
            "text_body": text_body,
            "preview_snippet": preview_snippet,
            "status": "draft_pending_approval",
            "reply_to_thread_id": body.thread_id,
            "reply_to_message_id": draft_msg.get("id", ""),
        },
    }
    inbox_result = sb.table("inbox_items").insert(inbox_row).execute()
    inbox_item = inbox_result.data[0] if inbox_result.data else {}

    # Update thread status
    sb.table("email_threads").update({
        "status": "replied",
        "updated_at": now_iso,
    }).eq("id", body.thread_id).execute()

    return {
        "draft": {
            "message_id": draft_msg.get("id", ""),
            "inbox_item_id": inbox_item.get("id", ""),
            "to": thread.get("contact_email", ""),
            "subject": reply_subject,
            "preview_snippet": preview_snippet,
            "status": "draft_pending_approval",
        },
    }


def _clean_notification_body(body: str) -> str:
    """Strip JSON/code artifacts from notification body to show clean text."""
    if not body:
        return ""
    import re
    text = body.strip()

    # Remove all markdown code fences (```json, ```delegate, ```)
    text = re.sub(r"```\w*\n?", "", text).strip()

    # Try to parse JSON and extract readable text
    json_match = re.search(r'[{\[]', text)
    if json_match:
        json_str = text[json_match.start():]
        try:
            import json as _j
            # Try to find complete JSON
            if json_str.startswith("["):
                end = json_str.rfind("]")
                if end > 0:
                    arr = _j.loads(json_str[:end + 1])
                    data = arr[0] if isinstance(arr, list) and arr else {}
                else:
                    data = {}
            else:
                end = json_str.rfind("}")
                if end > 0:
                    data = _j.loads(json_str[:end + 1])
                else:
                    data = {}

            # Extract readable text from known fields
            for key in ("text", "title", "description", "commentary", "body", "subject", "key_message"):
                if data.get(key):
                    return str(data[key])[:200]
            # Try nested posts
            for post in data.get("posts", [])[:2]:
                if post.get("text"):
                    return post["text"][:200]
        except Exception:
            pass

        # Fallback: strip all JSON syntax characters
        text = re.sub(r'[{}\[\]"\\]', '', text)
        text = re.sub(r'\s*:\s*', ': ', text)
        text = re.sub(r',\s*', ', ', text)
        text = re.sub(r'\s+', ' ', text).strip()

    return text[:200]


async def _notify(
    tenant_id: str,
    type: str,
    title: str,
    body: str = "",
    href: str = "",
    category: str = "inbox",
    priority: str = "normal",
) -> dict | None:
    """Persist a notification and emit it via Socket.IO."""
    try:
        clean_body = _clean_notification_body(body)
        sb = _get_supabase()
        row = {
            "tenant_id": tenant_id,
            "type": type,
            "category": category,
            "title": title,
            "body": clean_body,
            "href": href,
            "priority": priority,
            "is_read": False,
            "is_seen": False,
        }
        result = sb.table("notifications").insert(row).execute()
        saved = result.data[0] if result.data else row
        await sio.emit("notification", saved, room=tenant_id)
        return saved
    except Exception as e:
        logger.warning("Failed to save notification: %s", e)
        return None


async def _emit_sync_events(tenant_id: str, sync_result: dict):
    """Emit Socket.IO events for new inbound replies found during Gmail sync."""
    for reply in sync_result.get("new_replies", []):
        inbox_item = reply.get("inbox_item")
        if inbox_item:
            await sio.emit("inbox_new_item", {
                "id": inbox_item.get("id", ""),
                "agent": "email_marketer",
                "type": "email_reply",
                "title": inbox_item.get("title", ""),
                "status": "needs_review",
                "priority": "high",
                "created_at": inbox_item.get("created_at", ""),
            }, room=tenant_id)
        await sio.emit("email_reply_received", {
            "thread_id": reply.get("thread_id", ""),
            "sender": reply.get("sender", ""),
            "subject": reply.get("subject", ""),
            "snippet": reply.get("snippet", ""),
        }, room=tenant_id)
        await _notify(
            tenant_id, "reply_received",
            f"Reply from {reply.get('sender', 'someone')}",
            body=reply.get("snippet", "")[:200],
            href="/conversations",
            category="conversation",
            priority="high",
        )


@app.post("/api/email/{tenant_id}/sync")
async def trigger_email_sync(tenant_id: str):
    """Manually trigger Gmail inbound reply sync for a tenant."""
    from backend.tools.gmail_sync import sync_tenant_replies
    result = await sync_tenant_replies(tenant_id)
    await _emit_sync_events(tenant_id, result)
    return result


@app.post("/api/email/sync-all")
async def trigger_sync_all():
    """Trigger Gmail sync for all active tenants. Called by cron."""
    from backend.tools.gmail_sync import sync_all_tenants
    results = await sync_all_tenants()
    for r in results:
        tid = r.get("tenant_id", "")
        if tid:
            await _emit_sync_events(tid, r)
    return {"tenants_synced": len(results), "results": results}


# ─── Notifications ───

@app.get("/api/notifications/{tenant_id}/counts")
async def notification_counts(tenant_id: str):
    """Get unread notification counts by category."""
    sb = _get_supabase()
    result = sb.table("notifications").select("category", count="exact").eq(
        "tenant_id", tenant_id
    ).eq("is_read", False).execute()
    # Count per category from raw rows
    counts: dict[str, int] = {}
    for row in (result.data or []):
        cat = row.get("category", "other")
        counts[cat] = counts.get(cat, 0) + 1
    total = sum(counts.values())
    return {
        "inbox_unread": counts.get("inbox", 0),
        "conversations_unread": counts.get("conversation", 0),
        "system_unread": counts.get("system", 0),
        "status_unread": counts.get("status", 0),
        "total_unread": total,
    }


@app.get("/api/notifications/{tenant_id}")
async def list_notifications(tenant_id: str, category: str = "", unread_only: bool = False, limit: int = 30):
    """List recent notifications for a tenant."""
    sb = _get_supabase()
    query = sb.table("notifications").select("*").eq("tenant_id", tenant_id)
    if category:
        query = query.eq("category", category)
    if unread_only:
        query = query.eq("is_read", False)
    result = query.order("created_at", desc=True).limit(limit).execute()
    return {"notifications": result.data or []}


class MarkReadRequest(BaseModel):
    ids: list[str] = []  # empty = mark all


@app.post("/api/notifications/{tenant_id}/mark-read")
async def mark_notifications_read(tenant_id: str, body: MarkReadRequest):
    """Mark specific notification IDs (or all) as read."""
    sb = _get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    if body.ids:
        sb.table("notifications").update({"is_read": True, "updated_at": now}).eq(
            "tenant_id", tenant_id
        ).in_("id", body.ids).execute()
    else:
        sb.table("notifications").update({"is_read": True, "updated_at": now}).eq(
            "tenant_id", tenant_id
        ).eq("is_read", False).execute()
    return {"ok": True}


@app.post("/api/notifications/{tenant_id}/mark-seen")
async def mark_notifications_seen(tenant_id: str, body: MarkReadRequest):
    """Mark specific notification IDs (or all) as seen."""
    sb = _get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    if body.ids:
        sb.table("notifications").update({"is_seen": True, "updated_at": now}).eq(
            "tenant_id", tenant_id
        ).in_("id", body.ids).execute()
    else:
        sb.table("notifications").update({"is_seen": True, "updated_at": now}).eq(
            "tenant_id", tenant_id
        ).eq("is_seen", False).execute()
    return {"ok": True}


# ─── Webhook Endpoints ───
@app.post("/api/webhooks/sendgrid")
async def sendgrid_webhook(request: Request):
    payload = await request.json()
    tenant_id = request.headers.get("X-Tenant-Id", "")
    result = await handle_webhook("inbound_email", {"tenant_id": tenant_id, **payload})
    await sio.emit("agent_event", result, room=tenant_id)
    return result


@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.json()
    event_type = payload.get("type", "")
    tenant_id = payload.get("data", {}).get("object", {}).get("metadata", {}).get("tenant_id", "")
    if "invoice" in event_type:
        result = await handle_webhook("payment_received", {"tenant_id": tenant_id, **payload})
    else:
        result = {"status": "ignored", "event": event_type}
    return result


@app.post("/api/webhooks/shopify")
async def shopify_webhook(request: Request):
    payload = await request.json()
    tenant_id = request.headers.get("X-Tenant-Id", "")
    topic = request.headers.get("X-Shopify-Topic", "")
    event_map = {"orders/create": "new_order", "checkouts/create": "abandoned_cart"}
    event_type = event_map.get(topic, "unknown")
    result = await handle_webhook(event_type, {"tenant_id": tenant_id, **payload})
    return result


# ─── Agent Management API ───
@app.get("/api/agents/{tenant_id}")
async def list_agents(tenant_id: str):
    statuses = await get_agent_status(tenant_id)
    return {"tenant_id": tenant_id, "agents": statuses}


@app.post("/api/agents/{tenant_id}/{agent_name}/run")
async def run_agent(tenant_id: str, agent_name: str):
    # Agent starts working at desk
    await _emit_agent_status(tenant_id, agent_name, "working",
                             current_task=f"Running {agent_name} task",
                             action="start_work")

    result = await dispatch_agent(tenant_id, agent_name)
    await sio.emit("agent_event", result, room=tenant_id)

    # Agent done — return to idle
    await _emit_agent_status(tenant_id, agent_name, "idle",
                             action="task_complete")

    # Save output to inbox
    content = result.get("result", "")
    if content and isinstance(content, str):
        content_type = _infer_content_type(agent_name, content)
        title = _extract_title(agent_name, "", content)
        saved = _save_inbox_item(
            tenant_id=tenant_id,
            agent=agent_name,
            title=title,
            content=content,
            content_type=content_type,
        )
        if saved:
            await sio.emit("inbox_new_item", {
                "id": saved["id"],
                "agent": agent_name,
                "type": content_type,
                "title": title,
                "status": "ready",
                "created_at": saved.get("created_at", ""),
            }, room=tenant_id)

    return result


@app.post("/api/agents/{tenant_id}/{agent_name}/pause")
async def pause_agent(tenant_id: str, agent_name: str):
    config = get_tenant_config(tenant_id)
    if agent_name in config.active_agents:
        config.active_agents.remove(agent_name)
        save_tenant_config(config)
    # Also pause in Paperclip orchestrator
    await pause_agent_paperclip(agent_name)
    return {"status": "paused", "agent": agent_name}


@app.post("/api/agents/{tenant_id}/{agent_name}/resume")
async def resume_agent(tenant_id: str, agent_name: str):
    config = get_tenant_config(tenant_id)
    if agent_name not in config.active_agents:
        config.active_agents.append(agent_name)
        save_tenant_config(config)
    # Also resume in Paperclip orchestrator
    await resume_agent_paperclip(agent_name)
    return {"status": "resumed", "agent": agent_name}


# ─── Virtual Office API ───
@app.get("/api/office/agents/{tenant_id}")
async def virtual_office_agents(tenant_id: str):
    """Return all virtual office agents with their current persisted status."""
    now = datetime.now(timezone.utc).isoformat()
    live = _live_agent_status.get(tenant_id, {})

    # Load persisted status from Supabase (survives page navigation)
    db_statuses: dict[str, dict] = {}
    try:
        sb = _get_supabase()
        result = sb.table("agent_status").select("agent_id,status,current_task,updated_at").eq(
            "tenant_id", tenant_id
        ).execute()
        for row in (result.data or []):
            db_statuses[row["agent_id"]] = row
    except Exception:
        pass

    # Also check tasks table for agents with in_progress tasks
    task_statuses: dict[str, str] = {}
    try:
        sb = _get_supabase()
        result = sb.table("tasks").select("agent,task").eq(
            "tenant_id", tenant_id
        ).eq("status", "in_progress").execute()
        for t in (result.data or []):
            task_statuses[t["agent"]] = t["task"]
    except Exception:
        pass

    agents = []
    for a in VIRTUAL_OFFICE_AGENTS:
        aid = a["agent_id"]
        live_entry = live.get(aid, {})
        db_entry = db_statuses.get(aid, {})
        live_status = live_entry.get("status")
        db_status = db_entry.get("status")

        # Priority: in-memory live > persisted DB > task-based > idle
        if live_status and live_status not in ("idle",):
            status = live_status
            current_task = live_entry.get("current_task", "")
            last_updated = live_entry.get("last_updated", now)
        elif db_status and db_status not in ("idle",):
            status = db_status
            current_task = db_entry.get("current_task", "")
            last_updated = db_entry.get("updated_at", now)
        elif aid in task_statuses:
            status = "working"
            current_task = task_statuses[aid]
            last_updated = now
        else:
            status = "idle"
            current_task = ""
            last_updated = now

        agents.append({
            "agent_id": aid,
            "name": a["name"],
            "role": a["role"],
            "model": a["model"],
            "status": status,
            "current_task": current_task,
            "department": a["department"],
            "last_updated": last_updated,
        })
    return {"agents": agents}


# ─── Dashboard API ───
@app.get("/api/dashboard/{tenant_id}/config")
async def dashboard_config(tenant_id: str):
    """Return tenant business info for the dashboard."""
    try:
        config = get_tenant_config(tenant_id)
        return {
            "tenant_id": tenant_id,
            "business_name": config.business_name,
            "product_name": config.product.name,
            "product_description": config.product.description,
            "positioning": config.gtm_playbook.positioning,
            "channels": config.channels,
            "active_agents": config.active_agents,
            "brand_voice_tone": config.brand_voice.tone,
            "action_plan_30": config.gtm_playbook.action_plan_30,
            "messaging_pillars": config.gtm_playbook.messaging_pillars,
            "onboarding_status": config.onboarding_status,
            "skipped_fields": config.skipped_fields,
        }
    except Exception:
        return {"tenant_id": tenant_id, "business_name": None}


@app.get("/api/dashboard/{tenant_id}/stats")
async def dashboard_stats(tenant_id: str):
    return {
        "tenant_id": tenant_id,
        "kpis": {
            "content_published": {"value": 0, "delta": 0, "delta_pct": 0},
            "emails_sent": {"value": 0, "open_rate": 0, "click_rate": 0},
            "social_engagement": {"value": 0, "delta_pct": 0},
            "ad_spend": {"value": 0, "roas": 0},
        },
    }


@app.get("/api/dashboard/{tenant_id}/activity")
async def dashboard_activity(tenant_id: str):
    """Return recent activity from inbox items and tasks."""
    sb = _get_supabase()
    activity = []
    try:
        # Recent inbox deliverables
        inbox_result = sb.table("inbox_items").select("agent,type,title,created_at").eq(
            "tenant_id", tenant_id
        ).order("created_at", desc=True).limit(20).execute()
        for item in (inbox_result.data or []):
            activity.append({
                "agent": item["agent"],
                "action": f"Delivered: {item['title'][:60]}",
                "type": item["type"],
                "timestamp": item["created_at"],
            })
    except Exception:
        pass
    try:
        # Recent completed tasks
        task_result = sb.table("tasks").select("agent,task,status,created_at").eq(
            "tenant_id", tenant_id
        ).order("created_at", desc=True).limit(20).execute()
        for task in (task_result.data or []):
            status_verb = "Completed" if task["status"] == "done" else "Working on"
            activity.append({
                "agent": task["agent"],
                "action": f"{status_verb}: {task['task'][:60]}",
                "type": "task",
                "timestamp": task["created_at"],
            })
    except Exception:
        pass
    # Sort by timestamp, newest first
    activity.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return {"tenant_id": tenant_id, "activity": activity[:30]}


@app.get("/api/dashboard/{tenant_id}/inbox")
async def dashboard_inbox(tenant_id: str):
    """Return inbox items for the dashboard (latest 5)."""
    try:
        sb = _get_supabase()
        result = sb.table("inbox_items").select("id,title,agent,type,status,priority,created_at").eq("tenant_id", tenant_id).order("created_at", desc=True).limit(5).execute()
        return {"tenant_id": tenant_id, "items": result.data}
    except Exception:
        return {"tenant_id": tenant_id, "items": []}




@app.get("/api/analytics/{tenant_id}")
async def analytics_data(tenant_id: str, date_range: str = "7d"):
    return {
        "tenant_id": tenant_id,
        "date_range": date_range,
        "funnel": {
            "impressions": 0, "clicks": 0, "signups": 0,
            "activated": 0, "converted": 0, "retained": 0,
        },
    }


# ─── Paperclip AI Integration ───
@app.get("/api/paperclip/status")
async def paperclip_status():
    """Check if Paperclip AI orchestrator is connected."""
    from backend.paperclip_sync import get_company_id, _agent_id_cache
    return {
        "connected": paperclip_connected(),
        "company_id": get_company_id(),
        "agents_registered": len(_agent_id_cache),
        "url": os.environ.get("PAPERCLIP_API_URL", "http://127.0.0.1:3100"),
    }


@app.post("/api/paperclip/heartbeat/{agent_name}")
async def paperclip_heartbeat(agent_name: str, request: Request):
    """Callback endpoint for Paperclip heartbeat invocations.

    When Paperclip triggers a heartbeat, it POSTs here. ARIA executes the
    agent logic and returns the result to Paperclip.
    """
    payload = await request.json()
    tenant_id = payload.get("metadata", {}).get("tenant_id")
    context = payload.get("metadata", {}).get("context", {})
    run_id = request.headers.get("X-Paperclip-Run-Id", "")

    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id required in metadata")

    from backend.agents import AGENT_REGISTRY
    agent_module = AGENT_REGISTRY.get(agent_name)
    if not agent_module:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}")

    try:
        result = await agent_module.run(
            tenant_id,
            **({"context": context} if context and "context" in agent_module.run.__code__.co_varnames else {}),
        )
        result["paperclip_run_id"] = run_id
        await sio.emit("agent_event", result, room=tenant_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── CEO Task Triage ───
class TriageRequest(BaseModel):
    title: str

@app.post("/api/ceo/triage")
async def ceo_triage(body: TriageRequest):
    """CEO agent analyzes a task and returns column, priority, and assigned agent."""
    from backend.tools.claude_cli import call_claude
    import json as _json

    system = (
        "You are the ARIA CEO, a Chief Marketing Strategist. "
        "Given a marketing task description, classify it by returning ONLY a JSON object with these fields:\n"
        '- "column": one of "backlog", "todo", "in_progress" (use your judgment: vague/aspirational ideas → backlog, concrete actionable tasks → todo, urgent/time-sensitive → in_progress)\n'
        '- "priority": one of "low", "medium", "high" (based on impact and urgency)\n'
        '- "agent": one of "ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist" (the best agent for the job)\n'
        '- "reason": one short sentence explaining your decision\n'
        "Return ONLY valid JSON, no markdown, no explanation outside the JSON."
    )
    try:
        raw = await call_claude(system, f"Triage this task: {body.title}", tenant_id="global")
        # Extract JSON from response
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            result = _json.loads(raw[start:end])
            # Validate values
            if result.get("column") not in ("backlog", "todo", "in_progress"):
                result["column"] = "todo"
            if result.get("priority") not in ("low", "medium", "high"):
                result["priority"] = "medium"
            if result.get("agent") not in ("ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist"):
                result["agent"] = "ceo"
            return result
        return {"column": "todo", "priority": "medium", "agent": "ceo", "reason": "Could not parse CEO response"}
    except Exception:
        return {"column": "todo", "priority": "medium", "agent": "ceo", "reason": "CEO agent unavailable, using defaults"}


# ─── Cron trigger endpoint ───
@app.post("/api/cron/run-scheduled")
async def cron_trigger():
    results = await run_scheduled_agents()

    # Also run Gmail inbound reply sync for all connected tenants
    sync_results = []
    try:
        from backend.tools.gmail_sync import sync_all_tenants
        sync_results = await sync_all_tenants()
        for sr in sync_results:
            tid = sr.get("tenant_id", "")
            if tid:
                await _emit_sync_events(tid, sr)
    except Exception as e:
        logger.warning("Gmail sync during cron failed: %s", e)

    total_imported = sum(r.get("imported", 0) for r in sync_results)
    return {
        "status": "completed",
        "tasks_run": len(results) if results else 0,
        "email_sync": {
            "tenants_synced": len(sync_results),
            "total_imported": total_imported,
        },
    }


# ─── Inbox helpers ───

def _infer_content_type(agent: str, content: str) -> str:
    """Infer the content type from the agent slug and output."""
    type_map = {
        "content_writer": "blog_post",
        "email_marketer": "email_sequence",
        "social_manager": "social_post",
        "ad_strategist": "ad_campaign",
        "ceo": "strategy_update",
    }
    return type_map.get(agent, "general")


def _extract_title(agent: str, task_desc: str, content: str) -> str:
    """Extract a short title from the task description or content."""
    if task_desc and len(task_desc) > 5:
        title = task_desc[:120].split("\n")[0]
        if len(task_desc) > 120:
            title = title.rsplit(" ", 1)[0] + "..."
        return title
    # Fallback: first non-empty line of content
    for line in content.split("\n"):
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return f"{agent} output"


def _save_inbox_item(
    tenant_id: str,
    agent: str,
    title: str,
    content: str,
    content_type: str = "general",
    priority: str = "medium",
    task_id: str | None = None,
    chat_session_id: str | None = None,
    status: str = "ready",
    email_draft: dict | None = None,
) -> dict | None:
    """Save an agent output to the inbox_items table. Returns the saved row."""
    _logger = logging.getLogger("aria.inbox")
    try:
        sb = _get_supabase()
        row = {
            "tenant_id": tenant_id,
            "agent": agent,
            "type": content_type,
            "title": title,
            "content": content,
            "status": status,
            "priority": priority,
        }
        if task_id:
            row["task_id"] = task_id
        if chat_session_id:
            row["chat_session_id"] = chat_session_id
        if email_draft:
            row["email_draft"] = email_draft
        result = sb.table("inbox_items").insert(row).execute()
        _logger.info("Saved inbox item: agent=%s title=%s status=%s", agent, title[:60], status)
        return result.data[0] if result.data else None
    except Exception as e:
        _logger.error("Failed to save inbox item: %s", e)
        return None


async def _run_agent_to_inbox(
    agent_module, agent_id: str, tenant_id: str, task_desc: str,
    session_id: str | None = None, task_id: str | None = None,
    priority: str = "medium",
):
    """Run an agent in background, drive office movement from real execution.

    Lifecycle:
      1. Brief meeting phase (1s) — CEO + agent walk to meeting room
      2. CEO returns to desk (idle), agent returns to desk (working)
      3. Agent executes for real — stays in "working"
      4. Agent stays "working" until task is moved to "done" on Kanban board
         (no auto-idle — task board is the source of truth)
    """
    import asyncio

    try:
        # Phase 1: Meeting (CEO + agent already walking to meeting room via caller)
        await asyncio.sleep(1)

        # Phase 2: CEO returns to desk
        if tenant_id:
            await _emit_agent_status(tenant_id, "ceo", "idle",
                                     action="return_to_desk")
            # Agent returns to desk and starts working
            await _emit_agent_status(tenant_id, agent_id, "working",
                                     current_task=task_desc,
                                     action="return_and_work")

        # Phase 3: Create placeholder inbox item immediately so it shows up in the inbox
        _logger = logging.getLogger("aria.inbox")
        _logger.info("Running agent %s for tenant %s — task: %s", agent_id, tenant_id, task_desc[:100])

        placeholder_content_type = _infer_content_type(agent_id, "")
        placeholder = _save_inbox_item(
            tenant_id=tenant_id,
            agent=agent_id,
            title=f"{agent_id.replace('_', ' ').title()} is working on: {task_desc[:80]}",
            content="Agent is processing this task...",
            content_type=placeholder_content_type,
            priority=priority,
            task_id=task_id,
            chat_session_id=session_id,
            status="processing",
        )
        placeholder_id = placeholder["id"] if placeholder else None

        # Notify frontend of the placeholder
        if placeholder and tenant_id:
            await sio.emit("inbox_new_item", {
                "id": placeholder_id,
                "agent": agent_id,
                "type": placeholder_content_type,
                "title": placeholder.get("title", ""),
                "status": "processing",
                "priority": priority,
                "created_at": placeholder.get("created_at", ""),
            }, room=tenant_id)

        # Phase 4: Actually run the agent (this is where real time is spent)
        result = await agent_module.run(tenant_id, context={"action": task_desc})
        content = result.get("result", "")
        _logger.info("Agent %s returned %d chars, keys: %s", agent_id, len(content), list(result.keys()))

        if not content and not result.get("email_draft"):
            _logger.warning("Agent %s returned empty content for tenant %s", agent_id, tenant_id)
            # Update placeholder to show it's done (empty result)
            if placeholder_id:
                try:
                    sb = _get_supabase()
                    sb.table("inbox_items").update({
                        "title": f"Completed: {task_desc[:80]}",
                        "content": "Agent completed but produced no output.",
                        "status": "completed",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", placeholder_id).execute()
                except Exception:
                    pass
            if task_id:
                try:
                    sb = _get_supabase()
                    sb.table("tasks").update({
                        "status": "done",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", task_id).execute()
                except Exception:
                    pass
                if tenant_id:
                    await sio.emit("task_updated", {
                        "id": task_id,
                        "agent": agent_id,
                        "status": "done",
                        "task": task_desc,
                    }, room=tenant_id)
                    try:
                        sb2 = _get_supabase()
                        other = sb2.table("tasks").select("id").eq(
                            "tenant_id", tenant_id
                        ).eq("agent", agent_id).eq("status", "in_progress").limit(1).execute()
                        if not other.data:
                            await _emit_agent_status(tenant_id, agent_id, "idle",
                                                     action="all_tasks_complete")
                    except Exception:
                        pass
            return

        content_type = _infer_content_type(agent_id, content)
        title = _extract_title(agent_id, task_desc, content)

        # If the agent returned an email draft, save as pending approval
        email_draft = result.get("email_draft")
        if email_draft:
            item_status = "draft_pending_approval"
            if email_draft.get("subject"):
                title = f"Email: {email_draft['subject']}"
            content = email_draft.get("preview_snippet", content)
        else:
            item_status = "ready"

        # Update the placeholder with the real content
        if placeholder_id:
            try:
                sb = _get_supabase()
                update_data: dict = {
                    "title": title,
                    "content": content,
                    "type": content_type,
                    "status": item_status,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                if email_draft:
                    update_data["email_draft"] = email_draft
                sb.table("inbox_items").update(update_data).eq("id", placeholder_id).execute()
                _logger.info("Updated inbox placeholder %s with real content", placeholder_id)
            except Exception as e:
                _logger.error("Failed to update placeholder: %s", e)
                # Fallback: save as new item
                placeholder_id = None

        # If placeholder update failed, save as new item
        if not placeholder_id:
            saved = _save_inbox_item(
                tenant_id=tenant_id,
                agent=agent_id,
                title=title,
                content=content,
                content_type=content_type,
                priority=priority,
                task_id=task_id,
                chat_session_id=session_id,
                status=item_status,
                email_draft=email_draft,
            )
            placeholder_id = saved["id"] if saved else None

        # Emit real-time update to frontend (replaces placeholder)
        if placeholder_id and tenant_id:
            await sio.emit("inbox_item_updated", {
                "id": placeholder_id,
                "agent": agent_id,
                "type": content_type,
                "title": title,
                "status": item_status,
                "priority": priority,
            }, room=tenant_id)
            n_type = "approval_needed" if item_status == "draft_pending_approval" else "inbox_new_item"
            await _notify(
                tenant_id, n_type, title,
                body=content[:200] if content else "",
                href="/inbox",
                category="inbox",
                priority=priority,
            )

        # Mark task as done and notify frontend in real-time
        if task_id:
            try:
                sb = _get_supabase()
                sb.table("tasks").update({
                    "status": "done",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", task_id).execute()
            except Exception:
                pass

            # Emit task_updated so Kanban board auto-refreshes
            if tenant_id:
                await sio.emit("task_updated", {
                    "id": task_id,
                    "agent": agent_id,
                    "status": "done",
                    "task": task_desc,
                }, room=tenant_id)

            # Agent done — return to idle
            if tenant_id:
                try:
                    sb2 = _get_supabase()
                    other = sb2.table("tasks").select("id").eq(
                        "tenant_id", tenant_id
                    ).eq("agent", agent_id).eq("status", "in_progress").limit(1).execute()
                    if not other.data:
                        await _emit_agent_status(tenant_id, agent_id, "idle",
                                                 action="all_tasks_complete")
                except Exception:
                    pass

    except Exception as e:
        logging.getLogger("aria.inbox").error("Agent %s failed for tenant %s: %s", agent_id, tenant_id, e)
        # Save error to inbox so user can see what went wrong
        _save_inbox_item(
            tenant_id=tenant_id,
            agent=agent_id,
            title=f"Failed: {task_desc[:60]}",
            content=f"The {agent_id} agent encountered an error while processing this task:\n\n"
                    f"**Task:** {task_desc}\n\n"
                    f"**Error:** {e}\n\n"
                    "Please try again. If this persists, check Settings > Integrations to ensure Gmail is connected.",
            content_type="error",
            priority=priority,
            task_id=task_id,
            chat_session_id=session_id,
        )
        # Mark task as done so it doesn't stay stuck in_progress
        if task_id:
            try:
                sb = _get_supabase()
                sb.table("tasks").update({
                    "status": "done",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", task_id).execute()
                if tenant_id:
                    await sio.emit("task_updated", {
                        "id": task_id, "agent": agent_id,
                        "status": "done", "task": task_desc,
                    }, room=tenant_id)
            except Exception:
                pass
        # Return agent to idle so it doesn't get stuck
        if tenant_id:
            try:
                await _emit_agent_status(tenant_id, agent_id, "idle",
                                         action="task_failed")
            except Exception:
                pass


# ─── CRM API — moved to backend/routers/crm.py ───
# ─── Inbox API — moved to backend/routers/inbox.py ───


# ─── Inbox Item Creation (for Paperclip agents) ───

class CreateInboxItem(BaseModel):
    title: str
    content: str
    type: str = "blog"
    agent: str = "content_writer"
    priority: str = "medium"
    status: str = "needs_review"
    email_draft: dict | None = None


@app.post("/api/inbox/{tenant_id}/items")
async def create_inbox_item(tenant_id: str, body: CreateInboxItem):
    """Create an inbox item — used by Paperclip agents to store their output."""
    sb = _get_supabase()
    row = {
        "tenant_id": tenant_id,
        "title": body.title,
        "content": body.content,
        "type": body.type,
        "agent": body.agent,
        "priority": body.priority,
        "status": body.status,
    }
    if body.email_draft:
        row["email_draft"] = body.email_draft

    result = sb.table("inbox_items").insert(row).execute()
    item = result.data[0] if result.data else None

    # Emit real-time notification
    if item and tenant_id:
        await sio.emit("inbox_updated", {"action": "created", "item": item}, room=tenant_id)
        # Create notification
        try:
            sb.table("notifications").insert({
                "tenant_id": tenant_id,
                "title": f"New from {body.agent}: {body.title}",
                "body": body.content[:200],
                "category": "inbox",
                "href": "/inbox",
            }).execute()
        except Exception:
            pass

    return {"item": item}


# ─── CEO Actions ───

def _get_ceo_action_descriptions() -> str:
    """Get compact action descriptions for CEO system prompt."""
    from backend.ceo_actions import get_action_descriptions
    return get_action_descriptions()


def _format_action_result(action_name: str, result: dict) -> str:
    """Format an action result as readable markdown for the chat response."""
    if not result:
        return ""

    # ── Error results ──
    if result.get("error"):
        return f"**Error:** {result['error']}"

    # ═══════════ READ operations ═══════════

    # ── Contacts ──
    if action_name == "read_contacts":
        contacts = result.get("contacts", [])
        total = result.get("total", len(contacts))
        if not contacts:
            return "No contacts found in your CRM."
        lines = [f"**CRM Contacts** ({total} total)\n"]
        lines.append("| Name | Email | Status | Source |")
        lines.append("|------|-------|--------|--------|")
        for c in contacts[:20]:
            lines.append(f"| {c.get('name', '—')} | {c.get('email') or '—'} | {c.get('status') or '—'} | {c.get('source') or '—'} |")
        if total > 20:
            lines.append(f"\n*Showing 20 of {total} contacts.*")
        return "\n".join(lines)

    if action_name == "read_companies":
        companies = result.get("companies", [])
        if not companies:
            return "No companies found in your CRM."
        lines = [f"**CRM Companies** ({len(companies)} total)\n"]
        lines.append("| Name | Domain | Industry | Size |")
        lines.append("|------|--------|----------|------|")
        for c in companies[:20]:
            lines.append(f"| {c.get('name', '—')} | {c.get('domain') or '—'} | {c.get('industry') or '—'} | {c.get('size') or '—'} |")
        return "\n".join(lines)

    if action_name == "read_deals":
        deals = result.get("deals", [])
        if not deals:
            return "No deals found in your pipeline."
        lines = [f"**CRM Deals** ({len(deals)} total)\n"]
        lines.append("| Title | Value | Stage |")
        lines.append("|-------|-------|-------|")
        for d in deals[:20]:
            val = d.get("value", 0)
            lines.append(f"| {d.get('title', '—')} | {f'${val:,.0f}' if val else '—'} | {d.get('stage') or '—'} |")
        return "\n".join(lines)

    if action_name == "read_inbox":
        items = result.get("items", [])
        if not items:
            return "Your inbox is empty."
        lines = [f"**Inbox** ({len(items)} items)\n"]
        for i, item in enumerate(items[:15], 1):
            title = item.get("title", item.get("type", "Item"))
            lines.append(f"{i}. **{title}** — {item.get('status', '—')} (from {item.get('agent', '—')})")
        return "\n".join(lines)

    if action_name == "read_tasks":
        tasks = result.get("tasks", [])
        if not tasks:
            return "No tasks found."
        lines = [f"**Tasks** ({len(tasks)} total)\n"]
        lines.append("| Agent | Task | Priority | Status |")
        lines.append("|-------|------|----------|--------|")
        for t in tasks[:20]:
            lines.append(f"| {t.get('agent', '—')} | {t.get('task', '—')[:60]} | {t.get('priority') or '—'} | {t.get('status') or '—'} |")
        return "\n".join(lines)

    if action_name == "read_activities":
        activities = result.get("activities", [])
        if not activities:
            return "No CRM activities found."
        lines = [f"**CRM Activities** ({len(activities)} recent)\n"]
        for a in activities[:15]:
            ts = a.get("created_at", "")[:10] if a.get("created_at") else ""
            lines.append(f"- **{a.get('type', '—')}** — {a.get('description', '—')} ({ts})")
        return "\n".join(lines)

    if action_name == "read_email_threads":
        threads = result.get("threads", [])
        if not threads:
            return "No email threads found."
        lines = [f"**Email Threads** ({len(threads)} total)\n"]
        lines.append("| Subject | Contact | Status |")
        lines.append("|---------|---------|--------|")
        for t in threads[:20]:
            lines.append(f"| {t.get('subject') or '—'} | {t.get('contact_email') or '—'} | {t.get('status') or '—'} |")
        return "\n".join(lines)

    if action_name == "read_notifications":
        notifs = result.get("notifications", [])
        if not notifs:
            return "No notifications."
        lines = [f"**Notifications** ({len(notifs)} recent)\n"]
        for n in notifs[:15]:
            read_icon = "" if n.get("is_read") else " (unread)"
            lines.append(f"- **{n.get('title', '—')}**{read_icon} — {n.get('category', '')} — {(n.get('created_at') or '')[:10]}")
        return "\n".join(lines)

    if action_name == "read_agent_logs":
        logs = result.get("logs", [])
        if not logs:
            return "No agent logs found."
        lines = [f"**Agent Logs** ({len(logs)} recent)\n"]
        lines.append("| Agent | Action | Status | Time |")
        lines.append("|-------|--------|--------|------|")
        for l in logs[:20]:
            ts = (l.get("timestamp") or "")[:16].replace("T", " ")
            lines.append(f"| {l.get('agent_name', '—')} | {l.get('action', '—')[:40]} | {l.get('status') or '—'} | {ts} |")
        return "\n".join(lines)

    # ═══════════ CREATE operations ═══════════

    if action_name == "create_contact":
        c = result.get("contact", {})
        if c:
            return f"**Contact created:** {c.get('name', '')} ({c.get('email') or 'no email'}) — Status: {c.get('status', 'lead')}"
        return "Contact created."

    if action_name == "create_company":
        c = result.get("company", {})
        if c:
            return f"**Company created:** {c.get('name', '')}"
        return "Company created."

    if action_name == "create_deal":
        d = result.get("deal", {})
        if d:
            val = d.get("value", 0)
            return f"**Deal created:** {d.get('title', '')} — {f'${val:,.0f}' if val else 'no value'} — Stage: {d.get('stage', 'lead')}"
        return "Deal created."

    if action_name == "create_task":
        t = result.get("task", {})
        if t:
            return f"**Task created:** {t.get('task', '')} — Assigned to: {t.get('agent', '')} — Priority: {t.get('priority', 'medium')}"
        return "Task created."

    if action_name == "create_activity":
        a = result.get("activity", {})
        if a:
            return f"**Activity logged:** {a.get('type', '')} — {a.get('description', '')}"
        return "Activity logged."

    # ═══════════ UPDATE operations ═══════════

    if action_name == "update_contact":
        changes = result.get("changes", {})
        return f"**Contact updated** (ID: {result.get('updated', '—')}). Changes: {', '.join(f'{k}={v}' for k, v in changes.items() if k != 'updated_at')}"

    if action_name == "update_company":
        changes = result.get("changes", {})
        return f"**Company updated** (ID: {result.get('updated', '—')}). Changes: {', '.join(f'{k}={v}' for k, v in changes.items() if k != 'updated_at')}"

    if action_name == "update_deal":
        changes = result.get("changes", {})
        return f"**Deal updated** (ID: {result.get('updated', '—')}). Changes: {', '.join(f'{k}={v}' for k, v in changes.items() if k != 'updated_at')}"

    if action_name == "update_task_status":
        return f"**Task updated** (ID: {result.get('updated', '—')}) — New status: {result.get('new_status', '—')}"

    if action_name == "update_inbox_status":
        return f"**Inbox item updated** (ID: {result.get('updated', '—')}) — New status: {result.get('new_status', '—')}"

    if action_name == "update_email_thread":
        return f"**Email thread updated** (ID: {result.get('updated', '—')}) — New status: {result.get('new_status', '—')}"

    if action_name == "mark_notifications_read":
        count = result.get("marked_read", 0)
        return f"**Notifications marked as read:** {count}"

    # ═══════════ DELETE operations ═══════════

    if action_name in ("delete_contact", "delete_company", "delete_deal", "delete_task", "delete_inbox_item"):
        entity = action_name.replace("delete_", "").replace("_", " ").title()
        return f"**{entity} deleted** (ID: {result.get('deleted', '—')})"

    # ═══════════ Special operations ═══════════

    if action_name == "publish_social_post":
        return f"**Post published to Twitter** — Tweet ID: {result.get('tweet_id', '—')}"

    if action_name == "publish_to_linkedin":
        return f"**Post published to LinkedIn** — Post ID: {result.get('post_id', '—')}"

    if action_name == "send_email_draft":
        return f"**Email sent** to {result.get('to', '—')} — Subject: {result.get('subject', '—')}"

    if action_name == "send_whatsapp":
        return f"**WhatsApp message sent** to {result.get('to', '—')}"

    if action_name == "sync_gmail":
        return f"**Gmail synced** — {result.get('imported', 0)} new messages imported"

    if action_name == "run_agent":
        return f"**Agent `{result.get('ran', '—')}` started** — Status: {result.get('status', '—')}"

    if action_name == "draft_email_reply":
        return f"**Email reply drafted** for thread {result.get('thread_id', '—')} — sent to inbox for approval"

    if action_name == "cancel_draft":
        return f"**Draft cancelled** (ID: {result.get('updated', '—')})"

    # ═══════════ Scheduler operations ═══════════

    if action_name == "schedule_task":
        t = result.get("task", {})
        if t:
            return f"**Task scheduled:** {t.get('title', '')} — {t.get('task_type', '')} at {t.get('scheduled_at', '')}"
        return "Task scheduled."

    if action_name == "read_scheduled_tasks":
        tasks = result.get("tasks", [])
        if not tasks:
            return "No scheduled tasks found."
        lines = [f"**Scheduled Tasks** ({len(tasks)} total)\n"]
        lines.append("| Title | Type | Scheduled At | Status |")
        lines.append("|-------|------|-------------|--------|")
        for t in tasks[:20]:
            sa = (t.get("scheduled_at") or "")[:16].replace("T", " ")
            lines.append(f"| {t.get('title', '—')} | {t.get('task_type', '—')} | {sa} | {t.get('status', '—')} |")
        return "\n".join(lines)

    if action_name == "reschedule_task":
        return f"**Task rescheduled** (ID: {result.get('updated', '—')}) — New time: {result.get('changes', {}).get('scheduled_at', '—')}"

    if action_name == "cancel_scheduled_task":
        return f"**Scheduled task cancelled** (ID: {result.get('updated', '—')})"

    if action_name == "execute_scheduled_now":
        if result.get("error"):
            return f"**Execution failed:** {result['error']}"
        return "**Task executed immediately** — check inbox/notifications for results."

    return ""


class CEOActionRequest(BaseModel):
    action: str
    params: dict = {}
    confirmed: bool = False


@app.post("/api/ceo/{tenant_id}/action")
async def ceo_execute_action(tenant_id: str, body: CEOActionRequest):
    """Execute a CEO business action with confirmation enforcement."""
    from backend.ceo_actions import execute_action, is_forbidden_request

    result = await execute_action(
        tenant_id=tenant_id,
        action_name=body.action,
        params=body.params,
        confirmed=body.confirmed,
    )

    if result["status"] == "needs_confirmation":
        return result  # Frontend shows confirmation dialog

    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result.get("message", "Action failed"))

    # Emit real-time update with entity type for targeted refresh
    action_def = None
    try:
        from backend.ceo_actions import ACTION_REGISTRY
        action_def = ACTION_REGISTRY.get(body.action, {})
    except Exception:
        pass
    await sio.emit("ceo_action_executed", {
        "action": body.action,
        "entity": action_def.get("entity", "") if action_def else "",
        "result": result,
    }, room=tenant_id)

    return result


# ─── CEO Chat ───
import pathlib as _pathlib

_AGENTS_DIR = _pathlib.Path(__file__).resolve().parent.parent / "docs" / "agents"
_CEO_MD_FULL = (_AGENTS_DIR / "ceo.md").read_text(encoding="utf-8")
_CEO_MD = _CEO_MD_FULL[:800]  # Truncate to ~200 tokens — prompt caching handles the rest
_AGENT_MDS = {}
for _f in _AGENTS_DIR.glob("*.md"):
    _AGENT_MDS[_f.stem] = _f.read_text(encoding="utf-8")
# Load skill files
_SKILLS_DIR = _AGENTS_DIR / "skills"
if _SKILLS_DIR.exists():
    for _f in _SKILLS_DIR.glob("*.md"):
        _AGENT_MDS[f"skill_{_f.stem}"] = _f.read_text(encoding="utf-8")

# In-memory chat cache with LRU eviction (max 100 sessions)
_chat_sessions: dict[str, list[dict]] = {}
_MAX_CACHED_SESSIONS = 100


def _evict_chat_sessions():
    """Remove oldest sessions if cache exceeds max size."""
    if len(_chat_sessions) > _MAX_CACHED_SESSIONS:
        excess = len(_chat_sessions) - _MAX_CACHED_SESSIONS
        for key in list(_chat_sessions.keys())[:excess]:
            del _chat_sessions[key]


def _save_chat_message(session_id: str, tenant_id: str, role: str, content: str, delegations: list | None = None):
    """Persist a single chat message to Supabase."""
    try:
        sb = _get_supabase()
        # Ensure session row exists
        sb.table("chat_sessions").upsert({
            "id": session_id,
            "tenant_id": tenant_id or None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="id").execute()
        # Insert message
        sb.table("chat_messages").insert({
            "session_id": session_id,
            "role": role,
            "content": content,
            "delegations": delegations or [],
        }).execute()
    except Exception:
        pass


def _auto_title(session_id: str, first_message: str):
    """Set the chat title from the user's first message."""
    title = first_message[:80].split("\n")[0]
    if len(first_message) > 80:
        title = title.rsplit(" ", 1)[0] + "..."
    try:
        sb = _get_supabase()
        sb.table("chat_sessions").update({"title": title}).eq("id", session_id).execute()
    except Exception:
        pass


class CEOChatMessage(BaseModel):
    session_id: str
    message: str
    tenant_id: str = ""


@app.post("/api/ceo/chat")
async def ceo_chat(body: CEOChatMessage):
    """Send a message to the CEO agent. The CEO reads its own .md file and all sub-agent .md files,
    then responds and may delegate tasks to sub-agents."""
    from backend.tools.claude_cli import call_claude
    import json as _json

    _evict_chat_sessions()
    session = _chat_sessions.setdefault(body.session_id, [])
    is_first_message = len(session) == 0
    session.append({"role": "user", "content": body.message})

    # Persist user message to DB
    tenant_id = body.tenant_id
    _save_chat_message(body.session_id, tenant_id, "user", body.message)
    if is_first_message:
        _auto_title(body.session_id, body.message)

    # CEO is now in a meeting (processing the user's message)
    if tenant_id:
        await _emit_agent_status(tenant_id, "ceo", "running",
                                 current_task="In meeting with user",
                                 action="meeting_with_user")

    # Include sub-agent docs only on first message (cached via prompt caching after that)
    # Truncate each to 200 chars to save tokens — the CEO just needs to know capabilities
    sub_agent_context = "\n".join(
        f"- {name}: {content[:200].replace(chr(10), ' ')}"
        for name, content in _AGENT_MDS.items()
        if name != "ceo" and not name.startswith("skill_")
    )

    # Load tenant config once — reused for business context + integration checks
    business_context = ""
    tc = None
    tenant_id = body.tenant_id
    if tenant_id:
        try:
            tc = get_tenant_config(tenant_id)
            if tc.agent_brief:
                # ~150 tokens (pre-generated compact summary)
                business_context = f"\n## Business Context\n{tc.agent_brief}\nPositioning: {tc.gtm_playbook.positioning}\nChannels: {', '.join(tc.channels)}\n"
            else:
                # Fallback — compact fields only
                business_context = f"""
## Business Context
{tc.business_name}: {tc.product.name} — {tc.product.description}
Audience: {', '.join(tc.icp.target_titles) if tc.icp.target_titles else 'N/A'}
Positioning: {tc.gtm_playbook.positioning}
Voice: {tc.brand_voice.tone}
Channels: {', '.join(tc.channels)}
"""
        except Exception:
            pass

    # Check connected integrations for this tenant (reuse tc from above)
    integration_notes = ""
    if tenant_id and tc:
        try:
            _gmail_connected = bool(
                tc.integrations.google_access_token or tc.integrations.google_refresh_token
            )
            if _gmail_connected:
                integration_notes += f"""
5. **Gmail is connected** ({tc.owner_email}). When the user asks you to SEND an email,
   delegate to email_marketer with a task starting with "SEND:" including the recipient email.
   IMPORTANT: Always include the recipient's full email address in the task description."""

            _twitter_connected = bool(tc.integrations.twitter_access_token or tc.integrations.twitter_refresh_token)
            if _twitter_connected:
                integration_notes += f"""
6. **X/Twitter is connected** (@{tc.integrations.twitter_username or 'user'}). When the user asks to post on social media:
   - Delegate to social_manager with task like "Adapt latest content for social media"
   - Social Manager fetches the latest Content Writer output and creates platform-specific posts
   - Posts go to Inbox for user approval — NEVER auto-publish without approval"""
        except Exception:
            pass

    # ── CRM context injection (only when message references contacts/deals/companies) ──
    crm_context = ""
    _crm_keywords = ["contact", "contacts", "company", "companies", "deal", "deals", "pipeline",
                      "lead", "leads", "prospect", "customer", "crm", "send email to", "reach out to",
                      "follow up with", "who", "client", "clients"]
    _msg_lower = body.message.lower()
    if tenant_id and any(kw in _msg_lower for kw in _crm_keywords):
        try:
            _crm_sb = _get_supabase()
            # Fetch compact summaries — minimal tokens
            _contacts = _crm_sb.table("crm_contacts").select("name,email,status,company_id").eq(
                "tenant_id", tenant_id
            ).order("created_at", desc=True).limit(20).execute()
            _deals = _crm_sb.table("crm_deals").select("title,value,stage").eq(
                "tenant_id", tenant_id
            ).order("created_at", desc=True).limit(10).execute()

            if _contacts.data:
                _contact_lines = [f"  - {c['name']} ({c['email'] or 'no email'}) [{c['status']}]" for c in _contacts.data]
                crm_context += "\n## CRM Contacts (" + str(len(_contacts.data)) + ")\n" + "\n".join(_contact_lines)
            if _deals.data:
                _deal_lines = [f"  - {d['title']} — ${d['value']} [{d['stage']}]" for d in _deals.data]
                crm_context += "\n## CRM Deals (" + str(len(_deals.data)) + ")\n" + "\n".join(_deal_lines)
            if crm_context:
                crm_context += "\nUse this CRM data to give specific advice. Reference contacts/deals by name when relevant."
        except Exception:
            pass

    system_prompt = f"""{_CEO_MD}
{business_context}{crm_context}
## Sub-Agent Documentation
{sub_agent_context}

## Instructions
You are chatting with a developer founder who needs marketing help.
You already know their business from the onboarding data above — use it to give specific, personalized advice.
If CRM data is provided above, you can reference it for context. But when the user explicitly asks to LIST or SHOW contacts, companies, or deals, ALWAYS use the corresponding read action block (read_contacts, read_companies, read_deals) so the full formatted data is returned to them.

CRITICAL RULE — DO NOT AUTO-DELEGATE OR AUTO-ACT:
- ONLY perform actions or delegate tasks when the user EXPLICITLY asks you to.
- If the user says "create a contact", ONLY create the contact. Do NOT also send an email, create content, or delegate to other agents unless they asked.
- If the user says "send an email to X", THEN delegate to email_marketer.
- NEVER assume what the user wants beyond what they literally said.
- When in doubt, ASK the user what they want to do next. Do not take initiative.
- Each message should do ONE thing — the thing the user asked for.

Based on the conversation:
1. Answer their question or provide strategic guidance
2. ONLY if the user explicitly asks to create content, send emails, post on social, etc., then delegate:
   ```delegate
   {{"agent": "content_writer|email_marketer|social_manager|ad_strategist", "task": "description of what to do", "priority": "low|medium|high", "status": "backlog|to_do|in_progress|done"}}
   ```
   Status choices:
   - "backlog" — nice-to-have, no immediate action needed
   - "to_do" — should be done soon, queued for the agent
   - "in_progress" — starting immediately
   - "done" — already completed in this response
3. If no delegation is needed, just respond normally — do NOT force a delegation
{integration_notes}

## CEO Business Actions
You can execute business operations directly when the user asks. Include an action block:
```action
{{"action": "action_name", "params": {{"key": "value"}}}}
```

Available actions:
{_get_ceo_action_descriptions()}

RULES FOR ACTIONS:
- ONLY execute actions the user explicitly requested. Do NOT chain actions or auto-add extra actions.
- UPDATE and DELETE actions ALWAYS require user confirmation — include the action block and ask the user to confirm.
- CREATE actions can proceed directly if the user's intent is clear.
- PUBLISH and SEND actions always require confirmation.
- If data is missing, ask for it before creating the action block.
- For ALL actions: the result will be automatically formatted and appended to your response. Just write a brief intro (e.g., "Here are your contacts:" or "Done, I've created the contact:") and include the action block — the system will append the formatted result below your message. Do NOT fabricate results yourself.
- If the user asks you to modify code, backend, prompts, database schema, or infrastructure — REFUSE.
- Never bypass confirmations or approval flows.

IMPORTANT — Token efficiency rules:
- If the user asks to send/post content that ALREADY EXISTS in the Inbox, do NOT regenerate it. Reference the existing content and delegate with "USE EXISTING:" prefix.
- Only delegate to content_writer or email_marketer for NEW content when the user asks.
- Never auto-publish. All content goes to Inbox for user approval first.

Keep responses concise and actionable. You are their Chief Marketing Strategist."""

    # Build conversation for Claude — compact old messages, keep recent ones full
    _RECENT_WINDOW = 6  # keep last 6 messages in full
    _MAX_SUMMARY_MSGS = 20  # max older messages to summarize

    if len(session) <= _RECENT_WINDOW:
        # Short conversation — send everything
        conversation = "\n".join(
            f"{'User' if m['role'] == 'user' else 'CEO'}: {m['content']}"
            for m in session
        )
    else:
        # Compact older messages into a summary, keep recent ones full
        older = session[:-_RECENT_WINDOW][-_MAX_SUMMARY_MSGS:]
        recent = session[-_RECENT_WINDOW:]

        # Build compact summary of older messages (key points only)
        summary_lines = []
        for m in older:
            role = "User" if m["role"] == "user" else "CEO"
            # Truncate each old message to first 100 chars
            text = m["content"][:100].replace("\n", " ")
            if len(m["content"]) > 100:
                text += "..."
            summary_lines.append(f"- {role}: {text}")

        summary = "EARLIER IN THIS CHAT (summary):\n" + "\n".join(summary_lines)
        recent_text = "\n".join(
            f"{'User' if m['role'] == 'user' else 'CEO'}: {m['content']}"
            for m in recent
        )
        conversation = f"{summary}\n\nRECENT MESSAGES:\n{recent_text}"

    try:
        raw = await call_claude(system_prompt, conversation, tenant_id=tenant_id or "global", agent_id="ceo")
    except Exception as exc:
        import traceback
        logger = logging.getLogger("aria.ceo_chat")
        logger.error(f"CEO chat error: {exc}\n{traceback.format_exc()}")
        raw = f"I encountered an error: {str(exc)[:200]}. Please try again."

    # Check for forbidden requests
    from backend.ceo_actions import is_forbidden_request, REFUSAL_MESSAGE
    if is_forbidden_request(body.message):
        # The CEO should already refuse in its response, but double-check
        if "can't" not in raw.lower() and "cannot" not in raw.lower() and "don't have access" not in raw.lower():
            raw = REFUSAL_MESSAGE

    # Parse delegation blocks
    delegations = []
    clean_response = raw
    if "```delegate" in raw:
        import re
        blocks = re.findall(r"```delegate\s*\n(.*?)\n```", raw, re.DOTALL)
        for block in blocks:
            try:
                d = _json.loads(block.strip())
                if d.get("agent") in ("content_writer", "email_marketer", "social_manager", "ad_strategist"):
                    delegations.append(d)
            except _json.JSONDecodeError:
                pass
        clean_response = re.sub(r"```delegate\s*\n.*?\n```", "", raw, flags=re.DOTALL).strip()

    # Parse CEO action blocks
    ceo_actions = []
    if "```action" in clean_response:
        import re
        action_blocks = re.findall(r"```action\s*\n(.*?)\n```", clean_response, re.DOTALL)
        for block in action_blocks:
            try:
                a = _json.loads(block.strip())
                if a.get("action"):
                    ceo_actions.append(a)
            except _json.JSONDecodeError:
                pass
        clean_response = re.sub(r"```action\s*\n.*?\n```", "", clean_response, flags=re.DOTALL).strip()

    # Execute non-confirmation actions immediately; queue confirmations for frontend
    action_results = []
    pending_confirmations = []
    if ceo_actions and tenant_id:
        from backend.ceo_actions import execute_action, ACTION_REGISTRY
        for a in ceo_actions:
            action_name = a.get("action", "")
            params = a.get("params", {})
            result = await execute_action(tenant_id, action_name, params, confirmed=False)
            if result["status"] == "needs_confirmation":
                pending_confirmations.append(result)
            elif result["status"] == "executed":
                action_results.append(result)
            else:
                action_results.append(result)

    # Append formatted action results to the response so data appears in chat
    for ar in action_results:
        if ar.get("status") not in ("executed", "error"):
            continue
        action_name = ar.get("action", "")
        data = ar.get("result", {}) if ar["status"] == "executed" else {"error": ar.get("message", "Unknown error")}
        formatted = _format_action_result(action_name, data)
        if formatted:
            clean_response = clean_response.rstrip() + "\n\n" + formatted

    session.append({"role": "assistant", "content": clean_response})

    # Persist assistant message to DB
    _save_chat_message(body.session_id, tenant_id, "assistant", clean_response, delegations)

    # No delegations — CEO meeting is over, return to idle
    if not delegations and tenant_id:
        await _emit_agent_status(tenant_id, "ceo", "idle",
                                 action="chat_response_sent")

    # Save delegations as tasks, emit status events, and execute in background
    saved_tasks = []
    for d in delegations:
        agent_id = d["agent"]
        task_desc = d.get("task", "")

        # Save to Supabase tasks table — always start as in_progress
        if tenant_id:
            try:
                sb = _get_supabase()
                task_row = {
                    "tenant_id": tenant_id,
                    "agent": agent_id,
                    "task": task_desc,
                    "priority": d.get("priority", "medium"),
                    "status": "in_progress",
                }
                result = sb.table("tasks").insert(task_row).execute()
                if result.data:
                    saved_tasks.append(result.data[0])
                    # Notify Kanban board of new task
                    await sio.emit("task_updated", {
                        "id": result.data[0]["id"],
                        "agent": agent_id,
                        "status": "in_progress",
                        "task": task_desc,
                    }, room=tenant_id)
            except Exception:
                pass

        # Emit agent_status_change: CEO walks to meeting room, then agent does
        if tenant_id:
            # CEO starts moving to meeting room
            await _emit_agent_status(tenant_id, "ceo", "running",
                                     current_task=f"Briefing {agent_id} on: {task_desc[:60]}",
                                     action="walk_to_meeting")
            # Target agent starts moving to meeting room
            await _emit_agent_status(tenant_id, agent_id, "running",
                                     current_task=task_desc,
                                     action="walk_to_meeting")
        # Execute agent — route through Paperclip if connected, else local fallback
        try:
            from backend.orchestrator import dispatch_agent
            from backend.paperclip_sync import is_connected
            import asyncio as _aio

            if is_connected():
                # Paperclip-first: create issue + trigger heartbeat
                _aio.create_task(dispatch_agent(tenant_id, agent_id, context={
                    "task": task_desc,
                    "priority": d.get("priority", "medium"),
                    "session_id": body.session_id,
                }))
            else:
                # Local fallback
                from backend.agents import AGENT_REGISTRY
                agent_module = AGENT_REGISTRY.get(agent_id)
                if agent_module:
                    _aio.create_task(_run_agent_to_inbox(
                        agent_module, agent_id, tenant_id, task_desc,
                        body.session_id,
                        saved_tasks[-1]["id"] if saved_tasks else None,
                        d.get("priority", "medium"),
                    ))
        except Exception:
            pass

    response_data = {
        "response": clean_response,
        "delegations": delegations,
        "tasks": saved_tasks,
        "session_id": body.session_id,
    }

    # Include action results and pending confirmations
    if action_results:
        response_data["action_results"] = action_results
    if pending_confirmations:
        response_data["pending_confirmations"] = pending_confirmations

    return response_data


@app.get("/api/ceo/chat/{session_id}/history")
async def ceo_chat_history(session_id: str):
    """Get chat history for a session — loads from DB."""
    # Check in-memory cache first
    if session_id in _chat_sessions and _chat_sessions[session_id]:
        return {"session_id": session_id, "messages": _chat_sessions[session_id]}
    # Load from DB
    try:
        sb = _get_supabase()
        result = sb.table("chat_messages").select("role,content,delegations").eq("session_id", session_id).order("created_at").execute()
        messages = [{"role": r["role"], "content": r["content"], "delegations": r.get("delegations", [])} for r in result.data]
        if messages:
            _chat_sessions[session_id] = messages
        return {"session_id": session_id, "messages": messages}
    except Exception:
        return {"session_id": session_id, "messages": []}


@app.get("/api/ceo/chat/sessions/{tenant_id}")
async def list_chat_sessions(tenant_id: str):
    """List all chat sessions for a tenant, newest first."""
    try:
        sb = _get_supabase()
        result = sb.table("chat_sessions").select("id,title,created_at,updated_at").eq("tenant_id", tenant_id).order("updated_at", desc=True).execute()
        return {"sessions": result.data}
    except Exception:
        return {"sessions": []}


# ─── Project Tasks API ───
@app.get("/api/tasks/{tenant_id}")
async def list_tasks(tenant_id: str):
    """List all tasks for a tenant, ordered by creation date."""
    try:
        sb = _get_supabase()
        result = sb.table("tasks").select("*").eq("tenant_id", tenant_id).order("created_at", desc=True).execute()
        return {"tasks": result.data}
    except Exception as e:
        return {"tasks": [], "error": str(e)}


class TaskUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: str, body: TaskUpdate):
    """Update a task's status or priority. Syncs agent visual status in Virtual Office."""
    sb = _get_supabase()
    updates = {}
    if body.status:
        updates["status"] = body.status
    if body.priority:
        updates["priority"] = body.priority
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Fetch task details before updating (for status sync)
    task_result = sb.table("tasks").select("agent,tenant_id,task").eq("id", task_id).execute()

    sb.table("tasks").update(updates).eq("id", task_id).execute()

    # Sync agent visual status with task status change
    if body.status and task_result.data:
        task = task_result.data[0]
        agent_id = task["agent"]
        tid = task["tenant_id"]
        if body.status == "in_progress":
            await _emit_agent_status(tid, agent_id, "working",
                                     current_task=task.get("task", ""),
                                     action="task_started")
        elif body.status in ("done", "to_do", "backlog"):
            # Only go idle if agent has no OTHER in_progress tasks
            other = sb.table("tasks").select("id").eq(
                "tenant_id", tid
            ).eq("agent", agent_id).eq("status", "in_progress").neq(
                "id", task_id
            ).limit(1).execute()
            if not other.data:
                await _emit_agent_status(tid, agent_id, "idle",
                                         action="task_status_changed")

    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    """Delete a task. If it was in_progress, sync agent back to idle."""
    sb = _get_supabase()

    # Fetch before deleting for status sync
    task_result = sb.table("tasks").select("agent,tenant_id,status").eq("id", task_id).execute()

    sb.table("tasks").delete().eq("id", task_id).execute()

    # If deleted task was in_progress, check if agent has other active tasks
    if task_result.data and task_result.data[0].get("status") == "in_progress":
        task = task_result.data[0]
        agent_id = task["agent"]
        tid = task["tenant_id"]
        other = sb.table("tasks").select("id").eq(
            "tenant_id", tid
        ).eq("agent", agent_id).eq("status", "in_progress").limit(1).execute()
        if not other.data:
            await _emit_agent_status(tid, agent_id, "idle",
                                     action="task_deleted")

    return {"ok": True}


# ─── WebSocket for real-time chat ───
@app.websocket("/ws/chat/{tenant_id}")
async def websocket_chat(websocket: WebSocket, tenant_id: str):
    await websocket.accept()
    await sio.enter_room(websocket.client, tenant_id)
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_json({"type": "message", "content": f"Received: {data}"})
    except WebSocketDisconnect:
        pass


# ─── API Usage tracking endpoint ───
@app.get("/api/usage")
async def api_usage(tenant_id: str = "global"):
    """Return current API usage stats (tokens, requests) for a tenant."""
    from backend.tools.claude_cli import get_usage
    return get_usage(tenant_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.server:socket_app", host="0.0.0.0", port=8000, reload=True)
