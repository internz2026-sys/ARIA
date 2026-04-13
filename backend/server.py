"""ARIA FastAPI Server — webhooks, chat, agent management, dashboard API."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import socketio
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
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
    get_paperclip_agent_id,
    handle_webhook,
    pause_agent_paperclip,
    resume_agent_paperclip,
    run_scheduled_agents,
    _sanitize_error_message,
    _urllib_request,
    PaperclipUnreachable,
)
from backend.orchestrator import is_connected as paperclip_connected
from backend.agents import AGENT_REGISTRY
from backend.services.paperclip_chat import normalize_comments, pick_agent_output
from backend.paperclip_office_sync import (
    _is_finished,
    _is_failed,
    _add_processed,
    poll_completed_issues,
    sync_agent_statuses,
)

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


async def _paperclip_office_sync_loop():
    """Background loop: import completed Paperclip issues to inbox + sync Virtual Office.

    Adaptive backoff: starts at 5s for responsive updates, backs off
    to 30s after 6 consecutive empty cycles (~30s of nothing happening),
    snaps back to 5s the moment any actual work is detected. This
    cuts 70-80% of Paperclip API calls during idle hours (overnight,
    weekends) without sacrificing responsiveness when activity is
    happening.

    Activity signals that reset the interval to 5s:
      - poll_completed_issues imported a new inbox row
      - sync_agent_statuses observed an agent state change
      - Any tick where Paperclip wasn't connected (so we don't get
        stuck on the long interval if Paperclip restarts)

    poke_paperclip_poller() (defined below) lets the chat handler
    and inbox routes manually reset the interval when the user does
    something so the next tick is fast even if we were in idle mode.
    """
    _log = logging.getLogger("aria.paperclip_office_sync")

    FAST_INTERVAL = 5
    SLOW_INTERVAL = 30
    EMPTY_CYCLES_BEFORE_BACKOFF = 6

    interval = FAST_INTERVAL
    empty_streak = 0

    while True:
        # If something poked us, jump straight to fast mode immediately
        if _paperclip_poller_poke.is_set():
            _paperclip_poller_poke.clear()
            interval = FAST_INTERVAL
            empty_streak = 0

        await asyncio.sleep(interval)
        try:
            if not paperclip_connected():
                interval = FAST_INTERVAL  # always fast when reconnecting
                empty_streak = 0
                continue
            imported = await poll_completed_issues()
            status_changed = await sync_agent_statuses(sio)
            did_work = bool(imported) or bool(status_changed)
            if did_work:
                empty_streak = 0
                interval = FAST_INTERVAL
            else:
                empty_streak += 1
                if empty_streak >= EMPTY_CYCLES_BEFORE_BACKOFF:
                    interval = SLOW_INTERVAL
        except Exception as e:
            _log.warning("Paperclip office sync failed: %s", e)


# Event used to "poke" the paperclip poller from anywhere in the codebase
# (e.g. after a chat send or inbox write) so the next tick fires fast even
# if the loop was in 30s idle mode. Single-process; for multi-worker
# deployments this would need to broadcast across processes.
_paperclip_poller_poke = asyncio.Event()


def poke_paperclip_poller() -> None:
    """Wake the Paperclip office-sync loop on the next iteration.
    Call this whenever the user does something that should cause the
    poller to look for new state right away (e.g. CEO chat dispatched
    a sub-agent, inbox row updated, etc).
    """
    try:
        _paperclip_poller_poke.set()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: initialize background loops + integrations.

    NOTE: We used to call `await paperclip_init()` here to auto-register
    agents with Paperclip on every restart. That was deleted along with
    backend/paperclip_sync.py — agents are now configured directly in
    Paperclip's UI and ARIA only does runtime lookups via the helpers in
    backend/orchestrator.py.
    """
    # Proactive Claude CLI auth health check. The CLI's config-rotation
    # race can leave ~/.claude.json missing on container startup (only the
    # backup survives), and the reactive auto-heal in call_claude only
    # fires after the first failed request. Run the restore here so the
    # very first chat call after a rebuild is guaranteed to work.
    try:
        from backend.tools.claude_cli import _try_restore_claude_config
        if _try_restore_claude_config():
            logger.warning("Startup: auto-restored ~/.claude.json from backup")
        else:
            logger.info("Startup: ~/.claude.json is healthy (no restore needed)")
    except Exception as e:
        logger.warning("Startup .claude.json check failed: %s", e)

    # Initialize semantic cache (Qdrant)
    try:
        from backend.services.semantic_cache import ensure_collection
        ensure_collection()
        logger.info("Semantic cache (Qdrant) initialized")
    except Exception as e:
        logger.warning("Qdrant not available — semantic caching disabled: %s", e)
    sync_task = asyncio.create_task(_gmail_sync_loop())
    scheduler_task = asyncio.create_task(_scheduler_executor_loop())
    office_sync_task = asyncio.create_task(_paperclip_office_sync_loop())
    yield
    sync_task.cancel()
    scheduler_task.cancel()
    office_sync_task.cancel()
    # Close the shared Paperclip httpx client so we don't leak connections
    # on graceful shutdown (uvicorn reload during dev, container stop in prod).
    try:
        from backend.orchestrator import close_httpx_client
        await close_httpx_client()
    except Exception as e:
        logger.warning("Failed to close orchestrator httpx client: %s", e)


app = FastAPI(title="ARIA API", version="1.0.0", lifespan=lifespan)

# ── Register routers ──────────────────────────────────────────────────────
from backend.routers.crm import router as crm_router
from backend.routers.inbox import router as inbox_router
from backend.routers.campaigns import router as campaigns_router
# NOTE: backend/routers/paperclip.py was a webhook receiver for the HTTP
# adapter experiment — we reverted to claude_local, so Paperclip never
# calls our webhook anymore. The agents now POST results back to ARIA via
# the aria-backend-api skill (which curls /api/inbox/{tenant}/items).

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
    "/api/onboarding/save-draft",
    "/api/onboarding/draft",
    "/api/whatsapp/webhook",
    "/api/cron/run-scheduled",
}

_PUBLIC_PREFIXES = (
    "/api/auth/",           # OAuth callbacks (Twitter, LinkedIn)
    "/api/webhooks/",       # External webhooks (Stripe, SendGrid)
    "/api/inbox/",          # Inbox item creation (used by Paperclip agents)
    "/api/media/",          # Image generation (used by Paperclip Media Designer)
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

# In-memory live status store + persisted to Supabase. Bounded so a busy
# multi-tenant deployment can't grow this dict unbounded — eviction is by
# insertion order (oldest tenant out).
_live_agent_status: dict[str, dict[str, dict]] = {}
_LIVE_STATUS_MAX_TENANTS = 1000


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
    if tenant_id not in _live_agent_status and len(_live_agent_status) >= _LIVE_STATUS_MAX_TENANTS:
        # Evict oldest entry by insertion order to keep the dict bounded.
        oldest = next(iter(_live_agent_status), None)
        if oldest is not None:
            _live_agent_status.pop(oldest, None)
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
    {"agent_id": "media", "name": "Media Designer", "role": "Visual Content Creator", "model": "haiku-4-5", "department": "design-studio"},
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
        now = datetime.now(timezone.utc)
        start = start or (now - timedelta(days=7)).isoformat()
        end = end or (now + timedelta(days=60)).isoformat()
    return {"tasks": scheduler_service.calendar_tasks(tenant_id, start, end)}


@app.get("/api/calendar/{tenant_id}/activity")
async def get_calendar_activity(tenant_id: str, start: str = "", end: str = ""):
    """Unified marketing activity feed for the calendar view.

    Returns events from multiple sources (scheduled tasks, inbox drafts,
    sent items) in a single normalized event shape, so the calendar
    becomes a 'marketing activity dashboard' instead of a 'things I
    explicitly queued' calendar.

    Each event has:
      - id: stable id (uuid or composite)
      - source: 'scheduled' | 'inbox_draft' | 'inbox_sent' | 'agent_run'
      - title: short display label
      - timestamp: ISO datetime to anchor on the calendar
      - status: optional status badge
      - agent: optional agent slug for color/icon
      - href: optional deep-link target inside ARIA
      - metadata: source-specific extras
    """
    sb = _get_supabase()
    if not start or not end:
        now = datetime.now(timezone.utc)
        start = start or (now - timedelta(days=30)).isoformat()
        end = end or (now + timedelta(days=60)).isoformat()

    events: list[dict] = []

    # 1. Scheduled tasks (existing source)
    try:
        tasks = scheduler_service.calendar_tasks(tenant_id, start, end)
        for t in tasks:
            tt = t.get("task_type", "")
            href = "/calendar"
            payload = t.get("payload") or {}
            inbox_id = payload.get("inbox_item_id")
            if inbox_id:
                href = f"/inbox?id={inbox_id}"
            events.append({
                "id": f"scheduled:{t.get('id')}",
                "source": "scheduled",
                "task_type": tt,
                "title": t.get("title") or tt,
                "timestamp": t.get("scheduled_at"),
                "status": t.get("status", ""),
                "approval_status": t.get("approval_status", ""),
                "href": href,
                "metadata": {
                    "timezone": t.get("timezone"),
                    "created_by": t.get("created_by"),
                    "raw_id": t.get("id"),
                },
            })
    except Exception as e:
        logger.warning("[calendar-activity] scheduled fetch failed: %s", e)

    # 2. Inbox items (drafts + sent). Drafts use created_at, sent items
    #    use updated_at when status is sent/published. Both within the
    #    requested date range. This is what makes the calendar useful
    #    even when nothing is explicitly scheduled.
    try:
        inbox_rows = (
            sb.table("inbox_items")
            .select("id,title,agent,type,status,created_at,updated_at")
            .eq("tenant_id", tenant_id)
            .gte("created_at", start)
            .lte("created_at", end)
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        )
        for row in (inbox_rows.data or []):
            status = row.get("status") or ""
            is_sent = status in ("sent", "published", "completed")
            # Choose timestamp: when it was sent (if applicable) or when
            # it was created (if it's still a draft / pending)
            ts = row.get("updated_at") if is_sent and row.get("updated_at") else row.get("created_at")
            events.append({
                "id": f"inbox:{row.get('id')}",
                "source": "inbox_sent" if is_sent else "inbox_draft",
                "task_type": row.get("type", ""),
                "title": row.get("title", "Inbox item"),
                "timestamp": ts,
                "status": status,
                "agent": row.get("agent", ""),
                "href": f"/inbox?id={row.get('id')}",
                "metadata": {
                    "raw_id": row.get("id"),
                    "type": row.get("type"),
                },
            })
    except Exception as e:
        logger.warning("[calendar-activity] inbox fetch failed: %s", e)

    # Sort by timestamp ascending so the calendar can render in chrono order
    events.sort(key=lambda e: (e.get("timestamp") or ""))

    return {
        "tenant_id": tenant_id,
        "start": start,
        "end": end,
        "events": events,
        "counts": {
            "total": len(events),
            "scheduled": sum(1 for e in events if e["source"] == "scheduled"),
            "inbox_draft": sum(1 for e in events if e["source"] == "inbox_draft"),
            "inbox_sent": sum(1 for e in events if e["source"] == "inbox_sent"),
        },
    }


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


# ─── Onboarding Draft Persistence ───
# Server-side persistence of in-progress onboarding state, keyed on the
# Supabase auth user_id. Solves the bug where users who clear localStorage,
# open onboarding in a second tab, hard-refresh after a long idle, or
# switch browsers would lose their 10-min CEO conversation and have to
# restart from scratch.
#
# Frontend flow:
#   1. /review extracts the config -> POST /api/onboarding/save-draft
#      to mirror it to the DB
#   2. /select-agents tries GET /api/onboarding/draft on mount BEFORE
#      reading localStorage; falls back to localStorage only if the API
#      returns empty
#   3. After successful /save-config, frontend calls DELETE to clean up

class OnboardingDraftPayload(BaseModel):
    user_id: str
    session_id: str | None = None
    extracted_config: dict
    skipped_topics: list | None = None
    conversation_history: list | None = None


@app.post("/api/onboarding/save-draft")
async def save_onboarding_draft(body: OnboardingDraftPayload):
    """Upsert the user's in-progress onboarding draft.

    Public (no JWT required) because the user is mid-onboarding and may
    not have a tenant yet -- but the user_id MUST come from the
    authenticated Supabase session on the client side. This endpoint
    just trusts that and writes the row.
    """
    try:
        sb = _get_supabase()
        row = {
            "user_id": body.user_id,
            "session_id": body.session_id,
            "extracted_config": body.extracted_config,
            "skipped_topics": body.skipped_topics,
            "conversation_history": body.conversation_history,
        }
        # Upsert on user_id so we always have at most one in-progress
        # draft per user. The trigger updates updated_at automatically.
        sb.table("onboarding_drafts").upsert(row, on_conflict="user_id").execute()
        return {"saved": True}
    except Exception as e:
        logger.warning("Failed to save onboarding draft: %s", e)
        return {"saved": False, "error": str(e)[:200]}


@app.get("/api/onboarding/draft")
async def get_onboarding_draft(user_id: str):
    """Return the user's most recent in-progress onboarding draft, or 404
    if none exists. Used by /select-agents on mount before falling back
    to localStorage."""
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    try:
        sb = _get_supabase()
        result = (
            sb.table("onboarding_drafts")
            .select("session_id,extracted_config,skipped_topics,conversation_history,updated_at")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="No draft found")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Failed to load onboarding draft: %s", e)
        raise HTTPException(status_code=500, detail="Could not load draft")


@app.delete("/api/onboarding/draft")
async def delete_onboarding_draft(user_id: str):
    """Clean up the user's draft after successful save-config. Best-effort:
    if the delete fails the row will just expire naturally over time."""
    try:
        sb = _get_supabase()
        sb.table("onboarding_drafts").delete().eq("user_id", user_id).execute()
        return {"deleted": True}
    except Exception as e:
        logger.warning("Failed to delete onboarding draft: %s", e)
        return {"deleted": False}


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


@app.post("/api/media/{tenant_id}/generate")
async def generate_media_image(tenant_id: str, payload: dict = Body(default={})):
    """Direct image-generation endpoint for the Paperclip Media Designer agent.

    Bypasses Paperclip dispatch and calls media_agent.run() locally so the agent
    actually produces a real PNG via Pollinations -> Supabase Storage -> inbox.
    Public (no JWT) so the Paperclip-spawned Claude CLI can curl it from inside
    the container — same pattern as /api/inbox/.
    """
    from backend.agents import media_agent

    prompt = (payload or {}).get("prompt", "")
    if not prompt:
        return {"status": "failed", "error": "prompt is required"}

    result = await media_agent.run(tenant_id, {"prompt": prompt})
    inbox_row = (result or {}).get("inbox_item") if isinstance(result, dict) else None

    # Kill the watcher's "Media is working on..." placeholder so the user
    # sees ONE row that transitions processing -> ready, not a stale
    # placeholder lingering next to the finished image row.
    if inbox_row and tenant_id:
        await _cleanup_media_placeholder(tenant_id, inbox_row.get("id"))

        # Push the finished row to the UI in real time
        try:
            await sio.emit("inbox_new_item", {
                "id": inbox_row.get("id"),
                "agent": "media",
                "type": inbox_row.get("type", "image"),
                "title": inbox_row.get("title", ""),
                "status": inbox_row.get("status", "ready"),
                "priority": inbox_row.get("priority", "medium"),
                "created_at": inbox_row.get("created_at", ""),
            }, room=tenant_id)
        except Exception:
            pass

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
    """Real KPI counts from inbox_items + scheduled_tasks.

    All four queries run concurrently via asyncio.gather + to_thread,
    instead of the previous sequential blocking pattern. Each
    sb.table(...).execute() is a sync HTTP round-trip that blocks the
    event loop, so wrapping them in to_thread frees the loop AND
    gather lets them fly in parallel. ~4x faster dashboard load
    (200-800ms -> 50-200ms typical).
    """
    sb = _get_supabase()
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat()
    two_weeks_ago = (now - timedelta(days=14)).isoformat()

    _content_types = ("blog_post", "email_sequence", "social_post", "ad_campaign", "email", "blog", "social")
    _published_statuses = ("ready", "needs_review", "draft_pending_approval", "sent", "completed")

    # Each thread-wrapped lambda owns one query. Errors are swallowed
    # and produce a sentinel so a single failed query doesn't tank the
    # whole dashboard render.
    def _q_content():
        try:
            return sb.table("inbox_items").select("id,type,status,created_at", count="exact") \
                .eq("tenant_id", tenant_id) \
                .in_("type", list(_content_types)) \
                .in_("status", list(_published_statuses)) \
                .execute()
        except Exception as e:
            logger.warning("[dashboard-stats] content query failed: %s", e)
            return None

    def _q_sent_emails():
        try:
            return sb.table("inbox_items").select("id", count="exact") \
                .eq("tenant_id", tenant_id) \
                .in_("type", ("email_sequence", "email")) \
                .eq("status", "sent") \
                .execute()
        except Exception:
            return None

    def _q_social():
        try:
            return sb.table("inbox_items").select("id,created_at", count="exact") \
                .eq("tenant_id", tenant_id) \
                .in_("type", ("social_post", "social")) \
                .in_("status", ("sent", "ready", "completed")) \
                .execute()
        except Exception:
            return None

    def _q_ad_spend():
        try:
            return sb.table("campaigns").select("budget_spent").eq("tenant_id", tenant_id).execute()
        except Exception:
            return None  # campaigns table may not exist yet

    # Run all four queries concurrently. Each one stalls a thread, but
    # the asyncio event loop is free to handle other requests.
    content_res, sent_res, social_res, ad_res = await asyncio.gather(
        asyncio.to_thread(_q_content),
        asyncio.to_thread(_q_sent_emails),
        asyncio.to_thread(_q_social),
        asyncio.to_thread(_q_ad_spend),
    )

    # Content Published — total + 7d delta vs previous 7d
    if content_res is not None:
        all_rows = content_res.data or []
        content_total = content_res.count if content_res.count is not None else len(all_rows)
        content_this_week = sum(1 for r in all_rows if r.get("created_at", "") >= week_ago)
        content_prev_week = sum(1 for r in all_rows if two_weeks_ago <= r.get("created_at", "") < week_ago)
        content_delta = content_this_week - content_prev_week
        content_delta_pct = int((content_delta / content_prev_week) * 100) if content_prev_week > 0 else 0
    else:
        content_total = content_delta = content_delta_pct = 0

    # Emails Sent — count only
    if sent_res is not None:
        emails_sent_count = sent_res.count if sent_res.count is not None else len(sent_res.data or [])
    else:
        emails_sent_count = 0

    # Social Engagement — count + 7d delta
    if social_res is not None:
        social_rows = social_res.data or []
        social_count = social_res.count if social_res.count is not None else len(social_rows)
        social_this_week = sum(1 for r in social_rows if r.get("created_at", "") >= week_ago)
        social_prev_week = sum(1 for r in social_rows if two_weeks_ago <= r.get("created_at", "") < week_ago)
        social_delta_pct = int(((social_this_week - social_prev_week) / social_prev_week) * 100) if social_prev_week > 0 else 0
    else:
        social_count = social_delta_pct = 0

    # Ad Spend — sum across campaigns
    ad_spend_value = sum((r.get("budget_spent") or 0) for r in (ad_res.data or [])) if ad_res is not None else 0

    return {
        "tenant_id": tenant_id,
        "kpis": {
            "content_published": {
                "value": content_total,
                "delta": content_delta,
                "delta_pct": content_delta_pct,
            },
            "emails_sent": {
                "value": emails_sent_count,
                "open_rate": 0,    # placeholder until we wire Gmail tracking
                "click_rate": 0,
            },
            "social_engagement": {
                "value": social_count,
                "delta_pct": social_delta_pct,
            },
            "ad_spend": {
                "value": ad_spend_value,
                "roas": 0,
            },
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
    from backend.orchestrator import AGENT_API_KEYS, get_company_id
    return {
        "connected": paperclip_connected(),
        "company_id": get_company_id(),
        "agents_registered": sum(1 for k in AGENT_API_KEYS.values() if k),
        "url": os.environ.get("PAPERCLIP_API_URL", "http://127.0.0.1:3100"),
    }


# Note: /api/paperclip/heartbeat/{agent_name} now lives in
# backend/routers/paperclip.py and is registered via app.include_router above.


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

def _parse_codeblock_json(block: str, kind: str) -> dict | None:
    """Parse a ```delegate or ```action JSON block, with recovery for the
    most common LLM mistakes.

    Returns the parsed dict, or None if parsing fails after recovery.
    Logs the failure with the original block for debugging. Was previously
    a bare json.loads in two places that silently dropped malformed
    delegations -- the CEO would promise delegation in prose but nothing
    fired, and the user saw text but no inbox row.
    """
    import json as _json_inner
    import re as _re_inner
    raw = block.strip()
    if not raw:
        return None
    # First attempt: literal parse
    try:
        return _json_inner.loads(raw)
    except _json_inner.JSONDecodeError:
        pass
    # Recovery: strip JS-style comments and trailing commas (Haiku
    # occasionally hallucinates these from training-data drift).
    cleaned = _re_inner.sub(r"//[^\n]*", "", raw)
    cleaned = _re_inner.sub(r"/\*.*?\*/", "", cleaned, flags=_re_inner.DOTALL)
    cleaned = _re_inner.sub(r",(\s*[}\]])", r"\1", cleaned)  # trailing commas
    try:
        return _json_inner.loads(cleaned)
    except _json_inner.JSONDecodeError:
        pass
    # Recovery: try extracting just the {...} substring in case the model
    # padded the block with extra prose
    match = _re_inner.search(r"\{.*\}", cleaned, _re_inner.DOTALL)
    if match:
        try:
            return _json_inner.loads(match.group(0))
        except _json_inner.JSONDecodeError:
            pass
    logging.getLogger("aria.ceo_chat").warning(
        "[%s-parse] all recovery attempts failed for block: %s",
        kind, raw[:300],
    )
    return None


def _safe_background(coro, *, label: str = "background"):
    """Spawn an asyncio task with an error callback so silent crashes show
    up in logs. Without this, exceptions raised inside _aio.create_task(...)
    coroutines only surface at GC time as 'Task exception was never
    retrieved' -- the user sees the chat reply but the inbox row never
    arrives and there's no error in any log you'd think to check.
    """
    import asyncio as _aio
    task = _aio.create_task(coro)

    def _on_done(t: _aio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            import traceback
            logging.getLogger("aria.background").error(
                "[%s] task crashed: %s\n%s",
                label, exc, "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            )

    task.add_done_callback(_on_done)
    return task


def _markdown_to_basic_html(text: str) -> str:
    """Quick markdown -> HTML converter for email body rendering.

    This is the fallback used when the agent's reply doesn't include a
    fenced ```html``` block. The frontend's email editor renders this in
    its Source / Preview tab so users see the body content instead of
    an empty editor. Not a full markdown parser -- just covers the
    common cases that show up in agent output: bold, italic, headers,
    bullet/numbered lists, paragraph breaks, and inline links.
    """
    import html as _html
    import re

    if not text:
        return ""

    # HTML-escape first so user content can never inject tags
    out = _html.escape(text)

    # Inline links: [text](url)  -- do BEFORE bold/italic so the brackets
    # don't get eaten by the asterisk parser
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', out)

    # Bold ** ** and italic * *
    out = re.sub(r"\*\*([^*\n]+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", out)

    # Headers (H1-H3)
    out = re.sub(r"(?m)^###\s+(.+)$", r"<h3>\1</h3>", out)
    out = re.sub(r"(?m)^##\s+(.+)$", r"<h2>\1</h2>", out)
    out = re.sub(r"(?m)^#\s+(.+)$", r"<h1>\1</h1>", out)

    # Horizontal rule
    out = re.sub(r"(?m)^---+\s*$", "<hr/>", out)

    # Lists -- group consecutive bullet/numbered lines into <ul>/<ol>
    lines = out.split("\n")
    rendered: list[str] = []
    in_ul = False
    in_ol = False
    for line in lines:
        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        ordered = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if bullet:
            if not in_ul:
                if in_ol:
                    rendered.append("</ol>")
                    in_ol = False
                rendered.append("<ul>")
                in_ul = True
            rendered.append(f"<li>{bullet.group(1)}</li>")
        elif ordered:
            if not in_ol:
                if in_ul:
                    rendered.append("</ul>")
                    in_ul = False
                rendered.append("<ol>")
                in_ol = True
            rendered.append(f"<li>{ordered.group(1)}</li>")
        else:
            if in_ul:
                rendered.append("</ul>")
                in_ul = False
            if in_ol:
                rendered.append("</ol>")
                in_ol = False
            rendered.append(line)
    if in_ul:
        rendered.append("</ul>")
    if in_ol:
        rendered.append("</ol>")
    out = "\n".join(rendered)

    # Paragraph wrapping: split on blank lines, wrap text-only blocks in <p>
    paragraphs = re.split(r"\n\s*\n", out)
    wrapped: list[str] = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        # If the block already starts with a tag, don't double-wrap
        if re.match(r"^\s*<(h\d|ul|ol|li|hr|p|div|table|blockquote)", p):
            wrapped.append(p)
        else:
            # Convert single newlines inside paragraphs to <br/>
            wrapped.append("<p>" + p.replace("\n", "<br/>") + "</p>")
    body_html = "\n".join(wrapped)

    return f'<div style="font-family: -apple-system, system-ui, sans-serif; line-height: 1.5; color: #1f2937;">{body_html}</div>'


def _enrich_task_desc_with_crm(task_desc: str, tenant_id: str) -> str:
    """If task_desc mentions a contact name from the CRM, append their email
    so the delegated agent can use it as the recipient. Otherwise return
    the task description unchanged.

    The CEO's CRM-context heuristic only triggers on "send email to <name>"
    style phrasing -- "create marketing email for Hanz" doesn't include a
    CRM noun, so the system prompt doesn't get the CRM dump and the CEO
    has no email to pass through to the email_marketer. This helper
    closes that gap by doing a cheap CRM lookup right before dispatch
    and inlining matched contacts into the task description.
    """
    if not task_desc or not tenant_id:
        return task_desc
    try:
        sb = _get_supabase()
        contacts = (
            sb.table("crm_contacts")
            .select("name,email,company_id,status")
            .eq("tenant_id", tenant_id)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )
        if not contacts.data:
            return task_desc

        task_lower = task_desc.lower()
        matches: list[dict] = []
        for c in contacts.data:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            # Match on full name OR first token (handle "Hanz" vs "Hanz Smith")
            tokens = [name.lower()] + [t.lower() for t in name.split() if len(t) >= 3]
            if any(t in task_lower for t in tokens):
                matches.append(c)
                if len(matches) >= 3:  # cap to avoid flooding the prompt
                    break

        if not matches:
            return task_desc

        # Append matched contacts to the task description so the agent
        # has the email address (and any status info) when generating.
        contact_lines = []
        for c in matches:
            email = c.get("email") or "(no email)"
            status = c.get("status") or ""
            contact_lines.append(f"  - {c['name']} <{email}>" + (f" [{status}]" if status else ""))
        return (
            f"{task_desc}\n\n"
            f"CRM contacts mentioned in this task (use as recipient):\n"
            + "\n".join(contact_lines)
        )
    except Exception as e:
        logging.getLogger("aria.crm").debug("CRM enrichment failed: %s", e)
        return task_desc


# ── Module-level email template regexes ───────────────────────────────
# Compiled once at import time so the email-render path doesn't recompile
# 12 patterns on every inbox row build. Used by _wrap_email_in_designed_template
# and _strip_html_to_text.
import re as _re_email
_EMAIL_BODY_TAG_RE = _re_email.compile(r"<body[^>]*>(.*?)</body>", _re_email.IGNORECASE | _re_email.DOTALL)
_EMAIL_CTA_RE = _re_email.compile(
    r"(?:book|schedule|claim|get|see|try|start|book a)\s+(?:your\s+)?(?:free\s+)?(?:[a-z\-]+\s+){0,3}(?:demo|call|trial|consultation|meeting)",
    _re_email.IGNORECASE,
)
_EMAIL_LI_CALLOUT_RE = _re_email.compile(r"<li[^>]*>\s*<strong>([^<]+?):</strong>\s*([^<]+?)</li>")
_EMAIL_P_RESULT_RE = _re_email.compile(
    r"<p[^>]*>\s*<strong>(Result|Summary|Bottom Line)[^<]*:?</strong>([^<]+?)</p>",
    _re_email.IGNORECASE,
)
_EMAIL_H2_RE = _re_email.compile(r"<h2[^>]*>(.*?)</h2>", _re_email.IGNORECASE | _re_email.DOTALL)
_EMAIL_H3_RE = _re_email.compile(r"<h3[^>]*>(.*?)</h3>", _re_email.IGNORECASE | _re_email.DOTALL)
_EMAIL_P_NOSTYLE_RE = _re_email.compile(r"<p(?![^>]*style=)")
_EMAIL_UL_NOSTYLE_RE = _re_email.compile(r"<ul(?![^>]*style=)")
_EMAIL_LI_NOSTYLE_RE = _re_email.compile(r"<li(?![^>]*style=)")
_EMAIL_A_NOSTYLE_RE = _re_email.compile(r"<a(?![^>]*style=)")
_EMAIL_STRONG_NOSTYLE_RE = _re_email.compile(r"<strong(?![^>]*style=)")
# _strip_html_to_text patterns
_STRIP_STYLE_RE = _re_email.compile(r"<style[^>]*>[\s\S]*?</style>", _re_email.IGNORECASE)
_STRIP_SCRIPT_RE = _re_email.compile(r"<script[^>]*>[\s\S]*?</script>", _re_email.IGNORECASE)
_STRIP_BR_RE = _re_email.compile(r"<br\s*/?>", _re_email.IGNORECASE)
_STRIP_P_CLOSE_RE = _re_email.compile(r"</p>", _re_email.IGNORECASE)
_STRIP_DIV_CLOSE_RE = _re_email.compile(r"</div>", _re_email.IGNORECASE)
_STRIP_LI_CLOSE_RE = _re_email.compile(r"</li>", _re_email.IGNORECASE)
_STRIP_H_CLOSE_RE = _re_email.compile(r"</h[1-6]>", _re_email.IGNORECASE)
_STRIP_TAGS_RE = _re_email.compile(r"<[^>]+>")
_STRIP_BLANKS_RE = _re_email.compile(r"\n{3,}")


def _agent_html_already_designed(html: str) -> bool:
    """Return True if the agent's HTML output already has its own design.

    We only want to apply the backend's branded template wrapper to
    PLAIN, unstyled output (naked <p>/<ul>/<li> tags from the markdown
    converter). When the agent produces its own designed HTML -- inline
    styles, gradients, table-based layouts, dark themes, custom CTAs --
    we leave it alone so each email can look different.

    Detection signals (any one is enough):
      - Contains a <table> (almost always email-template layout)
      - Inline `style=` attribute count >= 5 (rich styling)
      - Mentions linear-gradient, max-width: 600px, or background-color
      - Has explicit @media or CSS-in-style-attr rules
    """
    if not html:
        return False
    h = html.lower()
    if "<table" in h:
        return True
    # Inline-style density
    if h.count('style="') >= 5 or h.count("style='") >= 5:
        return True
    if any(marker in h for marker in (
        "linear-gradient",
        "max-width: 600",
        "max-width:600",
        "background-color: #",
        "background:#",
        "background: #",
        "@media",
        "border-radius:",
        "box-shadow:",
    )):
        return True
    return False


def _business_name_for_template(tenant_id: str = "") -> str:
    """Return the tenant's business name for the email template header.

    Falls back to 'ARIA' if no tenant is known or the lookup fails.
    Cached implicitly by get_tenant_config so repeated calls are cheap.
    """
    if not tenant_id:
        return "ARIA"
    try:
        from backend.config.loader import get_tenant_config
        tc = get_tenant_config(tenant_id)
        return (tc.business_name or "ARIA").strip() or "ARIA"
    except Exception:
        return "ARIA"


def _wrap_email_in_designed_template(
    body_html: str,
    *,
    business_name: str = "ARIA",
    subject: str = "",
    preview_text: str = "",
    cta_text: str | None = None,
    cta_url: str | None = None,
) -> str:
    """Wrap plain HTML email content in a designed branded template.

    The agent produces simple `<p>Hi Hanz,</p>...<ul><li>...</li></ul>`
    output. To get the dark-themed branded design the user wants
    (gradient header, card-style sections, CTA button, footer), we
    wrap that plain content in this template shell. The agent stays
    dumb; the backend handles the design.

    Looks like: dark navy background, blue gradient header card with
    business name + tagline, dark inner card holding the body, cyan
    section headers, styled CTA button, muted footer with company
    name + year.

    If body_html already contains <html> or <!DOCTYPE, it's a complete
    document and we leave it alone (assume the agent designed it
    intentionally).
    """
    if not body_html:
        return ""

    body_lower = body_html.lower().lstrip()
    if body_lower.startswith(("<!doctype", "<html")):
        return body_html  # complete document already, don't double-wrap

    # Strip the outer <body> wrapper if the parser added one
    m = _EMAIL_BODY_TAG_RE.search(body_html)
    if m:
        body_html = m.group(1).strip()

    # Auto-extract a CTA from common phrases if not provided
    if not cta_text:
        m = _EMAIL_CTA_RE.search(_strip_html_to_text(body_html))
        if m:
            cta_text = m.group(0).title()
    if not cta_text:
        cta_text = "Schedule a 15-Minute Demo"
    if not cta_url:
        cta_url = "#"

    # Style sections that look like callouts. The agent often uses
    # **Bold:** prefix lines for highlights -- give them card styling
    # with a colored left border on a light background.
    def _stylize_callout(match) -> str:
        label = match.group(1)
        rest = match.group(2)
        return (
            f'<div style="background: #fffbeb; '
            f'border-left: 4px solid #f59e0b; padding: 12px 16px; '
            f'margin: 8px 0; border-radius: 4px;">'
            f'<strong style="color: #92400e;">{label}:</strong>'
            f'<span style="color: #1f2937;"> {rest}</span>'
            f"</div>"
        )

    # Find <li><strong>Label:</strong> rest</li> patterns and turn into callouts
    body_html = _EMAIL_LI_CALLOUT_RE.sub(_stylize_callout, body_html)

    # Highlight <p><strong>Result:</strong> ...</p> as a green callout
    body_html = _EMAIL_P_RESULT_RE.sub(
        lambda m: (
            f'<div style="background: #ecfdf5; '
            f'border-left: 4px solid #10b981; padding: 14px 18px; '
            f'margin: 16px 0; border-radius: 4px;">'
            f'<strong style="color: #047857;">{m.group(1)}:</strong>'
            f'<span style="color: #064e3b;"> {m.group(2)}</span>'
            f"</div>"
        ),
        body_html,
    )

    # Restyle <h2>/<h3> as blue section headers
    body_html = _EMAIL_H2_RE.sub(
        r'<h2 style="color: #2563eb; font-size: 20px; font-weight: 600; margin: 28px 0 12px 0;">\1</h2>',
        body_html,
    )
    body_html = _EMAIL_H3_RE.sub(
        r'<h3 style="color: #2563eb; font-size: 17px; font-weight: 600; margin: 24px 0 10px 0;">\1</h3>',
        body_html,
    )

    # Restyle paragraphs, lists, links, and bold text in the light theme
    body_html = _EMAIL_P_NOSTYLE_RE.sub(
        '<p style="color: #374151; font-size: 15px; line-height: 1.7; margin: 14px 0;"',
        body_html,
    )
    body_html = _EMAIL_UL_NOSTYLE_RE.sub(
        '<ul style="color: #374151; padding-left: 22px; margin: 14px 0;"',
        body_html,
    )
    body_html = _EMAIL_LI_NOSTYLE_RE.sub(
        '<li style="margin: 8px 0; line-height: 1.6;"',
        body_html,
    )
    body_html = _EMAIL_A_NOSTYLE_RE.sub(
        '<a style="color: #2563eb; text-decoration: underline;"',
        body_html,
    )
    body_html = _EMAIL_STRONG_NOSTYLE_RE.sub(
        '<strong style="color: #111827;"',
        body_html,
    )

    # Build the template
    business_name_safe = (business_name or "ARIA").strip() or "ARIA"
    title_text = subject.strip() if subject else f"News from {business_name_safe}"
    tagline = preview_text.strip() if preview_text else f"From the {business_name_safe} team"
    year = datetime.now(timezone.utc).year

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_text}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f3f4f6; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color: #f3f4f6; padding: 32px 12px;">
  <tr><td align="center">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width: 600px; width: 100%; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04);">
      <!-- Header card with blue gradient -->
      <tr><td style="background: linear-gradient(135deg, #2563eb 0%, #1e40af 100%); padding: 40px 32px; text-align: center;">
        <h1 style="color: #ffffff; font-size: 26px; font-weight: 700; margin: 0 0 8px 0; line-height: 1.3;">{title_text}</h1>
        <p style="color: rgba(255,255,255,0.9); font-size: 15px; margin: 0; line-height: 1.5;">{tagline}</p>
      </td></tr>
      <!-- Body card (light/white) -->
      <tr><td style="background-color: #ffffff; padding: 36px 36px 24px 36px;">
        {body_html}
        <!-- CTA button -->
        <div style="text-align: center; margin: 32px 0 8px 0;">
          <a href="{cta_url}" style="display: inline-block; background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%); color: #ffffff; padding: 14px 32px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px; box-shadow: 0 2px 4px rgba(37, 99, 235, 0.2);">{cta_text}</a>
        </div>
      </td></tr>
      <!-- Footer -->
      <tr><td style="background-color: #f9fafb; padding: 24px 32px; border-top: 1px solid #e5e7eb; text-align: center;">
        <p style="color: #6b7280; font-size: 12px; margin: 4px 0;">&copy; {year} {business_name_safe}. All rights reserved.</p>
        <p style="color: #6b7280; font-size: 12px; margin: 4px 0;">
          <a href="#" style="color: #6b7280; text-decoration: none;">Privacy Policy</a> &nbsp;|&nbsp;
          <a href="#" style="color: #6b7280; text-decoration: none;">Contact Us</a> &nbsp;|&nbsp;
          <a href="#" style="color: #6b7280; text-decoration: none;">Unsubscribe</a>
        </p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


def _strip_html_to_text(html: str) -> str:
    """Convert an HTML body into a plain text approximation for the
    text_body / preview_snippet fields. Not perfect -- just enough to
    give the user a readable plaintext version. Mirrors the same logic
    the frontend uses in stripHtml() at frontend/app/.../inbox/page.tsx.
    """
    if not html:
        return ""
    out = html
    out = _STRIP_STYLE_RE.sub("", out)
    out = _STRIP_SCRIPT_RE.sub("", out)
    out = _STRIP_BR_RE.sub("\n", out)
    out = _STRIP_P_CLOSE_RE.sub("\n\n", out)
    out = _STRIP_DIV_CLOSE_RE.sub("\n", out)
    out = _STRIP_LI_CLOSE_RE.sub("\n", out)
    out = _STRIP_H_CLOSE_RE.sub("\n\n", out)
    out = _STRIP_TAGS_RE.sub("", out)
    out = (out
           .replace("&nbsp;", " ")
           .replace("&amp;", "&")
           .replace("&lt;", "<")
           .replace("&gt;", ">")
           .replace("&quot;", '"')
           .replace("&#39;", "'"))
    out = _STRIP_BLANKS_RE.sub("\n\n", out)
    return out.strip()


def _parse_html_email_draft(text: str, fallback_to: str = "") -> dict | None:
    """Parse an email_draft when the agent's content IS raw HTML.

    Detection: content starts with <!DOCTYPE, <html>, or has many HTML
    tags relative to length. The previous markdown parser would extract
    the <html><body style="..."> opening tag as the SUBJECT field via
    the first-sentence fallback, which is exactly the bug we saw in
    production.

    Strategy:
      - Subject: prefer <title>, then first <h1>/<h2>, then any
        SUBJECT: marker in the rendered text
      - To: any email-shaped token in the rendered text (NOT in
        attribute values like style="font-family: ...@...")
      - html_body: the inner HTML between <body> tags, or the whole
        thing if no body tag
      - text_body: stripped HTML
    """
    import re
    if not text or len(text) < 30:
        return None

    # Subject extraction order: <title> -> <h1>/<h2>/<h3> -> Subject markers
    # in stripped text -> first non-greeting <p> content
    subject = None
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if m:
        subject = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    if not subject:
        for tag in ("h1", "h2", "h3"):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.IGNORECASE | re.DOTALL)
            if m:
                candidate = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                if candidate and len(candidate) > 5:
                    subject = candidate
                    break

    # Fall back to running subject markers against the stripped text.
    # This catches "Subject Line Options:" / "Subject A: ..." patterns
    # the agent embeds inside <p> tags.
    if not subject:
        stripped = _strip_html_to_text(text)
        # Pattern: A: "..." or A) "..." after a "Subject" header
        m = re.search(
            r"Subject[^\n]{0,40}\n+\s*A[):]\s*[\"']?([^\"'\n]+)[\"']?",
            stripped, re.IGNORECASE,
        )
        if m:
            subject = m.group(1).strip()
        if not subject:
            # Pattern: Subject: <value>
            m = re.search(r"(?:^|\n)\s*Subject\s*(?:Line)?\s*[:\-]\s*[\"']?([^\"'\n]+)[\"']?", stripped, re.IGNORECASE)
            if m:
                cand = m.group(1).strip()
                if cand and len(cand) > 5:
                    subject = cand
        if not subject:
            # Pattern: Preview Text: <value>
            m = re.search(r"(?:^|\n)\s*Preview\s*(?:Text)?\s*[:\-]\s*[\"']?([^\"'\n]+)[\"']?", stripped, re.IGNORECASE)
            if m:
                cand = m.group(1).strip()
                if cand and len(cand) > 5:
                    subject = cand
        if not subject:
            # Last resort: first <p> that isn't a greeting
            for m in re.finditer(r"<p[^>]*>(.*?)</p>", text, re.IGNORECASE | re.DOTALL):
                cand = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                if not cand or len(cand) < 15:
                    continue
                if re.match(r"^(hi|hello|hey|dear|best|sincerely|cheers|thanks|p\.?s\.?)\b", cand, re.IGNORECASE):
                    continue
                # Truncate to 100 chars at word boundary
                if len(cand) > 100:
                    cand = cand[:100].rsplit(" ", 1)[0] + "..."
                subject = cand
                break

    if subject:
        subject = subject.replace("&amp;", "&").replace("&nbsp;", " ").strip()
        # Truncate at line breaks for one-line subjects
        subject = subject.split("\n")[0].strip()[:200]

    # html_body: prefer the inner contents of <body>...</body>
    html_body = text
    m = re.search(r"<body[^>]*>(.*?)</body>", text, re.IGNORECASE | re.DOTALL)
    if m:
        html_body = m.group(1).strip()

    # Only wrap in the backend template when the agent's HTML is
    # plain/unstyled. If the agent already designed its own email
    # (inline styles, gradients, table layouts), leave it alone.
    if html_body and not _agent_html_already_designed(html_body):
        preview_text = ""
        pm = re.search(r"Preview\s*Text[^:]*:\s*([^\n]+)", _strip_html_to_text(text), re.IGNORECASE)
        if pm:
            preview_text = pm.group(1).strip().strip('"').strip("*").strip()[:120]
        html_body = _wrap_email_in_designed_template(
            html_body,
            business_name=_business_name_for_template(),
            subject=subject or "",
            preview_text=preview_text,
        )

    # text_body and preview from the rendered text
    text_body = _strip_html_to_text(text)
    preview = text_body[:200]

    # Recipient: any email address in the rendered text (avoids attribute
    # value matches like style="font: ...@...")
    to = fallback_to or ""
    if not to:
        # Strip ALL tag attributes first so style="...@..." doesn't false-match
        text_only = re.sub(r"<[^>]+>", " ", text)
        m = re.search(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b", text_only)
        if m:
            to = m.group(1)

    if not (subject or text_body):
        return None

    return {
        "subject": (subject or "Untitled email")[:300],
        "to": to or "",
        "send_time": "",
        "text_body": text_body[:5000],
        "html_body": html_body or "",
        "preview_snippet": preview,
        "status": "draft_pending_approval",
    }


def _parse_email_draft_from_text(text: str, fallback_to: str = "") -> dict | None:
    """Extract structured email fields from a free-form agent reply.

    The local _run_agent_to_inbox path produced rich email_draft objects
    directly from the agent's Python module return value (subject, body,
    body_html, to, preview_snippet). The Paperclip path only gives us
    the agent's text reply as a comment, so we have to parse markdown
    looking for the same fields.

    Patterns recognised (case-insensitive, all optional):
      **Subject:** ... | Subject: ...                  -> subject
      **To:** | **Recipient:** | **Send to:** ...      -> to (email address)
      **Send Time:** | **When:** ...                   -> send_time
      ```html ... ```  fenced code block               -> body_html
      everything else                                  -> body (plaintext)

    Returns a dict suitable for the inbox_items.email_draft column, or
    None if nothing email-shaped was found. The frontend renders the
    Approve & Send / Schedule / Cancel draft buttons whenever this
    column is non-null.
    """
    import re

    if not text or len(text) < 30:
        return None

    # If the agent posted raw HTML as content (which we saw in production
    # after the agent started generating designed emails directly), use
    # the HTML-aware parser instead -- the markdown patterns below would
    # otherwise grab the <html><body style="..."> opening tag as the
    # first sentence and use it as the subject.
    _stripped = text.lstrip()
    _looks_like_html = (
        _stripped.startswith("<!DOCTYPE")
        or _stripped[:200].lower().startswith(("<html", "<body"))
        or text.count("<") > 20  # heavy tag density = probably HTML
    )
    if _looks_like_html:
        html_result = _parse_html_email_draft(text, fallback_to=fallback_to)
        if html_result:
            return html_result
        # Fall through to markdown parser if HTML parsing returned None

    # Subject -- handle three common email_marketer formats:
    #   1. **Subject:** "value"  /  Subject: value
    #   2. **Subject Line A/B Testing:**\n  **A)** "..."\n  **B)** "..."   (use A)
    #   3. Subject line as a header followed by quoted line on the next line
    subject = None
    # Format 2: A/B test header -> grab the **A)** value
    m = re.search(
        r"\*\*\s*Subject\s*Line\s*(?:A/B\s*)?(?:Testing|Test|Variants?)?\s*:?\s*\*\*\s*\n+\s*\*\*?\s*A\)?\s*\*?\*?\s*[:\-]?\s*(.+)",
        text,
        re.IGNORECASE,
    )
    if m:
        subject = m.group(1).strip().splitlines()[0]
    # Format 1: inline subject value
    if not subject:
        for pat in (
            r"\*\*\s*Subject\s*(?:Line)?\s*:?\s*\*\*\s*[:\-]?\s*(.+)",
            r"(?:^|\n)\s*Subject\s*(?:Line)?\s*[:\-]\s*(.+)",
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip().splitlines()[0]
                if candidate and not candidate.startswith("**"):
                    subject = candidate
                    break
    # Format 3: orphan **A)** line right after a Subject header (already
    # picked up by format 2, but be defensive in case A/B header was on
    # a different line shape)
    if not subject:
        m = re.search(
            r"\*\*\s*Subject[^\n]*\*\*\s*\n+\s*\*?\*?\s*A\)?\s*\*?\*?\s*[:\-]?\s*(.+)",
            text,
            re.IGNORECASE,
        )
        if m:
            subject = m.group(1).strip().splitlines()[0]
    # Clean up the matched subject
    if subject:
        subject = subject.strip()
        # Strip surrounding asterisks, quotes, backticks
        subject = re.sub(r'^[\s\*"\'`]+|[\s\*"\'`]+$', "", subject).strip()
        if not subject:
            subject = None

    # Fallback 1: Preview Text. Agents sometimes emit
    # **Preview Text:** "..." instead of an explicit Subject. The preview
    # is a marketing-style one-line summary, which is exactly what we
    # want for a subject when nothing better exists.
    if not subject:
        m = re.search(
            r"\*\*\s*Preview\s*(?:Text)?\s*(?:\([^)]*\))?\s*:?\s*\*\*\s*[:\-]?\s*(.+)",
            text,
            re.IGNORECASE,
        )
        if m:
            candidate = m.group(1).strip().splitlines()[0]
            candidate = re.sub(r'^[\s\*"\'`]+|[\s\*"\'`]+$', "", candidate).strip()
            if candidate and not candidate.startswith("**") and len(candidate) > 5:
                subject = candidate[:200]

    # Fallback 2: first non-trivial sentence of the email body. We strip
    # the markdown markers we already extracted, then look for the first
    # line that isn't a label or greeting like "Hi Hanz,".
    if not subject:
        cleaned = re.sub(r"\*\*[^*]+\*\*\s*[:\-]?", "", text)  # strip **Label:** markers
        cleaned = re.sub(r"```[\s\S]*?```", "", cleaned)        # strip code blocks
        cleaned = re.sub(r"^---+\s*$", "", cleaned, flags=re.MULTILINE)
        for line in cleaned.split("\n"):
            line = line.strip()
            if not line or len(line) < 15:
                continue
            # Skip greetings and signoffs
            if re.match(r"^(hi|hello|hey|dear|best|sincerely|cheers|thanks|p\.?s\.?)\b", line, re.IGNORECASE):
                continue
            # Skip lines that are mostly markdown noise
            if line.startswith(("#", "-", "*", "[", ">")):
                continue
            # First good sentence -- truncate to 100 chars at a word boundary
            candidate = line[:120]
            if len(line) > 120:
                candidate = candidate.rsplit(" ", 1)[0] + "..."
            subject = candidate
            break

    # Recipient email address
    to = fallback_to or ""
    if not to:
        for pat in (
            r"\*\*\s*(?:To|Recipient|Send\s*to)\s*:?\s*\*\*\s*[:\-]?\s*([^\s\n*]+@[^\s\n*]+)",
            r"(?:^|\n)\s*(?:To|Recipient)\s*[:\-]\s*([^\s\n]+@[^\s\n]+)",
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                to = m.group(1).strip().rstrip(".,;:")
                break
    if not to:
        # Fall back to any email-shaped token in the text
        m = re.search(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b", text)
        if m:
            to = m.group(1)

    # Send time / when
    send_time = None
    for pat in (
        r"\*\*\s*(?:Send\s*Time|When)\s*:?\s*\*\*\s*[:\-]?\s*(.+)",
        r"(?:^|\n)\s*Send\s*Time\s*[:\-]\s*(.+)",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            send_time = m.group(1).strip().splitlines()[0].strip("*").strip()
            break

    # HTML body in fenced code block
    body_html = None
    m = re.search(r"```html\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    if m:
        body_html = m.group(1).strip()

    # Plain body: strip the fenced HTML block + the marker lines we already
    # extracted, leaving the rest as the readable email body
    body = text
    body = re.sub(r"```html\s*\n.*?\n```", "", body, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"\*\*\s*Subject[^\n]*\n", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\*\*\s*(?:To|Recipient|Send\s*to)[^\n]*\n", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\*\*\s*(?:Send\s*Time|When)[^\n]*\n", "", body, flags=re.IGNORECASE)
    body = body.strip()

    # Bail if nothing useful was extracted -- the watcher's plain content
    # field is a better fallback than an empty email_draft.
    if not (subject or body_html or body):
        return None

    # If the agent didn't emit a fenced ```html``` block, generate basic
    # HTML from the plain markdown body so the inbox UI's email editor
    # has something to render in its Source/Preview tab.
    if not body_html and body:
        body_html = _markdown_to_basic_html(body)

    # Extract preview text if the agent included one
    preview_text = ""
    pm = re.search(r"\*\*\s*Preview\s*(?:Text)?[^*]*\*\*\s*[:\-]?\s*([^\n]+)", text, re.IGNORECASE)
    if pm:
        preview_text = pm.group(1).strip().strip('"').strip("*").strip()[:120]

    # Only wrap the agent's HTML in the backend template when the
    # agent's output is plain unstyled HTML. If the agent already
    # produced its own designed email (inline styles, gradients,
    # table layouts), leave it alone -- each email is allowed to
    # look different. The wrapper is a fallback for the "agent
    # only emitted naked <p>/<ul>" case.
    if body_html and not _agent_html_already_designed(body_html):
        final_html = _wrap_email_in_designed_template(
            body_html,
            business_name=_business_name_for_template(),
            subject=subject or "",
            preview_text=preview_text,
        )
    else:
        final_html = body_html

    # IMPORTANT: field names must match the frontend's EmailDraft
    # interface (frontend/app/(dashboard)/inbox/page.tsx) -- it reads
    # `email_draft.html_body` and `email_draft.text_body`, NOT
    # `body_html` / `body`.
    return {
        "subject": (subject or "Untitled email")[:300],
        "to": to or "",
        "send_time": send_time or "",
        "text_body": body[:5000],
        "html_body": final_html,
        "preview_snippet": (body or text)[:200],
        "status": "draft_pending_approval",
    }


def _parse_social_drafts_from_text(text: str) -> dict | None:
    """Extract X/Twitter and LinkedIn post variants from an agent reply.

    Patterns recognised:
      **Twitter:** ... | **X:** ... | **X/Twitter:** ...
      **LinkedIn:** ...
      ## Twitter / ## LinkedIn headers

    Returns a dict like {twitter: "...", linkedin: "..."} for the
    inbox row's social_draft column, or None if nothing recognisable
    was found. The frontend uses this to render Publish to X /
    Publish to LinkedIn buttons.
    """
    import re

    if not text or len(text) < 30:
        return None

    def _grab_section(label_pattern: str) -> str | None:
        """Find a section starting with **<label>:** or ## <label> and
        return its body up to the next section marker or end of text."""
        # Match either bold-prefix or H2 heading
        pat = (
            rf"(?:\*\*\s*{label_pattern}\s*[:\-]?\s*\*\*|##\s*{label_pattern})"
            rf"\s*[:\-]?\s*(.*?)"
            rf"(?=\n\s*\*\*\s*\w[^*]*\*\*\s*[:\-]|\n\s*##\s+|\Z)"
        )
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        section = m.group(1).strip()
        # Strip leading/trailing markdown bullets and quotes
        section = re.sub(r"^[\s>\-*]+", "", section)
        section = re.sub(r"[\s>\-*]+$", "", section)
        return section or None

    twitter = (
        _grab_section(r"(?:Twitter|X(?:/Twitter)?|Tweet)")
    )
    linkedin = _grab_section(r"LinkedIn")

    if not (twitter or linkedin):
        return None

    return {
        "twitter": (twitter or "")[:1000],
        "linkedin": (linkedin or "")[:5000],
    }


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
    paperclip_issue_id: str | None = None,
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
        if paperclip_issue_id:
            # Setting this at insert time prevents the race where the
            # global poller imports a duplicate row in the gap between
            # placeholder creation and the watcher's later UPDATE.
            row["paperclip_issue_id"] = paperclip_issue_id
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

    # Hoist placeholder_id outside the try so the except handler at the
    # bottom can update it instead of creating a duplicate "Failed:" row.
    # Without this, an error mid-run leaves the placeholder orphaned at
    # "processing" forever.
    placeholder_id: str | None = None

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
        logging.getLogger("aria.inbox").error(
            "Agent %s failed for tenant %s: %s", agent_id, tenant_id, e, exc_info=True,
        )
        # Sanitize the error -- don't leak raw exception details (may
        # include API keys / connection strings) into the user-visible
        # inbox row.
        error_summary = _sanitize_error_message(e)
        error_content = (
            f"The {agent_id} agent encountered an error while processing this task:\n\n"
            f"**Task:** {task_desc}\n\n"
            f"**Error:** {error_summary}\n\n"
            "Please try again. If this persists, check Settings > Integrations "
            "to ensure required credentials (Gmail, X/Twitter, etc.) are connected."
        )
        error_title = f"Failed: {task_desc[:60]}"
        # Update the placeholder if we have one (don't orphan it). Fall
        # back to creating a fresh row only if the placeholder update fails
        # or no placeholder was ever created.
        updated = False
        if placeholder_id:
            try:
                sb = _get_supabase()
                sb.table("inbox_items").update({
                    "title": error_title,
                    "content": error_content,
                    "type": "error",
                    "status": "needs_review",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", placeholder_id).execute()
                updated = True
            except Exception as upd_err:
                logging.getLogger("aria.inbox").error(
                    "Failed to update placeholder %s with error: %s", placeholder_id, upd_err,
                )
        if not updated:
            _save_inbox_item(
                tenant_id=tenant_id,
                agent=agent_id,
                title=error_title,
                content=error_content,
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


async def _dispatch_paperclip_and_watch_to_inbox(
    tenant_id: str,
    agent_id: str,
    task_desc: str,
    session_id: str | None = None,
    task_id: str | None = None,
    priority: str = "medium",
    timeout_sec: int = 600,
):
    """Dispatch an agent via Paperclip and actively watch the issue until it's done.

    The 5-second global poll loop in paperclip_office_sync was the safety
    net for inbox arrival, but it had two problems for active delegations:
      1. Latency — up to 5s after the agent finishes
      2. Sometimes silently misses runs (issue status not transitioning,
         output not picked up by pick_agent_output, etc.)

    Phases:
      1. Dispatch first to get the paperclip_issue_id (cheap, ~0.5-1s)
      2. Create the placeholder inbox row WITH the issue id baked in --
         this closes the dedupe race where the global poller could
         insert a duplicate before the watcher's later UPDATE
      3. Poll THIS specific issue with adaptive intervals (1s -> 4s)
      4. Bail fast on failed/cancelled status or PaperclipUnreachable
         outage instead of waiting the full 10 min
      5. As soon as a substantive agent reply comment shows up, update
         the placeholder with the real content -> instant arrival

    The global poller still runs as a backstop for direct Paperclip
    Timer runs and edge cases this watcher times out on.
    """
    import asyncio as _aio_inner

    _logger = logging.getLogger("aria.paperclip_watch")

    # Phase 1: dispatch FIRST. Cheap (~0.5-1s) and gives us the issue id
    # we need to bake into the placeholder so the global poller can't
    # race us and insert a duplicate.
    try:
        result = await dispatch_agent(tenant_id, agent_id, context={
            "task": task_desc,
            "priority": priority,
            "session_id": session_id,
        })
    except Exception as e:
        _logger.error("[paperclip-watch] dispatch_agent raised for %s: %s", agent_id, e)
        result = {}

    paperclip_issue_id = result.get("paperclip_issue_id") if isinstance(result, dict) else None

    # Media special-case: we still create the placeholder so the user sees
    # "Media is working on..." instantly, but we skip the Paperclip-comment
    # polling/update phase. The Media Designer's instruction MD tells it to
    # curl /api/media/<tenant>/generate, which UPDATES this placeholder in
    # place with the real Pollinations PNG. Polling Paperclip comments was
    # the source of stale-URL pollution and duplicate rows.
    if agent_id == "media":
        placeholder_content_type = _infer_content_type(agent_id, "")
        placeholder = _save_inbox_item(
            tenant_id=tenant_id,
            agent=agent_id,
            title=f"Media is working on: {task_desc[:80]}",
            content="Generating image...",
            content_type=placeholder_content_type,
            priority=priority,
            task_id=task_id,
            chat_session_id=session_id,
            status="processing",
            paperclip_issue_id=paperclip_issue_id,
        )
        if placeholder and tenant_id:
            try:
                await sio.emit("inbox_new_item", {
                    "id": placeholder["id"],
                    "agent": agent_id,
                    "type": placeholder_content_type,
                    "title": placeholder.get("title", ""),
                    "status": "processing",
                    "priority": priority,
                    "created_at": placeholder.get("created_at", ""),
                }, room=tenant_id)
            except Exception:
                pass
        if paperclip_issue_id:
            try:
                _add_processed(paperclip_issue_id)  # block global poller too
            except Exception:
                pass
        return

    # Phase 2: create the placeholder inbox row. If we got an issue id,
    # bake it in so the dedupe column is set from the start.
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
        paperclip_issue_id=paperclip_issue_id,
    )
    placeholder_id = placeholder["id"] if placeholder else None
    if placeholder and tenant_id:
        try:
            await sio.emit("inbox_new_item", {
                "id": placeholder_id,
                "agent": agent_id,
                "type": placeholder_content_type,
                "title": placeholder.get("title", ""),
                "status": "processing",
                "priority": priority,
                "created_at": placeholder.get("created_at", ""),
            }, room=tenant_id)
        except Exception as e:
            _logger.debug("[paperclip-watch] sio.emit inbox_new_item failed: %s", e)

    # If dispatch failed, surface the error in the placeholder and bail
    if not paperclip_issue_id:
        _logger.error("[paperclip-watch] no paperclip_issue_id from dispatch -- marking placeholder failed")
        if placeholder_id:
            try:
                sb = _get_supabase()
                sb.table("inbox_items").update({
                    "title": f"Failed to dispatch: {task_desc[:80]}",
                    "content": f"Could not assign this task to {agent_id} via Paperclip. Check Paperclip is running and the agent is configured.",
                    "status": "needs_review",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", placeholder_id).execute()
            except Exception as e:
                _logger.error("[paperclip-watch] failed to update placeholder on dispatch error: %s", e)
        return

    # Mark in global poller's processed set right away so the 5s poller
    # never imports this issue independently. We own this issue now.
    _add_processed(paperclip_issue_id)

    _logger.warning(
        "[paperclip-watch] watching issue %s for %s output (timeout %ds)",
        paperclip_issue_id, agent_id, timeout_sec,
    )

    # Phase 3: adaptive polling on this specific issue. Times match the
    # CEO chat sync poller — fast at first, then back off.
    intervals = (1.0, 1.0, 1.5, 2.0, 2.0, 3.0, 4.0)
    interval_idx = 0
    start = _aio_inner.get_event_loop().time()
    output: str | None = None
    failure_reason: str | None = None
    consecutive_outage_errors = 0
    _MAX_CONSECUTIVE_OUTAGES = 5  # ~10s of consecutive failures before bailing

    while True:
        elapsed = _aio_inner.get_event_loop().time() - start
        if elapsed >= timeout_sec:
            failure_reason = (
                f"Timeout: agent did not respond within {timeout_sec // 60} minutes."
            )
            _logger.warning(
                "[paperclip-watch] timeout after %.1fs waiting for %s (issue %s)",
                elapsed, agent_id, paperclip_issue_id,
            )
            break

        delay = intervals[min(interval_idx, len(intervals) - 1)]
        await _aio_inner.sleep(delay)
        interval_idx += 1

        # Check the issue's current status. If Paperclip says the run
        # failed/cancelled, bail fast instead of polling for 10 minutes.
        try:
            issue_data = _urllib_request("GET", f"/api/issues/{paperclip_issue_id}", strict=True)
            consecutive_outage_errors = 0
        except PaperclipUnreachable as e:
            consecutive_outage_errors += 1
            _logger.warning(
                "[paperclip-watch] Paperclip unreachable on poll %d for issue %s: %s",
                consecutive_outage_errors, paperclip_issue_id, e,
            )
            if consecutive_outage_errors >= _MAX_CONSECUTIVE_OUTAGES:
                failure_reason = (
                    "Paperclip is unreachable. The agent may still be running --"
                    " the global 5s poller will catch the result if Paperclip recovers."
                )
                break
            continue  # transient -- try again

        if isinstance(issue_data, dict):
            issue_status = (issue_data.get("status") or "").lower()
            if _is_failed(issue_status):
                failure_reason = (
                    f"Paperclip marked the run {issue_status!r}. The agent failed "
                    f"or was cancelled. Check Paperclip for the run logs."
                )
                _logger.warning(
                    "[paperclip-watch] issue %s entered failed state %s -- bailing",
                    paperclip_issue_id, issue_status,
                )
                break

        # Cheap check: fetch comments and look for a substantive reply.
        try:
            raw_comments = _urllib_request(
                "GET", f"/api/issues/{paperclip_issue_id}/comments", strict=True,
            )
        except PaperclipUnreachable:
            # Already counted above; just retry
            continue
        comments = normalize_comments(raw_comments)
        candidate = pick_agent_output(comments, exclude_text=task_desc, expected_agent=agent_id)

        # Need a substantive reply, not a one-liner status update
        if candidate and len(candidate) >= 50:
            output = candidate
            _logger.warning(
                "[paperclip-watch] %s replied for issue %s (%d chars after %.1fs)",
                agent_id, paperclip_issue_id, len(output), elapsed,
            )
            break

        # If Paperclip says the issue is FINISHED but pick_agent_output
        # returned nothing usable, something is wrong with comment filtering.
        # Dump a sample of the comment shapes (author + length + body
        # preview) so we can see what the watcher is rejecting and why.
        # This logs at most ONCE per watcher run -- gated on `output is None`
        # AND `_is_finished(issue_status)` so we don't spam during normal
        # in-progress polling.
        if isinstance(issue_data, dict) and _is_finished(issue_status) and not output:
            try:
                sample = []
                for c in (comments or [])[:8]:
                    body = (c.get("body") or c.get("content") or "").strip()
                    author_field = c.get("author") or c.get("agent") or c.get("authorName") or "?"
                    if isinstance(author_field, dict):
                        author_field = (
                            author_field.get("name")
                            or author_field.get("displayName")
                            or author_field.get("slug")
                            or "?"
                        )
                    sample.append(
                        f"author={author_field!r} len={len(body)} preview={body[:80]!r}"
                    )
                _logger.warning(
                    "[paperclip-watch] issue %s status=%s but no usable comment "
                    "from pick_agent_output (expected_agent=%s); %d comments seen: %s",
                    paperclip_issue_id, issue_status, agent_id, len(comments), " | ".join(sample),
                )
            except Exception as diag_err:
                _logger.warning(
                    "[paperclip-watch] diagnostic dump failed: %s", diag_err,
                )
            # Issue is finished and we still got nothing -- bail with a
            # clearer failure reason than the generic timeout.
            failure_reason = (
                f"Paperclip marked the issue {issue_status!r} but no agent reply "
                f"was found in the comments. Check the watcher diagnostic log line "
                f"for what comments were seen."
            )
            break

    # Phase 4: write the result to inbox. Either we have output (success)
    # or we have a failure_reason (timeout / failed status / outage).
    # Hoist the now-iso once -- it was being recomputed 4 times below.
    now_iso = datetime.now(timezone.utc).isoformat()

    if not output:
        msg = failure_reason or "Agent run did not produce output."
        if placeholder_id:
            try:
                sb = _get_supabase()
                fail_update = {
                    "title": f"Failed: {task_desc[:80]}",
                    "content": msg,
                    "status": "needs_review",
                    "updated_at": now_iso,
                }
                await asyncio.to_thread(
                    lambda: sb.table("inbox_items").update(fail_update).eq("id", placeholder_id).execute()
                )
            except Exception as e:
                _logger.error("[paperclip-watch] failed to update placeholder on failure: %s", e)
        return

    # Before updating the placeholder, check whether the agent already
    # created its OWN inbox row via the aria-backend-api skill curl.
    # If yes, the watcher's placeholder is redundant -- delete it so
    # the user sees ONE row per delegation, not two. The agent's skill
    # curl row has the actual structured email content (html_body,
    # text_body, email_draft) that the inbox CREATE endpoint parsed,
    # while the watcher's placeholder only has the agent's reply
    # COMMENT (often a short summary like "Created and saved").
    #
    # Both the SELECT and the DELETE are wrapped in to_thread so they
    # don't block the event loop on the supabase-py sync HTTP call.
    skill_row_already_exists = False
    if placeholder_id:
        try:
            sb = _get_supabase()
            recent_window = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
            agent_rows = await asyncio.to_thread(
                lambda: sb.table("inbox_items")
                .select("id,content,status")
                .eq("tenant_id", tenant_id)
                .eq("agent", agent_id)
                .gte("created_at", recent_window)
                .neq("id", placeholder_id)
                .execute()
            )
            for r in (agent_rows.data or []):
                content_len = len(r.get("content") or "")
                if content_len > 200 and r.get("status") != "processing":
                    _logger.warning(
                        "[paperclip-watch] agent %s already created row %s for tenant %s "
                        "(content=%d chars) -- deleting placeholder %s to avoid duplicate",
                        agent_id, r["id"], tenant_id, content_len, placeholder_id,
                    )
                    try:
                        await asyncio.to_thread(
                            lambda: sb.table("inbox_items").delete().eq("id", placeholder_id).execute()
                        )
                        if tenant_id:
                            try:
                                await sio.emit("inbox_updated", {"action": "deleted", "id": placeholder_id}, room=tenant_id)
                            except Exception:
                                pass
                    except Exception as del_err:
                        _logger.error("[paperclip-watch] failed to delete placeholder: %s", del_err)
                    placeholder_id = None
                    skill_row_already_exists = True
                    break
        except Exception as e:
            _logger.debug("[paperclip-watch] agent-row dedupe lookup failed: %s", e)

    content_type = _infer_content_type(agent_id, output)
    title = _extract_title(agent_id, task_desc, output)
    email_draft: dict | None = None

    if agent_id == "email_marketer":
        email_draft = _parse_email_draft_from_text(output)
        if email_draft:
            content_type = "email_sequence"
            # Override the title with the email subject when we got one
            if email_draft.get("subject") and email_draft["subject"] != "Untitled email":
                title = f"Email: {email_draft['subject']}"
            _logger.info(
                "[paperclip-watch] parsed email_draft for issue %s (subject=%r, has_html=%s)",
                paperclip_issue_id, email_draft.get("subject"), bool(email_draft.get("html_body")),
            )

    elif agent_id in ("content_writer", "social_manager"):
        # Detect social content (Twitter/LinkedIn variants) so the
        # frontend renders the Publish to X / Publish to LinkedIn
        # buttons. content_type=social_post is the signal.
        social = _parse_social_drafts_from_text(output)
        task_lower = (task_desc or "").lower()
        looks_like_social = (
            social is not None
            or any(k in task_lower for k in ("social", "twitter", "linkedin", "tweet", "post for x"))
        )
        if looks_like_social:
            content_type = "social_post"
            _logger.info(
                "[paperclip-watch] detected social_post for issue %s (twitter=%s linkedin=%s)",
                paperclip_issue_id,
                bool(social and social.get("twitter")),
                bool(social and social.get("linkedin")),
            )

    inbox_status = "draft_pending_approval" if content_type == "email_sequence" else "needs_review"

    if placeholder_id:
        try:
            sb = _get_supabase()
            update_data: dict = {
                "title": title[:200],
                "content": output,
                "type": content_type,
                "status": inbox_status,
                "paperclip_issue_id": paperclip_issue_id,
                "updated_at": now_iso,
            }
            if email_draft:
                update_data["email_draft"] = email_draft
            await asyncio.to_thread(
                lambda: sb.table("inbox_items").update(update_data).eq("id", placeholder_id).execute()
            )
            _logger.info("[paperclip-watch] updated placeholder %s with real content", placeholder_id)
        except Exception as e:
            _logger.error("[paperclip-watch] failed to update placeholder %s: %s", placeholder_id, e)
            placeholder_id = None

    # If placeholder update failed, save as a fresh row -- BUT only if
    # we didn't intentionally delete the placeholder because the
    # agent's skill curl already created the canonical row. Without
    # this guard we'd just re-create the duplicate we just deleted.
    if not placeholder_id and not skill_row_already_exists:
        saved = _save_inbox_item(
            tenant_id=tenant_id,
            agent=agent_id,
            title=title[:200],
            content=output,
            content_type=content_type,
            priority=priority,
            task_id=task_id,
            chat_session_id=session_id,
            status=inbox_status,
            email_draft=email_draft,
        )
        placeholder_id = saved["id"] if saved else None

    # Notify frontend so the inbox auto-refreshes the row
    if placeholder_id and tenant_id:
        try:
            await sio.emit("inbox_item_updated", {
                "id": placeholder_id,
                "agent": agent_id,
                "type": content_type,
                "title": title,
                "status": inbox_status,
                "priority": priority,
            }, room=tenant_id)
            await _notify(
                tenant_id, "inbox_new_item", title,
                body=output[:200],
                href="/inbox",
                category="inbox",
                priority=priority,
            )
        except Exception:
            pass

    # Mark the kanban task as done
    if task_id:
        try:
            sb = _get_supabase()
            sb.table("tasks").update({
                "status": "done",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", task_id).execute()
            if tenant_id:
                await sio.emit("task_updated", {
                    "id": task_id,
                    "agent": agent_id,
                    "status": "done",
                    "task": task_desc,
                }, room=tenant_id)
        except Exception:
            pass

    # Return agent to idle in the Virtual Office
    if tenant_id:
        try:
            sb = _get_supabase()
            other = sb.table("tasks").select("id").eq(
                "tenant_id", tenant_id
            ).eq("agent", agent_id).eq("status", "in_progress").limit(1).execute()
            if not other.data:
                await _emit_agent_status(tenant_id, agent_id, "idle",
                                         action="all_tasks_complete")
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
    paperclip_issue_id: str | None = None


@app.post("/api/inbox/{tenant_id}/items")
async def create_inbox_item(tenant_id: str, body: CreateInboxItem):
    """Create an inbox item — used by Paperclip agents to store their output.

    Two paths can hit this endpoint:
      1. The agent's aria-backend-api skill curl from inside Paperclip
         (the agent's own POST after generating content)
      2. The watcher's _save_inbox_item fallback when its placeholder
         update fails

    For path 1, the agent rarely populates email_draft itself, so we
    parse the content here for the same email/social structured fields
    the watcher extracts. This is what makes the Approve & Send /
    Publish to X / Publish to LinkedIn buttons render in the inbox
    regardless of which write path created the row.

    Dedupe: if paperclip_issue_id is provided AND a row already exists
    for that issue (created by the watcher's placeholder), we UPDATE
    that row instead of creating a duplicate. The agent doesn't
    currently send paperclip_issue_id, but we accept it for the future
    when the skill MD is updated.
    """
    sb = _get_supabase()

    # ── Reject confirmation/status messages ────────────────────────────
    # Agents POST "Saved successfully!" status messages as SECOND inbox
    # items, immediately after they POST the real content. Detect those
    # and short-circuit so they don't create duplicate rows.
    #
    # No length filter -- the previous 600-char cap let through long
    # confirmation messages like "✅ Email draft created and saved
    # to ARIA inbox Draft ID: 023c59e9-... Email Details: <full echo
    # of the email>" which were 1000+ chars. The pattern markers
    # alone are reliable enough: a real email draft NEVER starts
    # with ✅ or contains the literal phrase "saved to ARIA inbox".
    _content_lower = (body.content or "").strip().lower()
    _is_confirmation = (
        "saved to aria inbox" in _content_lower
        or "saved to inbox" in _content_lower
        or _content_lower.startswith(("✅", ":white_check_mark:", "[saved]", "[done]", "## task complete", "## email draft complete"))
        or "successfully saved" in _content_lower
        or "draft created and saved" in _content_lower
        or "draft id:" in _content_lower
        or _content_lower.startswith("email draft created")
    )
    if _is_confirmation:
        logging.getLogger("aria.inbox").info(
            "[inbox-create] rejecting confirmation/status message from %s "
            "(content=%r) -- not creating duplicate row",
            body.agent, body.content[:120],
        )
        return {"item": None, "skipped": "confirmation_message"}

    # Reject duplicate media writes from the legacy aria-backend-api skill
    # (the canonical row was already created by /api/media/.../generate).
    if _is_duplicate_media_write(tenant_id, body):
        return {"item": None, "skipped": "duplicate_media_write"}

    # Apply the same parser the watcher uses, so the rich fields
    # populate even when the agent skipped them in its POST body.
    title = body.title
    content_type = body.type
    status = body.status
    email_draft = body.email_draft

    # Run the parser ALWAYS for email_marketer, even if the agent
    # provided email_draft itself -- the agent's dict is often
    # incomplete (no body_html, no recipient, generic subject) and
    # our parser fills in the gaps from the content text. Merge:
    # parser fields fill any keys the agent left blank.
    if body.agent == "email_marketer":
        parsed = _parse_email_draft_from_text(body.content)
        if parsed:
            if email_draft:
                # Merge: agent's fields win where set, parser fills gaps.
                # BUT: if the agent provided a subject that looks like
                # raw HTML (e.g. '<html><body style="...'), throw it
                # away in favor of the parser's extracted subject.
                # Same for `to` -- agent's HTML-tag-attribute matches
                # can produce garbage like 'apple-system@font.com'.
                merged = dict(parsed)
                for k, v in email_draft.items():
                    if not v:
                        continue
                    if k == "subject" and isinstance(v, str) and v.lstrip().startswith("<"):
                        continue  # parser's subject wins
                    if k == "to" and isinstance(v, str) and (v.startswith("<") or "font" in v.lower()):
                        continue  # parser's to wins
                    merged[k] = v
                email_draft = merged
            else:
                email_draft = parsed
            # Normalize the type so the frontend always renders the
            # editable form (Approve & Send / Schedule / Save changes /
            # Cancel draft) instead of treating one row as static
            # 'email_draft' and another as editable 'email'. The
            # canonical type for emails the user can review/edit is
            # 'email_sequence'.
            content_type = "email_sequence"
            status = "draft_pending_approval"
            # Override title with the extracted subject ONLY if it's a
            # real subject (not HTML, not the "Untitled" fallback) AND
            # the agent's own title is generic.
            parsed_subject = email_draft.get("subject", "") if email_draft else ""
            subject_is_clean = (
                parsed_subject
                and parsed_subject != "Untitled email"
                and not parsed_subject.lstrip().startswith("<")
            )
            if subject_is_clean:
                if not title or title.lower().startswith(("draft", "marketing email", "email", "untitled")):
                    title = f"Email: {parsed_subject}"

    if body.agent in ("content_writer", "social_manager"):
        social = _parse_social_drafts_from_text(body.content)
        if social or any(k in body.content.lower()[:500] for k in ("**twitter:**", "**linkedin:**", "**x:**", "**x/twitter:**")):
            content_type = "social_post"

    # ── Best-effort dedupe based on recent activity ────────────────────
    # When the agent doesn't send paperclip_issue_id (which is most of
    # the time today), still try to avoid creating obvious duplicates
    # within the same delegation: same tenant + same agent + recent
    # creation time + same content prefix = update the existing row
    # instead of inserting another. The agent's behavior is to POST
    # the same email content twice in some cases, and without dedupe
    # we end up with two rows showing the same draft.
    try:
        # 5-minute window catches the watcher placeholder that gets
        # updated 60-90s after the agent's skill curl posts. Previously
        # set to 60s and missed by 11s in production.
        recent_window = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        recent = (
            sb.table("inbox_items")
            .select("id,content,type")
            .eq("tenant_id", tenant_id)
            .eq("agent", body.agent)
            .gte("created_at", recent_window)
            .order("created_at", desc=True)
            .limit(8)
            .execute()
        )
        for r in (recent.data or []):
            r_content = (r.get("content") or "")[:300]
            new_prefix = (body.content or "")[:300]
            # 100-char overlap (relaxed from 200) catches drafts where
            # the agent re-formatted the same email between POSTs.
            if r_content and new_prefix and len(r_content) > 50 and r_content[:100] == new_prefix[:100]:
                # Same draft already exists -- update it instead of duplicating
                update_data = {
                    "title": title,
                    "content": body.content,
                    "type": content_type,
                    "status": status,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                if email_draft:
                    update_data["email_draft"] = email_draft
                sb.table("inbox_items").update(update_data).eq("id", r["id"]).execute()
                logging.getLogger("aria.inbox").info(
                    "[inbox-create] merged duplicate POST into existing row %s "
                    "(agent=%s, same content prefix)", r["id"], body.agent,
                )
                item_data = {"id": r["id"], "tenant_id": tenant_id, **update_data}
                if tenant_id:
                    try:
                        await sio.emit("inbox_updated", {"action": "updated", "item": item_data}, room=tenant_id)
                    except Exception:
                        pass
                return {"item": item_data, "deduped": True}
    except Exception as e:
        logging.getLogger("aria.inbox").debug("[inbox-create] recent-row dedupe lookup failed: %s", e)

    row = {
        "tenant_id": tenant_id,
        "title": title,
        "content": body.content,
        "type": content_type,
        "agent": body.agent,
        "priority": body.priority,
        "status": status,
    }
    if email_draft:
        row["email_draft"] = email_draft
    if body.paperclip_issue_id:
        row["paperclip_issue_id"] = body.paperclip_issue_id

    # Dedupe with the watcher's placeholder when we have an issue id
    item = None
    if body.paperclip_issue_id:
        try:
            existing = (
                sb.table("inbox_items")
                .select("id")
                .eq("tenant_id", tenant_id)
                .eq("paperclip_issue_id", body.paperclip_issue_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                placeholder_id = existing.data[0]["id"]
                update_data = {k: v for k, v in row.items() if k not in ("tenant_id",)}
                update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
                sb.table("inbox_items").update(update_data).eq("id", placeholder_id).execute()
                item = {"id": placeholder_id, **row}
                logging.getLogger("aria.inbox").info(
                    "[inbox-create] updated existing placeholder %s for paperclip_issue_id=%s",
                    placeholder_id, body.paperclip_issue_id,
                )
        except Exception as e:
            logging.getLogger("aria.inbox").warning(
                "[inbox-create] dedupe lookup failed: %s -- inserting fresh row", e,
            )

    if item is None:
        result = sb.table("inbox_items").insert(row).execute()
        item = result.data[0] if result.data else None

    # Emit real-time notification
    if item and tenant_id:
        await sio.emit("inbox_updated", {"action": "created", "item": item}, room=tenant_id)
        # Create notification
        try:
            sb.table("notifications").insert({
                "tenant_id": tenant_id,
                "title": f"New from {body.agent}: {title}",
                "body": body.content[:200],
                "category": "inbox",
                "href": "/inbox",
            }).execute()
        except Exception:
            pass

    # If this is a finished media row, kill any leftover "Media is working
    # on..." placeholder for this tenant. Covers the case where the agent
    # used the legacy aria-backend-api skill instead of /api/media/.../generate.
    if body.agent == "media" and item:
        await _cleanup_media_placeholder(tenant_id, item.get("id"))

    return {"item": item}


def _is_duplicate_media_write(tenant_id: str, body: "CreateInboxItem") -> bool:
    """Reject duplicate media writes from the legacy aria-backend-api skill.

    When the Media Designer agent has both the new instructions AND the old
    aria-backend-api skill enabled, it hits TWO endpoints per image request:
    /api/media/.../generate (creates the canonical row with the rendered PNG)
    and /api/inbox/.../items (creates a text summary). The second is noise.

    If a media row for this tenant was created in the last 60s, treat any
    new media POST to /api/inbox/ as a duplicate. The 60s window is wide
    enough to cover Pollinations latency + agent reply lag, narrow enough
    that legitimate back-to-back requests still go through.
    """
    if body.agent != "media":
        return False
    try:
        from datetime import timedelta
        sb = _get_supabase()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        existing = sb.table("inbox_items").select("id").eq("tenant_id", tenant_id).eq(
            "agent", "media"
        ).neq("status", "processing").gte("created_at", cutoff).limit(1).execute()
        return bool(existing.data)
    except Exception:
        return False


async def _cleanup_media_placeholder(tenant_id: str, keep_id: str | None) -> None:
    """Delete any 'processing' media inbox row for this tenant other than keep_id.

    Called whenever a finished media row is written via either path
    (/api/media/.../generate or /api/inbox/.../items) so the user doesn't
    see a stale 'Media is working on...' placeholder lingering after the
    real image arrives. Emits inbox_item_deleted so the frontend updates
    in real time.
    """
    if not tenant_id:
        return
    try:
        sb = _get_supabase()
        q = sb.table("inbox_items").select("id").eq("tenant_id", tenant_id).eq(
            "agent", "media"
        ).eq("status", "processing")
        if keep_id:
            q = q.neq("id", keep_id)
        rows = q.execute().data or []
        for r in rows:
            pid = r.get("id")
            if not pid:
                continue
            try:
                sb.table("inbox_items").delete().eq("id", pid).execute()
            except Exception:
                continue
            try:
                await sio.emit("inbox_item_deleted", {"id": pid}, room=tenant_id)
            except Exception:
                pass
    except Exception:
        pass


# ─── CEO Actions ───

# Action descriptions are static (built from ACTION_REGISTRY at import time)
# so cache at module load instead of rebuilding the list of strings on every
# CEO chat call. Saves ~1ms + a bunch of garbage allocations per request.
try:
    from backend.ceo_actions import get_action_descriptions as _ceo_action_descriptions_fn
    _CEO_ACTION_DESCRIPTIONS = _ceo_action_descriptions_fn()
except Exception:
    _CEO_ACTION_DESCRIPTIONS = ""


def _get_ceo_action_descriptions() -> str:
    """Get compact action descriptions for CEO system prompt (cached)."""
    return _CEO_ACTION_DESCRIPTIONS


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
_CEO_MD = _CEO_MD_FULL[:4000]  # Need enough to include all delegation rules + sub-agent list. Prompt caching keeps cost low on repeat calls.
# Sub-agent role MDs — used by the CEO chat handler to build a one-line
# capabilities cheat sheet on the FIRST chat turn only (see _build_sub_agent_context).
# Skill MDs are NOT loaded here — BaseAgent.run() loads them per-agent at
# runtime via backend.agents.base._load_agent_skill().
_AGENT_MDS = {}
for _f in _AGENTS_DIR.glob("*.md"):
    if _f.stem != "ceo":
        _AGENT_MDS[_f.stem] = _f.read_text(encoding="utf-8")

# Shared `re` alias for all module-level compiled patterns below. Imported
# once under a short name so the pre-compiled regex blocks don't each need
# their own import line. Kept at module scope because these patterns run on
# every CEO chat call.
import re as _re_crm

# Pre-compiled regex patterns for CEO chat delegate/action block parsing.
# Compiling these per-request is cheap but not free, and the chat handler
# is the hottest non-streaming endpoint. Module-level wins about 50us per
# call without changing semantics.
_DELEGATE_BLOCK_RE = _re_crm.compile(r"```delegate\s*\n(.*?)\n```", _re_crm.DOTALL)
_ACTION_BLOCK_RE = _re_crm.compile(r"```action\s*\n(.*?)\n```", _re_crm.DOTALL)

# CRM context heuristic — module-level regexes (compiled once, not per request)
# so the CEO chat handler can decide in O(1) whether to inject the CRM block.
# Word-boundary matching avoids substring false positives ("ideal"→"deal",
# "leader"→"lead", "calling"→"call").
_CRM_TRIGGER_PHRASES = (
    "send email to", "reach out to", "follow up with",
    "the contact", "this contact", "all contacts", "my contacts",
    "the company", "this company", "all companies", "my companies",
    "the deal", "this deal", "all deals", "my deals",
    "the lead", "this lead", "all leads", "my leads",
    "crm",
)
_CRM_NOUN_RE = _re_crm.compile(
    r"\b(contacts?|compan(?:y|ies)|deals?|leads?|prospects?|pipelines?)\b"
)
_CRM_VERB_RE = _re_crm.compile(
    r"\b(create|add|update|delete|remove|find|show|list|search|email|call)\b"
)

# In-memory chat cache with LRU eviction (max 100 sessions)
_chat_sessions: dict[str, list[dict]] = {}
_MAX_CACHED_SESSIONS = 100

# Per-session asyncio.Lock so two concurrent ceo_chat requests for the
# same session_id don't interleave their session.append(user) /
# session.append(assistant) calls and corrupt the conversation history.
# Created lazily on first use; lifecycle matches _chat_sessions.
_chat_session_locks: dict[str, "asyncio.Lock"] = {}


def _get_chat_session_lock(session_id: str) -> "asyncio.Lock":
    """Return the asyncio.Lock for this session_id, creating one if needed.

    Safe to call from any coroutine on the main event loop -- dict.setdefault
    is atomic in CPython for the GIL-protected modify-or-create case.
    """
    lock = _chat_session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_session_locks[session_id] = lock
    return lock


def _evict_chat_sessions():
    """Remove oldest sessions if cache exceeds max size."""
    if len(_chat_sessions) > _MAX_CACHED_SESSIONS:
        excess = len(_chat_sessions) - _MAX_CACHED_SESSIONS
        for key in list(_chat_sessions.keys())[:excess]:
            del _chat_sessions[key]
            # Drop the lock too if no longer needed
            _chat_session_locks.pop(key, None)


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


def _summarize_ceo_assistant_message(content: str) -> str:
    """Compress a CEO assistant message into a one-line summary for history.

    The model is autoregressive and will copy its own prior outputs verbatim
    if it sees them in the context — that's the root cause of the
    'CEO returns a full GTM strategy review on every message' bug, and the
    'CEO uses last message's subject for a new request' bug. By replacing
    each prior CEO turn with a short tag instead of the raw content, the
    model knows the conversation flow exists but has nothing concrete to
    plagiarise.

    Args:
        content: the full CEO response text from a previous turn

    Returns:
        A bracketed one-line summary like '[CEO previously delegated to media]'
        or '[CEO: Hi! How can I help you today?]'
    """
    import re

    if not content:
        return "[CEO previously responded]"

    # Highest-signal patterns first: delegations and actions
    if "```delegate" in content:
        match = re.search(r'"agent"\s*:\s*"([\w_]+)"', content)
        agent = match.group(1) if match else "an agent"
        return f"[CEO previously delegated to {agent}]"
    if "```action" in content:
        match = re.search(r'"action"\s*:\s*"([\w_]+)"', content)
        action = match.group(1) if match else "an action"
        return f"[CEO previously executed action: {action}]"

    # Plain prose: strip markdown and take just the first non-empty line
    cleaned = content.strip()
    cleaned = re.sub(r"```[\s\S]*?```", "", cleaned)        # fenced code blocks
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)  # ATX headers
    cleaned = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", cleaned)  # bold/italic
    cleaned = re.sub(r"^\s*[-*]\s+", "", cleaned, flags=re.MULTILINE)  # bullets
    cleaned = re.sub(r"^\s*\d+\.\s+", "", cleaned, flags=re.MULTILINE)  # numbered

    first_line = ""
    for line in cleaned.split("\n"):
        line = line.strip()
        if line and line not in ("---", "***"):
            first_line = line
            break

    if not first_line:
        return "[CEO previously responded]"
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    return f"[CEO: {first_line}]"


def _format_history_message(m: dict, *, keep_verbatim: bool = False) -> str:
    """Render one prior message for the history block.

    User messages stay verbatim so the model knows what was actually asked.
    CEO assistant messages get summarised to break verbatim-copying priming
    UNLESS keep_verbatim is True, which the caller sets for the immediately
    prior CEO turn so follow-ups like 'go with number 1' have the actual
    options in context.
    """
    if m.get("role") == "user":
        return f"User: {m.get('content', '')}"
    content = m.get("content", "")
    if keep_verbatim:
        return f"CEO (previous response — keep this in mind for the user's reply): {content}"
    return _summarize_ceo_assistant_message(content)


# Threshold for keeping the most recent CEO response verbatim. Anything under
# this size is treated as a conversational reply (clarifying question, brief
# delegation announcement, short status update) and the next user message
# almost certainly refers back to its content. Anything over this size is a
# long-form artifact (GTM review, multi-section report) and including it
# verbatim primes the model to plagiarise its own prior output, which was
# the original reason _summarize_ceo_assistant_message exists.
_KEEP_VERBATIM_MAX_CHARS = 2000


def _last_assistant_index(history: list[dict]) -> int | None:
    """Index in history of the most recent assistant (CEO) message, or None."""
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "assistant":
            return i
    return None


@app.post("/api/ceo/chat")
async def ceo_chat(body: CEOChatMessage):
    """Send a message to the CEO agent. The CEO reads its own .md file and all sub-agent .md files,
    then responds and may delegate tasks to sub-agents.

    Per-session asyncio.Lock prevents two concurrent requests for the same
    session_id from interleaving their session.append() calls and
    corrupting history. Without the lock, a user double-clicking send or
    having the chat open in two tabs could send 2 ceo_chat() calls that
    both call session.append(user) -> call_claude() -> session.append(assistant)
    in interleaved order, producing garbled history and possibly duplicate
    messages saved to Supabase.
    """
    lock = _get_chat_session_lock(body.session_id)
    async with lock:
        return await _ceo_chat_impl(body)


async def _ceo_chat_impl(body: CEOChatMessage):
    from backend.tools.claude_cli import call_claude, MODEL_HAIKU
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

    # Inject the sub-agent capabilities cheat sheet ONLY on the first message
    # of a session. Subsequent turns rely on the CEO already knowing its team
    # from earlier in the conversation. This used to fire every turn — the old
    # comment claimed it was first-message-only but the code didn't actually
    # check is_first_message. Saves ~1k tokens per non-first chat call.
    # On follow-up turns we leave a single line so the CEO doesn't forget the
    # roster entirely if the conversation history scrolls off.
    if is_first_message:
        sub_agent_context = "\n".join(
            f"- {name}: {content[:200].replace(chr(10), ' ')}"
            for name, content in _AGENT_MDS.items()
        )
    else:
        sub_agent_context = (
            "Your team: content_writer, email_marketer, social_manager, ad_strategist, media."
        )

    # Load tenant config once — reused for business context + integration checks.
    # tenant_id was already set above from body.tenant_id; don't reassign here.
    business_context = ""
    tc = None
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
        except Exception as e:
            # Log loudly so we know when the CEO is replying without
            # business context (would otherwise be a silent generic-advice bug)
            logging.getLogger("aria.ceo_chat").warning(
                "[ceo-chat] get_tenant_config(%s) failed: %s -- CEO will reply without business context",
                tenant_id, e,
            )

    # Check connected integrations for this tenant (reuse tc from above).
    # Compact one-line notes only — the CEO doesn't need a paragraph per
    # integration. Saves ~300 tokens per chat call.
    integration_lines = []
    if tenant_id and tc:
        try:
            if tc.integrations.google_access_token or tc.integrations.google_refresh_token:
                integration_lines.append(
                    f"Gmail connected ({tc.owner_email}) — to send mail, delegate to email_marketer "
                    f'with a task starting "SEND:" and include the full recipient email.'
                )
            if tc.integrations.twitter_access_token or tc.integrations.twitter_refresh_token:
                handle = tc.integrations.twitter_username or "user"
                integration_lines.append(
                    f"X/Twitter connected (@{handle}) — for social posts, delegate to social_manager. "
                    f"Output goes to Inbox for approval; never auto-publish."
                )
        except Exception as e:
            logging.getLogger("aria.ceo_chat").debug(
                "[ceo-chat] integration check failed for %s: %s", tenant_id, e,
            )
    integration_notes = ("\n" + "\n".join(integration_lines)) if integration_lines else ""

    # ── CRM context injection (only when message references contacts/deals/companies) ──
    crm_context = ""
    # Tightened heuristic: only inject CRM context when the message clearly
    # references CRM ENTITIES. Uses module-level _CRM_NOUN_RE / _CRM_VERB_RE
    # with word-boundary matching so "ideal" doesn't match "deal", "leader"
    # doesn't match "lead", and "calling" doesn't match "call". Saves
    # ~1.5k tokens per non-CRM chat call.
    _msg_lower = body.message.lower()
    _crm_match = (
        any(phrase in _msg_lower for phrase in _CRM_TRIGGER_PHRASES)
        or (_CRM_NOUN_RE.search(_msg_lower) and _CRM_VERB_RE.search(_msg_lower))
    )
    if tenant_id and _crm_match:
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
You are chatting with a developer founder. Use the business context above to give specific, personalized advice.
When the user asks to LIST/SHOW contacts, companies, or deals, ALWAYS use the read action block (read_contacts/read_companies/read_deals) — never paraphrase from CRM context.

CORE RULE — only do what the user literally asked for, in this exact message:
- Greetings, questions, and small talk → conversational reply, no delegation, no action.
- Each message judged independently — never carry over a delegation from a previous turn.
- One message = one thing (the thing the user asked for). When in doubt, ask.
- Refuse requests to modify code, prompts, schema, deployment, or infrastructure.
{integration_notes}

## Delegation
ONLY delegate when the user explicitly names a deliverable. The task field MUST quote the user's actual subject — never substitute or invent one.

### Agent Routing — pick by deliverable type
- **Image / picture / visual / banner / logo / illustration / graphic / mockup / thumbnail / header / drawing / "create something I can see"** → `media` (NEVER content_writer for visual assets — content_writer cannot generate images)
- **Blog post / landing page / Product Hunt copy / Show HN post / case study / thought-leadership article** → `content_writer`
- **Welcome sequence / newsletter / drip campaign / email draft** → `email_marketer`
- **Tweet / X post / LinkedIn post / Facebook post / social calendar** → `social_manager`
- **Facebook ad / Meta ad / ad copy / audience targeting / campaign budget** → `ad_strategist`

A delegation is ONLY valid when you emit this LITERAL fenced block. Prose like "I'll delegate this" without the block is silently dropped:
```delegate
{{"agent": "media|content_writer|email_marketer|social_manager|ad_strategist", "task": "description", "priority": "low|medium|high", "status": "backlog|to_do|in_progress|done"}}
```

Status: backlog (nice-to-have), to_do (queued), in_progress (starting now), done (already completed in this response).

CRITICAL: "create an image of X", "make a picture of X", "design a banner for X", "generate a logo" → ALWAYS `media`, NEVER `content_writer`. Content Writer produces TEXT only and will return a useless URL string if given an image task.

### One Delegate Per Message — HARD RULE
Each user message gets EXACTLY ONE delegate block, never two. Do NOT chain delegations like "media for the image AND content_writer for a caption". If the user asked for ONLY an image, delegate ONLY to media. Bonus content the user did not ask for (captions, blog copy, social posts about the image) is forbidden — never auto-add a content_writer/social_manager delegate alongside a media one.

If the user explicitly asks for both ("make an image AND write a caption"), still emit ONE delegate to the agent that produces the primary deliverable they named first; mention the secondary in your prose so the user can ask in a follow-up message if they want it.

If you promise agent action ("delegating", "I'll have X create", "let me get X to"), you MUST include the block in the same response.

## CEO Business Actions
Include an action block when executing business operations:
```action
{{"action": "action_name", "params": {{"key": "value"}}}}
```

Available actions:
{_CEO_ACTION_DESCRIPTIONS}

Action rules:
- Only execute actions the user explicitly requested — never chain or auto-add.
- UPDATE/DELETE/PUBLISH/SEND always require user confirmation before the block runs.
- CREATE can proceed when intent is clear; ask if data is missing.
- The system appends the formatted result automatically — write a brief intro ("Here are your contacts:") and include the block. Do NOT fabricate results.

Token efficiency:
- If the user asks to send/post content that ALREADY EXISTS in the Inbox, reference it and delegate with "USE EXISTING:" prefix instead of regenerating.
- Never auto-publish — all content goes to Inbox for approval.

Keep responses concise and actionable. You are their Chief Marketing Strategist."""

    # Build conversation for Claude — prior turns are summarised, NOT included
    # verbatim. The model is autoregressive: when it sees its own prior outputs
    # it will copy them verbatim, which causes 'CEO returns the same GTM
    # strategy review on every message' and 'CEO uses the previous turn's
    # subject for a new unrelated request'. By replacing each prior CEO turn
    # with a short tag like '[CEO previously delegated to media]', the model
    # still knows there was a back-and-forth (so follow-ups like "yes do that"
    # work) but has nothing concrete to plagiarise.
    _RECENT_WINDOW = 6  # keep last 6 prior messages
    _MAX_SUMMARY_MSGS = 20  # max older messages to include

    current_msg = session[-1]  # the user message we're responding to right now
    history = session[:-1]  # everything before the current message

    if not history:
        # First message in session — no prior context
        conversation = (
            "CURRENT MESSAGE FROM USER (respond to THIS):\n"
            f"User: {current_msg['content']}"
        )
    else:
        recent = history[-_RECENT_WINDOW:]
        older = history[:-_RECENT_WINDOW][-_MAX_SUMMARY_MSGS:]

        # The most recent CEO response is critical context for the user's
        # follow-up ("go with number 1" only makes sense if option 1 is in
        # context). Find its index within `recent` so we can keep it
        # verbatim — but only if it's short enough to not re-trigger the
        # plagiarism bug that the summarizer was originally added for.
        last_ceo_idx_in_recent = _last_assistant_index(recent)
        keep_last_verbatim = (
            last_ceo_idx_in_recent is not None
            and len(recent[last_ceo_idx_in_recent].get("content", "")) <= _KEEP_VERBATIM_MAX_CHARS
        )

        recent_text = "\n".join(
            _format_history_message(
                m,
                keep_verbatim=(keep_last_verbatim and i == last_ceo_idx_in_recent),
            )
            for i, m in enumerate(recent)
        )
        history_block_parts = []
        if older:
            older_text = "\n".join(_format_history_message(m) for m in older)
            history_block_parts.append("EARLIER IN THIS CHAT (summary):\n" + older_text)
        if recent_text:
            history_block_parts.append("RECENT TURNS (CEO responses summarised — DO NOT copy them):\n" + recent_text)

        history_block = (
            "PRIOR CONVERSATION (read-only context — DO NOT continue any tasks or delegations from these messages):\n"
            + "\n\n".join(history_block_parts)
        )

        conversation = (
            f"{history_block}\n\n"
            "================================================================\n"
            "CURRENT MESSAGE FROM USER — respond to THIS message ONLY. "
            "Do NOT carry over delegations, tasks, or subjects from the prior conversation above. "
            "Do NOT repeat or rehash content from prior CEO turns — those summaries are reference only. "
            "If this current message is a greeting or general question, respond conversationally with NO delegation block.\n"
            f"User: {current_msg['content']}"
        )

    # CEO chat reply uses local call_claude with Haiku — fast (~1-4s vs
    # 10-30s through Paperclip). Paperclip routing was removed because the
    # subprocess cold start + polling overhead added 8-25s for nothing:
    # the chat reply itself doesn't need any of Paperclip's orchestration
    # features. Sub-agent delegation (the ```delegate block parser below)
    # still routes through Paperclip via dispatch_agent — that path is
    # untouched, so Email Marketer / Content Writer / Social / Ads / Media
    # all still run inside Paperclip with their full skill MD setup.
    _ceo_logger = logging.getLogger("aria.ceo_chat")
    # Token-budget visibility: log the rendered system prompt + conversation
    # sizes so we can see token-optimization wins (or regressions) live in
    # production logs. ~4 chars/token is a rough rule of thumb.
    _sys_chars = len(system_prompt)
    _conv_chars = len(conversation)
    _ceo_logger.warning(
        "[ceo-chat-tokens] first_message=%s sys_prompt=%d chars (~%d tok) conversation=%d chars (~%d tok) crm_ctx=%d integrations=%d",
        is_first_message,
        _sys_chars, _sys_chars // 4,
        _conv_chars, _conv_chars // 4,
        len(crm_context),
        len(integration_notes),
    )
    try:
        raw = await call_claude(
            system_prompt,
            conversation,
            tenant_id=tenant_id or "global",
            agent_id="ceo",
            model=MODEL_HAIKU,
        )
    except Exception as exc:
        import traceback
        _ceo_logger.error(f"CEO chat error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        # Don't leak raw exception messages (may include API keys, connection
        # strings, JWT bits). Generic message + log the real error.
        raw = (
            "I had trouble processing that just now. Please try again in a moment "
            "-- if it keeps failing, check the backend logs for the error details."
        )

    # Check for forbidden requests. The check is intentionally narrow:
    # we only override the CEO's reply if (a) the user message clearly
    # asks for a forbidden action AND (b) the CEO's response doesn't
    # already contain a refusal phrase. The double gate prevents the
    # naive substring match from nuking legitimate replies that happen
    # to mention sensitive words ("don't touch the database schema").
    from backend.ceo_actions import is_forbidden_request, REFUSAL_MESSAGE
    if is_forbidden_request(body.message):
        refusal_markers = ("can't", "cannot", "don't have access", "i'm not able", "i won't")
        raw_lower = raw.lower()
        if not any(marker in raw_lower for marker in refusal_markers):
            raw = REFUSAL_MESSAGE

    # Parse delegation blocks
    delegations = []
    clean_response = raw
    if "```delegate" in raw:
        for block in _DELEGATE_BLOCK_RE.findall(raw):
            d = _parse_codeblock_json(block, "delegate")
            if d and d.get("agent") in ("content_writer", "email_marketer", "social_manager", "ad_strategist", "media"):
                delegations.append(d)
        clean_response = _DELEGATE_BLOCK_RE.sub("", raw).strip()

    # Defensive: if the model promised delegation in prose but forgot the block,
    # log loudly so we can see when the prompt isn't being followed.
    if not delegations:
        prose_promises = ("delegating", "i'll delegate", "i will delegate", "let me delegate",
                          "i'll have", "i will have", "having our", "media designer to create",
                          "media designer to generate")
        raw_lower = raw.lower()
        if any(phrase in raw_lower for phrase in prose_promises):
            logging.getLogger("aria.ceo_chat").warning(
                "CEO promised delegation in prose but emitted no ```delegate block. Raw response: %s",
                raw[:500],
            )

    # Parse CEO action blocks
    ceo_actions = []
    if "```action" in clean_response:
        for block in _ACTION_BLOCK_RE.findall(clean_response):
            a = _parse_codeblock_json(block, "action")
            if a and a.get("action"):
                ceo_actions.append(a)
        clean_response = _ACTION_BLOCK_RE.sub("", clean_response).strip()

    # Execute non-confirmation actions immediately; queue confirmations for frontend
    action_results = []
    pending_confirmations = []
    if ceo_actions and tenant_id:
        from backend.ceo_actions import execute_action, ACTION_REGISTRY
        for a in ceo_actions:
            action_name = a.get("action", "")
            params = a.get("params", {})
            try:
                result = await execute_action(tenant_id, action_name, params, confirmed=False)
            except Exception as exec_exc:
                # Don't let an action handler crash kill the whole chat
                # response after the CEO has already replied. Log it,
                # surface a sanitized error to the user, and keep going.
                logging.getLogger("aria.ceo_chat.actions").error(
                    "[ceo-action] %s raised: %s", action_name, exec_exc, exc_info=True,
                )
                action_results.append({
                    "status": "error",
                    "action": action_name,
                    "message": f"Action {action_name!r} failed -- check backend logs.",
                })
                continue
            if result.get("status") == "needs_confirmation":
                pending_confirmations.append(result)
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

        # Enrich the task description with CRM contact info before
        # dispatch. The CEO's CRM-context heuristic doesn't fire on
        # phrases like "create marketing email for Hanz" (no CRM noun),
        # so the CEO has no email address to pass through to the
        # email_marketer. This helper looks up the CRM directly and
        # appends matched contacts (with emails) to the task_desc, so
        # the agent can use the right recipient instead of a placeholder.
        if tenant_id and agent_id in ("email_marketer", "social_manager", "ad_strategist", "content_writer"):
            task_desc = _enrich_task_desc_with_crm(task_desc, tenant_id)

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
        # Execute agent — route through Paperclip if connected, else local fallback.
        # Loud logging here is critical: a silent failure inside this block was
        # the previous bug and made it look like 'Paperclip orchestration is not
        # being used' when really an import or task-creation error was being
        # swallowed by a bare except.
        _dispatch_logger = logging.getLogger("aria.ceo_chat.dispatch")
        try:
            connected = paperclip_connected()
            paperclip_id = get_paperclip_agent_id(agent_id) if connected else None
            _dispatch_logger.warning(
                "[ceo-dispatch] agent=%s paperclip_connected=%s paperclip_id=%s",
                agent_id, connected, paperclip_id,
            )

            if connected and paperclip_id:
                _dispatch_logger.warning(
                    "[ceo-dispatch] routing %s through Paperclip (id=%s) with active inbox watcher",
                    agent_id, paperclip_id,
                )
                # _dispatch_paperclip_and_watch_to_inbox handles dispatch +
                # placeholder + adaptive polling + inbox write in one task,
                # so the result lands in the inbox within ~1-2s of the agent
                # finishing instead of waiting up to 5s for the global poller.
                # Wrapped in _safe_background so any crash inside the task
                # gets logged instead of disappearing as "Task exception
                # was never retrieved".
                _safe_background(
                    _dispatch_paperclip_and_watch_to_inbox(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        task_desc=task_desc,
                        session_id=body.session_id,
                        task_id=saved_tasks[-1]["id"] if saved_tasks else None,
                        priority=d.get("priority", "medium"),
                    ),
                    label=f"paperclip-watch-{agent_id}",
                )
            else:
                _dispatch_logger.warning(
                    "[ceo-dispatch] FALLING BACK to local for %s "
                    "(connected=%s, paperclip_id=%s) — set PAPERCLIP_*_KEY env vars "
                    "in .env to enable Paperclip routing",
                    agent_id, connected, paperclip_id,
                )
                agent_module = AGENT_REGISTRY.get(agent_id)
                if agent_module:
                    _safe_background(
                        _run_agent_to_inbox(
                            agent_module, agent_id, tenant_id, task_desc,
                            body.session_id,
                            saved_tasks[-1]["id"] if saved_tasks else None,
                            d.get("priority", "medium"),
                        ),
                        label=f"local-agent-{agent_id}",
                    )
                else:
                    _dispatch_logger.error(
                        "[ceo-dispatch] no agent_module for %s in AGENT_REGISTRY", agent_id,
                    )
        except Exception as _disp_exc:
            import traceback
            _dispatch_logger.error(
                "[ceo-dispatch] FAILED to dispatch %s: %s\n%s",
                agent_id, _disp_exc, traceback.format_exc(),
            )

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
