"""ARIA FastAPI Server — webhooks, chat, agent management, dashboard API."""
from __future__ import annotations

import asyncio
import logging
import os
import re
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


async def _followup_nudge_loop():
    """Background loop: draft 7-day follow-up nudges for stale threads.

    Sweeps every 6 hours. For each tenant, finds email_threads where
    status=='awaiting_reply' and last_message_at is older than 7 days.
    For each such thread, if there isn't ALREADY a recent nudge draft
    sitting in the inbox (lookback: 7 days), spawn one via the same
    agent pipeline the manual "Generate Reply Draft" button uses, with
    custom_instructions biased toward a gentle, low-pressure nudge.

    Drafts land as draft_pending_approval in the inbox so the user
    still controls what actually sends — this is the FIRST recurring
    agent-driven cron in ARIA, and keeping human-in-the-loop is why.
    """
    _log = logging.getLogger("aria.followup_nudge")
    INTERVAL_SECS = 6 * 60 * 60  # every 6 hours
    STALE_DAYS = 7
    MAX_PER_TENANT = 3  # cap nudges per sweep to avoid burst sending

    # On container start, wait a minute before the first sweep so the
    # rest of the boot path settles. Without this the loop can race
    # Supabase init during rebuilds.
    await asyncio.sleep(60)

    while True:
        try:
            sb = _get_supabase()
            now = datetime.now(timezone.utc)
            stale_cutoff = (now - timedelta(days=STALE_DAYS)).isoformat()
            nudge_recent_cutoff = (now - timedelta(days=STALE_DAYS)).isoformat()

            stale = (
                sb.table("email_threads")
                .select("id, tenant_id, contact_email, subject, last_message_at")
                .eq("status", "awaiting_reply")
                .lt("last_message_at", stale_cutoff)
                .order("last_message_at", desc=False)
                .limit(100)
                .execute()
            )
            threads_by_tenant: dict[str, list[dict]] = {}
            for t in stale.data or []:
                threads_by_tenant.setdefault(t["tenant_id"], []).append(t)

            drafted = 0
            for tenant_id, threads in threads_by_tenant.items():
                # Dedupe: skip threads that already have a draft/nudge
                # inbox row created within the stale window so we don't
                # spam the user with duplicate drafts.
                try:
                    existing = (
                        sb.table("inbox_items")
                        .select("email_draft")
                        .eq("tenant_id", tenant_id)
                        .eq("agent", "email_marketer")
                        .gte("created_at", nudge_recent_cutoff)
                        .limit(100)
                        .execute()
                    )
                    threaded_ids = set()
                    for r in existing.data or []:
                        d = r.get("email_draft") or {}
                        if isinstance(d, dict) and d.get("reply_to_thread_id"):
                            threaded_ids.add(d["reply_to_thread_id"])
                except Exception:
                    threaded_ids = set()

                queued = 0
                for t in threads:
                    if queued >= MAX_PER_TENANT:
                        break
                    if t["id"] in threaded_ids:
                        continue
                    try:
                        await generate_draft_reply(
                            tenant_id,
                            DraftReplyRequest(
                                thread_id=t["id"],
                                custom_instructions=(
                                    "7-day follow-up nudge. Be brief, warm, and low-pressure — "
                                    "reference the original subject, acknowledge they may be busy, "
                                    "and ask one simple next-step question. Avoid salesy language."
                                ),
                            ),
                        )
                        drafted += 1
                        queued += 1
                        # Tiny stagger so we don't hammer the model all at once.
                        await asyncio.sleep(2)
                    except Exception as e:
                        _log.debug("nudge draft failed (tenant=%s thread=%s): %s",
                                   tenant_id, t["id"], e)

            if drafted:
                _log.info("Follow-up nudges: drafted %d stale-thread reminders", drafted)
        except Exception as e:
            _log.warning("Follow-up nudge sweep failed: %s", e)

        await asyncio.sleep(INTERVAL_SECS)


async def _content_repurpose_loop():
    """Weekly sweep: surface aging evergreen content for a refresh.

    Every 7 days, looks at content_library_entries for each tenant and
    picks the oldest blog_post / article / landing_page rows that are
    more than 90 days old and haven't been flagged for refresh in the
    last 30 days. Creates a low-key inbox suggestion row (type=
    'refresh_suggestion') the user can act on — does NOT auto-dispatch
    the content_writer, which would feel spammy.

    The suggestion body is a short markdown block: title of the aging
    asset + a one-liner on why we flagged it. User sees it in the
    inbox and can ask the CEO to refresh a specific one.
    """
    _log = logging.getLogger("aria.content_repurpose")
    INTERVAL_SECS = 7 * 24 * 60 * 60  # every 7 days
    AGING_DAYS = 90
    DEDUP_DAYS = 30
    MAX_SUGGESTIONS_PER_SWEEP = 3

    # Wait 10 minutes after boot so the loop doesn't pile on during
    # rebuilds. On a cold VPS restart the earlier loops take precedence.
    await asyncio.sleep(600)

    while True:
        try:
            sb = _get_supabase()
            now = datetime.now(timezone.utc)
            age_cutoff = (now - timedelta(days=AGING_DAYS)).isoformat()
            dedup_cutoff = (now - timedelta(days=DEDUP_DAYS)).isoformat()

            # Unique tenants with any aging content
            tenants_res = (
                sb.table("content_library_entries")
                .select("tenant_id")
                .in_("type", ["blog_post", "article", "landing_page"])
                .lt("created_at", age_cutoff)
                .limit(500)
                .execute()
            )
            tenant_ids = list({r["tenant_id"] for r in (tenants_res.data or []) if r.get("tenant_id")})

            suggestions_created = 0
            for tenant_id in tenant_ids:
                try:
                    # Find already-suggested entries so we don't re-flag
                    # the same asset within DEDUP_DAYS.
                    existing_res = (
                        sb.table("inbox_items")
                        .select("metadata, created_at")
                        .eq("tenant_id", tenant_id)
                        .eq("type", "refresh_suggestion")
                        .gte("created_at", dedup_cutoff)
                        .execute()
                    )
                    suggested_library_ids: set = set()
                    for r in existing_res.data or []:
                        meta = r.get("metadata") or {}
                        if isinstance(meta, str):
                            try:
                                import json as _json
                                meta = _json.loads(meta)
                            except Exception:
                                meta = {}
                        lid = (meta or {}).get("library_entry_id")
                        if lid:
                            suggested_library_ids.add(lid)

                    # Oldest aging entries, up to MAX_SUGGESTIONS_PER_SWEEP.
                    entries_res = (
                        sb.table("content_library_entries")
                        .select("id, type, title, created_at")
                        .eq("tenant_id", tenant_id)
                        .in_("type", ["blog_post", "article", "landing_page"])
                        .lt("created_at", age_cutoff)
                        .order("created_at", desc=False)
                        .limit(MAX_SUGGESTIONS_PER_SWEEP + len(suggested_library_ids))
                        .execute()
                    )
                    picked = 0
                    for entry in entries_res.data or []:
                        if picked >= MAX_SUGGESTIONS_PER_SWEEP:
                            break
                        if entry["id"] in suggested_library_ids:
                            continue
                        title = (entry.get("title") or "Untitled")[:120]
                        age_days = (now - datetime.fromisoformat(
                            entry["created_at"].replace("Z", "+00:00")
                        )).days
                        body = (
                            f"**{title}** was published {age_days} days ago. "
                            "Facts, examples, or positioning may be stale — consider asking the "
                            "Content Writer to refresh it.\n\n"
                            f"_To refresh, open CEO Chat and say:_ "
                            f"**Refresh the blog post titled \"{title}\" — update any stale facts and modernize the voice.**"
                        )
                        from backend.services import inbox as inbox_service
                        # metadata.library_entry_id is what the dedup
                        # lookup above matches against, so NEVER skip it
                        # here — without it every sweep would re-suggest
                        # the same aging rows.
                        inbox_service.create_item(
                            tenant_id=tenant_id,
                            agent="content_writer",
                            type="refresh_suggestion",
                            title=f"Refresh candidate: {title}",
                            content=body,
                            status="ready",
                            priority="low",
                            metadata={
                                "library_entry_id": entry["id"],
                                "library_entry_type": entry.get("type"),
                                "aging_days": age_days,
                            },
                        )
                        suggestions_created += 1
                        picked += 1
                except Exception as e:
                    _log.debug("repurpose sweep tenant=%s failed: %s", tenant_id, e)

            if suggestions_created:
                _log.info(
                    "Content repurpose: surfaced %d aging-asset suggestions across %d tenants",
                    suggestions_created, len(tenant_ids),
                )
        except Exception as e:
            _log.warning("Content repurpose sweep failed: %s", e)

        await asyncio.sleep(INTERVAL_SECS)


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
            imported = await poll_completed_issues(sio)
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


# ─── Inbox Dedup / Sanitization Constants ────────────────────────────────
#
# Paperclip skill curls occasionally POST with the display form of the
# agent slug ("email-marketer", "Email Marketer", "media-designer") or
# the legacy `media_designer` underscore. Everything else in the system
# (watcher placeholders, CEO dispatch, UI color/name maps) uses the
# canonical underscore slug. This alias map normalizes incoming writes.
# Must be defined BEFORE _NON_CANONICAL_AGENT_SLUGS below (which derives
# its filter from .keys()) — Python evaluates module-level statements
# top-to-bottom at import time.
_AGENT_SLUG_ALIASES: dict[str, str] = {
    "email-marketer": "email_marketer",
    "content-writer": "content_writer",
    "social-manager": "social_manager",
    "ad-strategist": "ad_strategist",
    "media-designer": "media",
    "media_designer": "media",
    "email marketer": "email_marketer",
    "content writer": "content_writer",
    "social manager": "social_manager",
    "ad strategist": "ad_strategist",
    "media designer": "media",
}


def _canon_agent_slug(raw: str | None) -> str | None:
    """Return the canonical agent slug for any alias form, or `raw` on miss."""
    if not raw:
        return raw
    return _AGENT_SLUG_ALIASES.get(raw.strip().lower(), raw)


def _looks_like_confirmation_message(content: str) -> bool:
    """True if the incoming content is an agent's "saved!" status message.

    These show up as SECOND inbox writes right after the agent's real
    content — rejecting them prevents duplicate rows with text like
    "✅ Email draft saved to ARIA Inbox" cluttering the inbox next to
    the actual email they're confirming.
    """
    text = (content or "").strip().lower()
    if not text:
        return False
    return (
        "saved to aria inbox" in text
        or "saved to inbox" in text
        or "successfully saved" in text
        or "draft created and saved" in text
        or "draft id:" in text
        or text.startswith((
            "✅",
            ":white_check_mark:",
            "[saved]",
            "[done]",
            "## task complete",
            "## email draft complete",
            "email draft created",
        ))
    )


# Historical slug variants derived from the alias map PLUS the title-case
# forms that sometimes came from Paperclip's display layer. Keeping the
# list generated from _AGENT_SLUG_ALIASES.keys() makes it impossible for
# the cleanup filter to drift from what the normalizer accepts.
_NON_CANONICAL_AGENT_SLUGS: tuple[str, ...] = tuple({
    *_AGENT_SLUG_ALIASES.keys(),
    # Title-case variants that sometimes land from Paperclip's UI.
    "Email Marketer", "Content Writer", "Social Manager",
    "Ad Strategist", "Media Designer",
})


def _cleanup_noncanonical_inbox_rows() -> int:
    """Purge inbox rows with non-canonical agent slugs.

    Runs once at backend startup. Every row whose agent is one of the
    hyphenated / title-case / legacy display forms is a historical
    duplicate from the window BEFORE the slug-normalization fix
    landed — the canonical row with the underscore slug already
    exists alongside it. This sweep removes the ghost rows so users
    don't have to click Delete on each one.

    Safe to run every boot: once no rows match the filter the delete
    is a cheap no-op. Logs the count so we can spot regressions if
    the dedupe ever backslides.
    """
    try:
        sb = _get_supabase()
        existing = (
            sb.table("inbox_items")
            .select("id", count="exact")
            .in_("agent", list(_NON_CANONICAL_AGENT_SLUGS))
            .limit(1)
            .execute()
        )
        count = existing.count if existing.count is not None else len(existing.data or [])
        if count <= 0:
            return 0
        sb.table("inbox_items").delete().in_(
            "agent", list(_NON_CANONICAL_AGENT_SLUGS)
        ).execute()
        return count
    except Exception as e:
        logger.warning("Startup non-canonical inbox cleanup failed: %s", e)
        return 0


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

    # One-shot cleanup: delete historical inbox rows whose agent slug
    # is the hyphenated / display form. These exist because old
    # Paperclip skill curls wrote with the display slug before the
    # normalization fix landed; every one of them has a canonical
    # underscore-slug twin. Safe no-op once the DB is clean.
    try:
        purged = _cleanup_noncanonical_inbox_rows()
        if purged:
            logger.warning(
                "Startup: purged %d non-canonical inbox rows (hyphenated agent slugs)",
                purged,
            )
    except Exception as e:
        logger.warning("Startup inbox cleanup failed: %s", e)

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
    followup_task = asyncio.create_task(_followup_nudge_loop())
    repurpose_task = asyncio.create_task(_content_repurpose_loop())
    yield
    sync_task.cancel()
    scheduler_task.cancel()
    office_sync_task.cancel()
    followup_task.cancel()
    repurpose_task.cancel()
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
from backend.routers.email import router as email_router
from backend.routers.admin import router as admin_router
# NOTE: backend/routers/paperclip.py was a webhook receiver for the HTTP
# adapter experiment — we reverted to claude_local, so Paperclip never
# calls our webhook anymore. The agents now POST results back to ARIA via
# the aria-backend-api skill (which curls /api/inbox/{tenant}/items).

app.include_router(crm_router)
app.include_router(inbox_router)
app.include_router(campaigns_router)
app.include_router(email_router)
app.include_router(admin_router)

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

    # Pause gate — soft-lock for users whose profiles.status is
    # 'paused' or 'suspended'. Only blocks "expensive" actions (CEO
    # chat, agent runs). Reads (dashboard, inbox history, settings)
    # stay open so the user still sees their data + the banner.
    #
    # The exact match for /api/ceo/chat is intentional — we want to
    # block POST /api/ceo/chat (the message send) while leaving the
    # session-history reads (/api/ceo/chat/{session_id}/history etc)
    # open for read.
    method = request.method
    is_expensive = (
        method == "POST" and (
            path == "/api/ceo/chat"
            or (path.startswith("/api/agents/") and path.endswith("/run"))
        )
    )
    if is_expensive:
        from backend.services.profiles import get_user_status, is_paused
        user_id = (user.get("sub") or "")
        status = get_user_status(user_id)
        if is_paused(status):
            from starlette.responses import JSONResponse
            return JSONResponse(
                status_code=403,
                content={"detail": "ACCOUNT_PAUSED", "status": status},
                headers=cors_headers,
            )

    # RBAC gate for /api/admin/* — every admin endpoint requires the
    # caller's profiles.role to be 'admin' or 'super_admin'. The role
    # is also stamped onto request.state so individual handlers can
    # read it without a second lookup. Roles live in the profiles
    # table (created by migrations/create_profiles.sql); the lookup
    # is cached for 60s so repeated admin calls don't hammer the DB.
    if path.startswith("/api/admin/"):
        from backend.services.profiles import get_user_role, is_admin
        user_id = (user.get("sub") or "")
        role = get_user_role(user_id)
        if not is_admin(role):
            from starlette.responses import JSONResponse
            return JSONResponse(
                status_code=403,
                content={"detail": "Admin access required"},
                headers=cors_headers,
            )
        request.state.user = user
        request.state.role = role

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


# ─── Current user profile snapshot (role + status) ───
@app.get("/api/profile/me")
async def profile_me(request: Request):
    """Return the calling user's role + status. Used by the dashboard
    layout to decide whether to show the "account paused" banner.

    The auth middleware has already verified the JWT and stamped
    request.state.user; this just adds the profiles row lookup.
    """
    user = getattr(request.state, "user", None) or {}
    user_id = (user.get("sub") if isinstance(user, dict) else "") or ""
    if not user_id or user_id == "dev-user":
        # Dev mode or unauthenticated — return active so the frontend
        # doesn't render a phantom paused banner during local dev.
        return {"user_id": user_id, "role": "user", "status": "active"}
    from backend.services.profiles import get_user_role, get_user_status
    return {
        "user_id": user_id,
        "role": get_user_role(user_id),
        "status": get_user_status(user_id),
    }


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
    # Sanitize agent meta-commentary so lines like "LinkedIn post for X
    # created and saved to ARIA inbox (item <uuid>). Status: needs_review"
    # never get published to the actual feed. Refuse if nothing
    # substantive remains.
    text = _sanitize_social_post_text(text)
    if not text or len(text) < 20:
        raise HTTPException(
            status_code=400,
            detail=(
                "Post text looks like metadata or agent confirmation, not a "
                "real post. Ask the CEO to regenerate the post, then publish."
            ),
        )

    # Optional image attachment — when the post row has a resolved
    # image_url (from Media Designer pipeline), upload it through
    # LinkedIn's 3-step asset flow so the post renders as an image card
    # instead of text-only. linkedin_tool.create_post falls back to
    # text-only if any step of the upload fails.
    image_url = (body.get("image_url") or "").strip() or None
    result = await linkedin_tool.create_post(
        access_token, author_urn, text, image_url=image_url,
    )

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


_SOCIAL_META_PATTERNS = (
    # Opening "X post for Y created and saved to ARIA inbox (item uuid)."
    re.compile(
        r"^\s*(linkedin post|twitter post|tweet|x post|social post|post)s?\s+"
        r"for\s+[^.\n]*\s+(created|saved|ready|generated)[^.\n]*"
        r"(\(item[^)]*\))?\s*\.?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    # Trailing "Posts saved to ARIA inbox (inbox item uuid) with status X
    # — ready for approval and publishing." Matches plural or singular,
    # with or without the parenthetical id, with or without the status
    # and ready-clause tails.
    re.compile(
        r"\b(posts?|tweets?|emails?|drafts?|content)\s+(saved|stored|added|pushed|delivered|submitted|returned|sent)\s+"
        r"(to|into)\s+(aria\s+)?inbox[^\n]*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    # Inline variant: "Posts delivered to ARIA inbox item: <uuid>"
    # where the UUID is on the same line (not trailing the paragraph).
    re.compile(
        r"\b(posts?|tweets?|emails?|drafts?|content)\s+(saved|stored|added|pushed|delivered|submitted|returned|sent)\s+"
        r"(to|into)\s+(aria\s+)?inbox(\s+item)?\s*:?\s*[a-f0-9-]{6,}\s*\.?",
        re.IGNORECASE,
    ),
    # Bare trailing leak: "Delivered to ARIA inbox: Item ID <uuid>
    # with status ready for user review and publishing." Starts with
    # a VERB not a noun, so the pattern above misses it.
    re.compile(
        r"(?:^|\s)(delivered|submitted|saved|returned|sent|pushed)\s+"
        r"(to|into)\s+(aria\s+)?inbox[^.\n]*?(item\s+id\s*:?\s*)?[a-f0-9-]{6,}[^.\n]*\.?",
        re.IGNORECASE,
    ),
    # "Item ID: <uuid>" on its own or with trailing status text
    re.compile(
        r"\bitem\s+id\s*:?\s*[a-f0-9-]{6,}[^\n]*",
        re.IGNORECASE,
    ),
    # "(inbox item <uuid>)" / "(item <uuid>)" parentheticals
    re.compile(r"\(\s*(inbox\s+)?item\s+[a-f0-9-]{6,}\s*\)", re.IGNORECASE),
    # Trailing "— ready for approval and publishing" / "ready for review"
    re.compile(
        r"[—\-]\s*ready\s+for\s+(approval|review|publishing|sending)[^\n]*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    # "Status: needs_review" / "Status: ready" / "with status needs_review"
    re.compile(r"(^|\s)with\s+status\s+[a-z_]+\s*\.?", re.IGNORECASE),
    re.compile(r"^\s*status\s*:\s*[a-z_]+\s*\.?\s*$", re.IGNORECASE | re.MULTILINE),
    # "**Post summary:**" block header
    re.compile(r"\*\*\s*post\s+summary\s*:?\s*\*\*", re.IGNORECASE),
    # "Saved to ARIA inbox" / "Successfully saved"
    re.compile(
        r"^\s*(saved to aria inbox|successfully saved|draft saved|draft id:)[^\n]*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    # "## Done" / "## Task Complete" style confirmation headings
    re.compile(r"^\s*#{1,3}\s+(done|task complete|result|summary)[^\n]*$", re.IGNORECASE | re.MULTILINE),
)


def _sanitize_social_post_text(raw: str) -> str:
    """Strip agent meta-commentary from a would-be social post so the
    published text isn't the agent's status message.

    Matches opening "X post for Y created and saved to ARIA inbox (item
    uuid).", parenthetical (item uuid), Status: lines, **Post summary:**
    headers, confirmation headings like ## Done, and markdown fences.
    Returns the cleaned string — callers should still guard on
    len < ~20 to refuse publishing when nothing substantive remains.
    """
    if not raw:
        return ""
    clean = raw.strip()
    # Strip markdown fences first so they don't anchor other patterns
    if clean.startswith("```"):
        clean = "\n".join(clean.split("\n")[1:])
    if clean.endswith("```"):
        clean = "\n".join(clean.split("\n")[:-1])
    for pat in _SOCIAL_META_PATTERNS:
        clean = pat.sub("", clean)
    # Collapse multiple blank lines left by stripped patterns
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean


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

    # Fallback: treat entire content as a single tweet. Sanitize
    # agent meta-commentary first so things like "Tweet for SMAPS-SIS
    # created and saved to ARIA inbox (item <uuid>). **Post summary:**
    # ..." never get published as the actual post.
    if not posts:
        clean = _sanitize_social_post_text(content)
        if not clean or len(clean) < 20:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No publishable post text found in this inbox row — only "
                    "an agent summary / confirmation message. Ask the CEO to "
                    "regenerate the post so the actual tweet lands here."
                ),
            )
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
    await _emit_scheduled_task_created(tenant_id, result.get("task"))
    return result


async def _emit_scheduled_task_created(tenant_id: str, task: dict | None) -> None:
    """Emit a scheduled_task_created Socket.IO event + a notification so
    the Calendar page refetches and the sidebar/toast reflect the new
    entry without a manual refresh.

    Called from every path that inserts into scheduled_tasks: this HTTP
    endpoint, the CEO's `schedule_task` / `schedule_pending_draft`
    actions (via the chat handler), and the watcher in
    _watch_and_fire_pending_schedule. Best-effort — a socket hiccup
    never fails the underlying scheduling.
    """
    if not tenant_id or not task:
        return
    try:
        await sio.emit("scheduled_task_created", {
            "id": task.get("id"),
            "tenant_id": tenant_id,
            "task_type": task.get("task_type"),
            "title": task.get("title"),
            "scheduled_at": task.get("scheduled_at"),
            "status": task.get("status"),
            "payload": task.get("payload") or {},
        }, room=tenant_id)
    except Exception as e:
        logger.debug("[scheduled_task_created] socket emit failed: %s", e)
    # Friendly confirmation in the Notifications list + sidebar badge.
    try:
        when = (task.get("scheduled_at") or "")[:16].replace("T", " ")
        await _notify(
            tenant_id, "scheduled",
            f"Scheduled: {task.get('title', 'Task')}",
            body=f"Set for {when}",
            category="status",
            priority="normal",
            resource_type="scheduled_task",
            resource_id=str(task.get("id") or ""),
        )
    except Exception:
        pass


# ── Agent-finished signal ─────────────────────────────────────────────────
#
# Emitted whenever a sub-agent's output finalizes into an inbox row
# (transition from `processing` placeholder → `ready` / `needs_review` /
# `draft_pending_approval`). Distinct from `inbox_new_item` / `inbox_item_
# updated` — those are low-level CRUD events the inbox page uses to refresh
# its list. `task_completed` is a higher-signal event the dashboard
# layout subscribes to for the success toast ("Social Manager finished —
# View Draft"), so we want exactly ONE emission per agent finish, not the
# 2-3 inbox_item_updated emissions that fire during a placeholder upsert.
#
# The agent display name and item id give the toast everything it needs
# to render + deep-link without a follow-up fetch.
_AGENT_DISPLAY_NAMES: dict[str, str] = {
    "ceo": "ARIA CEO",
    "content_writer": "Content Writer",
    "email_marketer": "Email Marketer",
    "social_manager": "Social Manager",
    "ad_strategist": "Ad Strategist",
    "media": "Media Designer",
}


def _agent_display_name(slug: str) -> str:
    if not slug:
        return "Agent"
    return _AGENT_DISPLAY_NAMES.get(slug) or slug.replace("_", " ").title()


async def _emit_task_completed(
    tenant_id: str,
    *,
    inbox_item_id: str,
    agent_id: str,
    title: str,
    content_type: str,
    status: str,
) -> None:
    """Emit a task_completed Socket.IO event so the dashboard can show
    a "Social Manager finished — View Draft" toast and the Kanban widget
    can move the row out of In Progress.

    Best-effort — a socket hiccup never fails the underlying inbox
    save. Skip emission for placeholders (status='processing') and for
    media drafts, which use their own inbox_new_item emission flow."""
    if not tenant_id or not inbox_item_id or not agent_id:
        return
    if status == "processing":
        return  # placeholders are NOT completions
    try:
        await sio.emit("task_completed", {
            "inbox_item_id": inbox_item_id,
            "tenant_id": tenant_id,
            "agent": agent_id,
            "agent_display_name": _agent_display_name(agent_id),
            "title": title or "Draft ready",
            "type": content_type,
            "status": status,
        }, room=tenant_id)
    except Exception as e:
        logger.debug("[task_completed] socket emit failed: %s", e)


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
    for agent_id in ["ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist", "media"]:
        if agent_id not in agent_usage:
            limits = AGENT_HOURLY_LIMITS.get(agent_id, DEFAULT_AGENT_LIMIT)
            agent_usage[agent_id] = {
                "requests": 0,
                "request_limit": limits.get("requests", DEFAULT_AGENT_LIMIT["requests"]),
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "token_limit": limits.get("tokens", DEFAULT_AGENT_LIMIT["tokens"]),
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


def _apply_onboarding_edit(config: "TenantConfig", field: str, value) -> None:
    """Apply a single onboarding field edit to BOTH the legacy nested config
    shape AND the flat gtm_profile mirror in one place.

    Why two writes per field: several sub-agents read from the legacy nested
    fields (config.icp.target_titles, config.product.differentiators, etc.)
    while CEO chat context and the agent brief read from config.gtm_profile.
    Keeping the two in sync here means edits always propagate to every agent.
    """
    gp = config.gtm_profile
    if field == "business_name":
        config.business_name = value
        gp.business_name = value
    elif field == "offer":
        config.product.description = value
        config.description = value
        gp.offer = value
    elif field == "target_audience":
        config.icp.target_titles = [t.strip() for t in value.split(",") if t.strip()]
        gp.audience = value
    elif field == "problem_solved":
        config.icp.pain_points = [p.strip() for p in value.split(",") if p.strip()]
        gp.problem = value
    elif field == "differentiator":
        config.product.differentiators = [d.strip() for d in value.split(",") if d.strip()]
        gp.differentiator = value
    elif field == "channels":
        config.channels = value
        gp.primary_channels = value
    elif field == "brand_voice":
        config.brand_voice.tone = value
        gp.brand_voice = value
    elif field == "thirty_day_goal":
        config.gtm_playbook.action_plan_30 = value
        gp.goal_30_days = value


_ONBOARDING_EDITABLE_FIELDS = (
    "business_name", "offer", "target_audience", "problem_solved",
    "differentiator", "channels", "brand_voice", "thirty_day_goal",
)


@app.post("/api/tenant/{tenant_id}/update-onboarding")
async def update_onboarding(tenant_id: str, body: UpdateOnboarding):
    """Update specific onboarding fields on an existing tenant, regenerate
    derived gtm_profile fields and the agent brief so edits propagate to
    every downstream consumer.
    """
    from backend.config.brief import generate_agent_brief
    from backend.onboarding_agent import _ensure_generated_fields

    try:
        config = get_tenant_config(tenant_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Tenant not found")

    for field in _ONBOARDING_EDITABLE_FIELDS:
        value = getattr(body, field)
        if value is not None:
            _apply_onboarding_edit(config, field, value)

    # Regenerate derived fields (positioning_summary, 30_day_gtm_focus) from
    # the refreshed answers. _ensure_generated_fields is deterministic — just
    # string interpolation over the gtm_profile dict, no LLM call.
    gp = config.gtm_profile
    gp_dict = gp.model_dump()
    gp_dict["positioning_summary"] = ""
    gp_dict["30_day_gtm_focus"] = ""
    regen = _ensure_generated_fields(gp_dict)
    gp.positioning_summary = regen.get("positioning_summary", "")
    gp.thirty_day_gtm_focus = regen.get("30_day_gtm_focus", "")

    config.onboarding_status = "completed"
    config.skipped_fields = []

    # Regenerate the condensed agent brief so CEO chat picks up the edits.
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



# Gmail send helpers live in backend/services/email_sender.py. All in-file
# callers moved to routers/email.py, so no aliases remain in this module.


# _AGENT_SLUG_ALIASES, _canon_agent_slug, _looks_like_confirmation_message,
# and _NON_CANONICAL_AGENT_SLUGS all live near the top of this file (just
# above _cleanup_noncanonical_inbox_rows). They're referenced at module
# load time by the lifespan-startup cleanup, so they must be defined
# BEFORE that block runs.


# ─── Gmail Send API ───
# All /api/email/* endpoints (send, approve-send, update-draft,
# cancel-draft, threads, draft-reply, send-reply, sync, sync-all) moved
# to backend/routers/email.py and are registered via include_router at
# the top of this module.


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
    resource_type: str = "",
    resource_id: str = "",
) -> dict | None:
    """Persist a notification and emit it via Socket.IO.

    `resource_type` + `resource_id` are the universal deep-link
    handles the frontend's getRouteForItem() uses to route the user
    straight to the specific asset referenced by the alert.
    `href` stays supported as an explicit override for pre-existing
    call sites — when `resource_type` + `resource_id` are supplied
    and `href` is empty, we auto-derive a clean /<section>?id=<id>
    URL so callers don't have to string-build by hand.

    Stored in `metadata` JSONB (graceful when the table doesn't have
    dedicated columns yet) AND top-level so both old and new shape
    consumers can read it.
    """
    try:
        clean_body = _clean_notification_body(body)
        sb = _get_supabase()

        # Auto-derive href from resource_type/id when the caller
        # didn't pass one explicitly. Mirrors the same mapping the
        # frontend uses; keeps the two in sync so bell clicks always
        # match the canonical path.
        if not href and resource_type and resource_id:
            section = _RESOURCE_TYPE_TO_PATH.get(resource_type, "")
            if section:
                href = f"{section}?id={resource_id}"

        metadata: dict = {}
        if resource_type:
            metadata["resource_type"] = resource_type
        if resource_id:
            metadata["resource_id"] = resource_id

        row: dict = {
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
        if metadata:
            row["metadata"] = metadata
        try:
            result = sb.table("notifications").insert(row).execute()
        except Exception as e_metadata:
            # If the notifications table doesn't have a `metadata`
            # column in this tenant's schema, retry without it so the
            # alert still lands. metadata is additive, not required.
            logger.debug("notifications insert with metadata failed, retrying bare: %s", e_metadata)
            row.pop("metadata", None)
            result = sb.table("notifications").insert(row).execute()
        saved = result.data[0] if result.data else row
        # Always echo metadata on the Socket.IO payload so the
        # frontend can deep-link even if the table didn't persist it.
        if metadata and "metadata" not in saved:
            saved["metadata"] = metadata
        if resource_type and "resource_type" not in saved:
            saved["resource_type"] = resource_type
        if resource_id and "resource_id" not in saved:
            saved["resource_id"] = resource_id
        await sio.emit("notification", saved, room=tenant_id)
        return saved
    except Exception as e:
        logger.warning("Failed to save notification: %s", e)
        return None


# Backend copy of the frontend's RESOURCE_TYPE_PATH. Keep these two in
# sync — any new resource_type added here should also be added to
# frontend/lib/notification-routing.ts so the router knows where to
# send the user when the notification arrives.
_RESOURCE_TYPE_TO_PATH: dict[str, str] = {
    "inbox_item": "/inbox",
    "email_draft": "/inbox",
    "email_sequence": "/inbox",
    "social_post": "/inbox",
    "blog_post": "/inbox",
    "article": "/inbox",
    "landing_page": "/inbox",
    "ad_campaign": "/inbox",
    "image": "/inbox",
    "media": "/inbox",
    "project": "/projects",
    "task": "/projects",
    "crm_contact": "/crm",
    "contact": "/crm",
    "crm_company": "/crm",
    "company": "/crm",
    "crm_deal": "/crm",
    "deal": "/crm",
    "email_thread": "/conversations",
    "conversation": "/conversations",
    "whatsapp_thread": "/conversations",
    "scheduled_task": "/calendar",
    "schedule": "/calendar",
    "campaign": "/campaigns",
    "agent": "/agents",
    "agent_log": "/agents",
    "integration": "/settings",
    "system": "/settings",
}


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


# ─── Notifications ───

@app.get("/api/notifications/{tenant_id}/counts")
async def notification_counts(tenant_id: str):
    """Return counts used by the sidebar badges.

    The sidebar's "Inbox" badge is meant to tell the user how many inbox
    items are waiting on THEIR action — not a raw count of system events.
    We compute it directly from inbox_items (pending_approval + needs_review
    + failed) so the badge always matches the tab totals the user sees on
    the inbox page. The old `inbox_unread` field is kept for back-compat
    with any caller that still reads it.
    """
    sb = _get_supabase()

    # Per-category notification counts (used by Conversations + System badges).
    notif_result = sb.table("notifications").select("category", count="exact").eq(
        "tenant_id", tenant_id
    ).eq("is_read", False).execute()
    notif_counts: dict[str, int] = {}
    for row in (notif_result.data or []):
        cat = row.get("category", "other")
        notif_counts[cat] = notif_counts.get(cat, 0) + 1

    # Inbox action-needed count — drives the sidebar Inbox badge.
    try:
        inbox_result = sb.table("inbox_items").select("status").eq(
            "tenant_id", tenant_id
        ).in_("status", ["draft_pending_approval", "needs_review", "failed"]).execute()
        inbox_action_needed = len(inbox_result.data or [])
    except Exception as e:
        logger.warning("inbox action count failed: %s", e)
        inbox_action_needed = 0

    total = inbox_action_needed + notif_counts.get("conversation", 0) + notif_counts.get("system", 0)

    return {
        # Sidebar uses this for the Inbox badge.
        "inbox_unread": inbox_action_needed,
        "inbox_action_needed": inbox_action_needed,
        "conversations_unread": notif_counts.get("conversation", 0),
        "system_unread": notif_counts.get("system", 0),
        "status_unread": notif_counts.get("status", 0),
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
    """Mark specific notification IDs (or all) as read.

    Emits `notifications_read` via Socket.IO so other tabs / windows
    open on the same tenant can drop their local is_read flags without
    a manual refetch. Payload: `{ids: [...]}` where an empty array
    means "mark-all-read".
    """
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

    # Best-effort multi-tab sync. A socket hiccup shouldn't fail the
    # API call — the caller's optimistic local update still holds.
    try:
        await sio.emit(
            "notifications_read",
            {"ids": body.ids or [], "tenant_id": tenant_id},
            room=tenant_id,
        )
    except Exception as e:
        logger.debug("notifications_read emit failed (non-fatal): %s", e)

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
            await _emit_task_completed(
                tenant_id,
                inbox_item_id=saved["id"],
                agent_id=agent_name,
                title=title,
                content_type=content_type,
                status="ready",
            )

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

    # Recover chat_session_id from the watcher's placeholder (most recent
    # "processing" media row for this tenant). The Paperclip Media
    # Designer never learns the session_id — it's ARIA-internal — so we
    # backfill it here so the canonical row is session-scoped.
    inherited_session_id: str | None = None
    try:
        sb = _get_supabase()
        ph = (
            sb.table("inbox_items")
            .select("chat_session_id")
            .eq("tenant_id", tenant_id)
            .eq("agent", "media")
            .eq("status", "processing")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if ph.data:
            inherited_session_id = ph.data[0].get("chat_session_id")
    except Exception:
        inherited_session_id = None

    result = await media_agent.run(tenant_id, {
        "prompt": prompt,
        "chat_session_id": inherited_session_id,
    })
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
            await _emit_task_completed(
                tenant_id,
                inbox_item_id=inbox_row.get("id") or "",
                agent_id="media",
                title=inbox_row.get("title", ""),
                content_type=inbox_row.get("type", "image"),
                status=inbox_row.get("status", "ready"),
            )
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


@app.get("/api/office/agents/{tenant_id}/{agent_id}/activity")
async def virtual_office_agent_activity(tenant_id: str, agent_id: str, limit: int = 5):
    """Recent activity feed for a specific agent — powers the
    AgentInfoPanel's Recent Activity list. Pulls from agent_logs
    (every dispatched action) and recent inbox_items authored by the
    agent, merges by timestamp, and returns the top N."""
    sb = _get_supabase()
    items: list[dict] = []
    try:
        logs = sb.table("agent_logs").select(
            "action,status,timestamp,result"
        ).eq("tenant_id", tenant_id).eq("agent_name", agent_id).order(
            "timestamp", desc=True
        ).limit(limit).execute()
        for row in (logs.data or []):
            result = row.get("result") or {}
            summary = ""
            if isinstance(result, dict):
                summary = result.get("task") or result.get("title") or result.get("message") or ""
            items.append({
                "kind": "log",
                "action": row.get("action") or "",
                "status": row.get("status") or "",
                "summary": str(summary)[:140],
                "timestamp": row.get("timestamp"),
            })
    except Exception as e:
        logger.debug("[office-activity] agent_logs fetch failed: %s", e)
    try:
        inbox = sb.table("inbox_items").select(
            "title,status,created_at,type"
        ).eq("tenant_id", tenant_id).eq("agent", agent_id).order(
            "created_at", desc=True
        ).limit(limit).execute()
        for row in (inbox.data or []):
            items.append({
                "kind": "inbox",
                "action": row.get("type") or "draft",
                "status": row.get("status") or "",
                "summary": (row.get("title") or "")[:140],
                "timestamp": row.get("created_at"),
            })
    except Exception as e:
        logger.debug("[office-activity] inbox_items fetch failed: %s", e)

    items.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return {"agent_id": agent_id, "items": items[:limit]}


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
    """Aggregated analytics for the Analytics page.

    Pulls data from inbox_items, tasks, agent_logs, and scheduled_tasks
    to produce the KPI cards + activity chart + breakdowns + recent
    feed the frontend renders. Every aggregation is best-effort: a
    missing table or bad row never crashes the endpoint, the affected
    bucket just returns empty.
    """
    days = 7 if date_range == "7d" else 30 if date_range == "30d" else 90
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    sb = _get_supabase()

    # ── Inbox items: source of most analytics ─────────────────────
    inbox_rows: list[dict] = []
    try:
        res = (
            sb.table("inbox_items")
            .select("id, agent, type, status, title, created_at")
            .eq("tenant_id", tenant_id)
            .gte("created_at", cutoff_iso)
            .order("created_at", desc=True)
            .limit(2000)
            .execute()
        )
        inbox_rows = list(res.data or [])
    except Exception as e:
        logger.debug("[analytics] inbox_items fetch failed: %s", e)

    # ── Aggregations ──────────────────────────────────────────────
    activity_by_day: dict[str, dict[str, int]] = {}
    by_agent: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}

    # Seed all days so the chart x-axis is continuous even on quiet days
    for i in range(days):
        day = (datetime.now(timezone.utc) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        activity_by_day[day] = {"total": 0}

    _TYPE_BUCKET = {
        "email_sequence": "email", "email": "email",
        "social_post": "social", "social": "social",
        "image": "image", "image_request": "image",
        "blog_post": "content", "article": "content", "landing_page": "content",
        "ad_campaign": "ad",
    }

    for row in inbox_rows:
        created = (row.get("created_at") or "")[:10]
        if created and created in activity_by_day:
            bucket = _TYPE_BUCKET.get(row.get("type") or "", "other")
            activity_by_day[created]["total"] = activity_by_day[created].get("total", 0) + 1
            activity_by_day[created][bucket] = activity_by_day[created].get(bucket, 0) + 1
        agent = row.get("agent") or "unknown"
        by_agent[agent] = by_agent.get(agent, 0) + 1
        rtype = row.get("type") or "unknown"
        by_type[rtype] = by_type.get(rtype, 0) + 1
        rstatus = row.get("status") or "unknown"
        by_status[rstatus] = by_status.get(rstatus, 0) + 1

    activity_series = [
        {"date": day, **counts} for day, counts in sorted(activity_by_day.items())
    ]

    # ── Recent activity feed (last 10 across all types) ───────────
    recent_activity = [
        {
            "id": r.get("id"),
            "agent": r.get("agent"),
            "type": r.get("type"),
            "status": r.get("status"),
            "title": (r.get("title") or "")[:120],
            "created_at": r.get("created_at"),
        }
        for r in inbox_rows[:10]
    ]

    # ── Task completion / scheduled task stats ────────────────────
    task_totals = {"total": 0, "completed": 0, "in_progress": 0, "failed": 0}
    try:
        tasks_res = (
            sb.table("tasks")
            .select("status")
            .eq("tenant_id", tenant_id)
            .gte("created_at", cutoff_iso)
            .limit(2000)
            .execute()
        )
        for t in tasks_res.data or []:
            s = (t.get("status") or "").lower()
            task_totals["total"] += 1
            if s in ("done", "completed"):
                task_totals["completed"] += 1
            elif s in ("in_progress", "working", "running"):
                task_totals["in_progress"] += 1
            elif s in ("failed", "cancelled", "canceled", "error"):
                task_totals["failed"] += 1
    except Exception as e:
        logger.debug("[analytics] tasks fetch failed: %s", e)

    scheduled_totals = {"upcoming": 0, "executed": 0, "failed": 0}
    try:
        sched_res = (
            sb.table("scheduled_tasks")
            .select("status, scheduled_at")
            .eq("tenant_id", tenant_id)
            .gte("created_at", cutoff_iso)
            .limit(2000)
            .execute()
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        for t in sched_res.data or []:
            s = (t.get("status") or "").lower()
            if s in ("sent", "executed", "completed", "done"):
                scheduled_totals["executed"] += 1
            elif s in ("failed", "cancelled", "canceled", "error"):
                scheduled_totals["failed"] += 1
            elif (t.get("scheduled_at") or "") > now_iso:
                scheduled_totals["upcoming"] += 1
    except Exception as e:
        logger.debug("[analytics] scheduled_tasks fetch failed: %s", e)

    # ── Totals / KPIs derived from above ──────────────────────────
    totals = {
        "items": len(inbox_rows),
        "agents_active": len(by_agent),
        "types_active": len(by_type),
        "days_in_range": days,
    }

    return {
        "tenant_id": tenant_id,
        "date_range": date_range,
        "totals": totals,
        "activity_series": activity_series,
        "by_agent": [{"agent": k, "count": v} for k, v in sorted(by_agent.items(), key=lambda x: -x[1])],
        "by_type": [{"type": k, "count": v} for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
        "by_status": [{"status": k, "count": v} for k, v in sorted(by_status.items(), key=lambda x: -x[1])],
        "recent_activity": recent_activity,
        "tasks": task_totals,
        "scheduled_tasks": scheduled_totals,
        # Keep the old funnel shape so the demo endpoint callers don't break.
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


# Email parser helpers live in backend/services/email_parser.py. Aliases
# keep the existing in-file references working.
from backend.services.email_parser import (
    markdown_to_basic_html as _markdown_to_basic_html,
    parse_html_email_draft as _parse_html_email_draft,
    parse_email_draft_from_text as _parse_email_draft_from_text,
    parse_social_drafts_from_text as _parse_social_drafts_from_text,
)


def _enrich_task_desc_with_crm(task_desc: str, tenant_id: str) -> str:
    """Enrich a delegated task with CRM context so the downstream agent
    can personalize its output.

    What gets appended when a contact name matches in the task:
    - contact email (so email_marketer has a recipient)
    - contact status (lead / customer / churned) and notes field
    - latest deal per matched contact (title, stage, value) so the
      agent can reference "about your [deal_title]" in email copy
      without the CEO having to quote it manually

    The CEO's CRM-context heuristic only triggers on "send email to X"
    phrasing — "create marketing email for Hanz" or "follow up with
    Tina" skip the heuristic, so this helper does the lookup at dispatch
    time and inlines the data directly into the task description.
    """
    if not task_desc or not tenant_id:
        return task_desc
    try:
        sb = _get_supabase()
        contacts_res = (
            sb.table("crm_contacts")
            .select("id,name,email,company_id,status,notes")
            .eq("tenant_id", tenant_id)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )
        if not contacts_res.data:
            return task_desc

        task_lower = task_desc.lower()
        matches: list[dict] = []
        for c in contacts_res.data:
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

        # Best-effort deal lookup for matched contacts — one query, all
        # matched contacts at once. Ordered by most recent so the agent
        # sees the current opportunity, not a stale one.
        deals_by_contact: dict[str, dict] = {}
        try:
            contact_ids = [m["id"] for m in matches if m.get("id")]
            if contact_ids:
                deals_res = (
                    sb.table("crm_deals")
                    .select("title,stage,value,contact_id,updated_at")
                    .eq("tenant_id", tenant_id)
                    .in_("contact_id", contact_ids)
                    .order("updated_at", desc=True)
                    .execute()
                )
                # Keep only the FIRST (most recent) deal per contact.
                for d in (deals_res.data or []):
                    cid = d.get("contact_id")
                    if cid and cid not in deals_by_contact:
                        deals_by_contact[cid] = d
        except Exception:
            pass

        # Render rich context lines per matched contact so the email_marketer
        # can pull name + email + status + deal + notes into the copy.
        lines: list[str] = []
        for c in matches:
            email = c.get("email") or "(no email)"
            status = c.get("status") or ""
            notes = (c.get("notes") or "").strip()
            header = f"  - {c['name']} <{email}>"
            if status:
                header += f" [{status}]"
            lines.append(header)
            deal = deals_by_contact.get(c.get("id"))
            if deal:
                bits = [deal.get("title") or "deal"]
                if deal.get("stage"):
                    bits.append(f"stage: {deal['stage']}")
                if deal.get("value"):
                    bits.append(f"value: ${deal['value']}")
                lines.append(f"      Deal: {' — '.join(bits)}")
            if notes:
                snippet = notes[:240].replace("\n", " ").strip()
                lines.append(f"      Notes: {snippet}")

        return (
            f"{task_desc}\n\n"
            f"CRM context for mentioned contacts (use for personalization):\n"
            + "\n".join(lines)
        )
    except Exception as e:
        logging.getLogger("aria.crm").debug("CRM enrichment failed: %s", e)
        return task_desc


# Template helpers live in backend/services/email_template.py. Aliases here
# so existing in-file references keep working. New code should import from
# the service module directly.
from backend.services.email_template import (
    agent_html_already_designed as _agent_html_already_designed,
    business_name_for_template as _business_name_for_template,
    wrap_email_in_designed_template as _wrap_email_in_designed_template,
    strip_html_to_text as _strip_html_to_text,
)


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


async def _execute_delegation(
    tenant_id: str,
    session_id: str | None,
    d: dict,
    cumulative_delay: int,
    saved_tasks: list[dict] | None,
) -> None:
    """Full per-delegation dispatch body, usable inline OR as a background task.

    Sleeps `cumulative_delay` seconds first so pipeline follow-ups
    fire only after their upstream has landed; 0 is a no-op for the
    immediate-dispatch path. The body:

      1. Enrich task_desc with CRM context (email / social / ads /
         content-writer only — media doesn't need CRM).
      2. Insert a `tasks` row in `in_progress` so the Kanban board
         shows the agent as working.
      3. Emit walk_to_meeting choreography for the Virtual Office.
      4. Dispatch through Paperclip if connected + registered, else
         fall back to the local `AGENT_REGISTRY[agent_id].run`.

    `saved_tasks`, when supplied, gets appended with the inserted
    tasks row so the HTTP response can include it. Pass None for
    background (delayed) dispatches — the task still persists to
    the DB, the UI picks it up via the socket event.
    """
    if cumulative_delay > 0:
        await asyncio.sleep(cumulative_delay)

    _log = logging.getLogger("aria.ceo_chat.dispatch")
    agent_id = d["agent"]
    task_desc = d.get("task", "")

    if tenant_id and agent_id in ("email_marketer", "social_manager", "ad_strategist", "content_writer"):
        task_desc = _enrich_task_desc_with_crm(task_desc, tenant_id)

    # Pipeline image hint: when the chain had a media step earlier,
    # resolve the concrete image URL via asset_lookup and inline it
    # directly into the task_desc as a MANDATORY instruction. The
    # previous version only said "include the image" without providing
    # the URL — the agent had to go discover it via its own
    # asset_lookup call, which it sometimes skipped (drift). Now the
    # URL is in the prompt and the instruction tells the agent exactly
    # what to do with it per platform.
    #
    # Auto-flag: if the CEO did NOT explicitly tag this delegation as a
    # pipeline follow-up but the current session has a recent media
    # image, treat it AS IF the flag were set. Covers multi-turn flows
    # (user makes an image in turn 1, asks for email/post in turn 2)
    # where the CEO's chain logic doesn't fire because there's only one
    # delegation in this turn. Session-scoped so parallel chats don't
    # leak each other's assets.
    is_downstream_agent = agent_id in (
        "email_marketer", "social_manager", "content_writer", "ad_strategist",
    )
    has_media_flag = bool(d.get("_pipeline_has_media_image"))
    if is_downstream_agent and not has_media_flag and tenant_id and session_id:
        try:
            from backend.services.asset_lookup import get_latest_image_url as _peek_img
            if _peek_img(tenant_id, within_minutes=360, session_id=session_id):
                has_media_flag = True
                logging.getLogger("aria.ceo_chat.dispatch").info(
                    "[pipeline-image] auto-flagged %s: session %s has a recent media image",
                    agent_id, session_id,
                )
        except Exception:
            pass

    if has_media_flag and is_downstream_agent:
        resolved_image_url: str | None = None
        try:
            from backend.services.asset_lookup import get_latest_image_url
            # 360-min window matches the sub-agent self-lookup helpers
            # (email/content/social all use 6h) so the pipeline-level
            # fetch doesn't cliff sooner than the agent's own fallback.
            # Session-scoped first to avoid cross-chat bleed.
            resolved_image_url = get_latest_image_url(
                tenant_id, within_minutes=360, session_id=session_id,
            )
        except Exception as e:
            logging.getLogger("aria.ceo_chat.dispatch").debug(
                "[pipeline-image] lookup failed for %s: %s", agent_id, e,
            )

        per_agent_instruction = {
            "email_marketer": "Embed this image at the top of the HTML body using <img src=\"<URL>\" style=\"max-width:100%;height:auto;border-radius:8px;\" alt=\"\"/>.",
            "social_manager": "Include this URL in the `image_url` field of each post in the output JSON (or append at the end of the caption if inline).",
            "content_writer": "Reference this image explicitly at the hero / top slot using markdown ![alt](<URL>) so the user can paste it directly.",
            "ad_strategist": "Use this as the hero visual in every ad variant. Reference the URL in the Creative Assets section of the campaign plan.",
        }.get(agent_id, "Include this URL prominently in your output.")

        if resolved_image_url:
            task_desc = (
                f"{task_desc.rstrip()}\n\n"
                f"=== MEDIA ASSET (REQUIRED) ===\n"
                f"You have been provided an image asset:\n"
                f"{resolved_image_url}\n\n"
                f"You MUST include this URL in your final output. "
                f"{per_agent_instruction}\n"
                f"Do NOT describe the image — use the URL directly so it renders for the user."
            )
        else:
            # Fall back to the soft hint when asset_lookup returned
            # nothing — the chain may have been sped up, or the media
            # step may have failed. The agent can still try its own
            # lookup via the existing keyword detector.
            task_lower = (task_desc or "").lower()
            if not any(w in task_lower for w in (
                "image", "picture", "photo", "banner", "logo", "visual",
                "graphic", "hero", "illustration", "thumbnail"
            )):
                task_desc = (
                    f"{task_desc.rstrip()}\n\n"
                    f"(Include the image just generated by the Media Designer — "
                    f"reference it at the top / hero slot of the deliverable.)"
                )

    # Explicit source-asset handoff: the CEO can reference specific prior
    # inbox rows by id via `source_inbox_item_ids: ["abc","def"]`. Resolve
    # each row, extract the useful bits (image URL / blog body / email
    # subject), and append a `[REFERENCED ASSET]` block to the task so
    # the downstream agent sees concrete content alongside its own task
    # instructions. Unlike the time-windowed lookups, this bypasses the
    # cliff problem entirely — "use the banner from two weeks ago" works
    # as long as the CEO found the id (via Recent Activity or read_inbox).
    source_ids = d.get("source_inbox_item_ids") or []
    if isinstance(source_ids, str):
        source_ids = [source_ids]
    if source_ids and tenant_id:
        try:
            from backend.services.asset_lookup import (
                get_inbox_row_by_id as _get_row,
                extract_image_url_from_row as _extract_img,
            )
            blocks: list[str] = []
            for sid in source_ids[:5]:  # cap at 5 to bound token cost
                row = _get_row(tenant_id, sid)
                if not row:
                    continue
                lines = [f"[REFERENCED ASSET — id={sid}]"]
                if row.get("title"):
                    lines.append(f"Title: {row['title']}")
                if row.get("type"):
                    lines.append(f"Type: {row['type']}")
                if row.get("agent"):
                    lines.append(f"Produced by: {row['agent']}")
                img = _extract_img(row)
                if img:
                    lines.append(f"Image URL: {img}")
                draft = row.get("email_draft")
                if isinstance(draft, dict):
                    if draft.get("subject"):
                        lines.append(f"Subject: {draft['subject']}")
                    snippet = draft.get("preview_snippet") or draft.get("text_body") or ""
                    if snippet:
                        lines.append(f"Preview: {snippet[:240]}")
                body = (row.get("content") or "").strip()
                if body and not img:
                    # Don't duplicate when the content is just a markdown image
                    lines.append(f"Content excerpt:\n{body[:900]}")
                blocks.append("\n".join(lines))
            if blocks:
                task_desc = (
                    f"{task_desc.rstrip()}\n\n"
                    + "\n\n".join(blocks)
                    + "\n\n(The referenced asset(s) above were produced earlier in "
                    "this workspace. Use them as source material / embed where "
                    "appropriate for this task.)"
                )
                _log = logging.getLogger("aria.ceo_chat.dispatch")
                _log.info(
                    "[ceo-dispatch] resolved %d source_inbox_item_ids for %s: %s",
                    len(blocks), agent_id, ",".join(source_ids[:5]),
                )
        except Exception as e:
            logging.getLogger("aria.ceo_chat.dispatch").warning(
                "[ceo-dispatch] source_inbox_item_ids resolver failed: %s", e,
            )

    saved_task_id: str | None = None
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
                saved_task_id = result.data[0]["id"]
                if saved_tasks is not None:
                    saved_tasks.append(result.data[0])
                await sio.emit("task_updated", {
                    "id": saved_task_id,
                    "agent": agent_id,
                    "status": "in_progress",
                    "task": task_desc,
                }, room=tenant_id)
        except Exception:
            pass

    if tenant_id:
        await _emit_agent_status(tenant_id, "ceo", "running",
                                 current_task=f"Briefing {agent_id} on: {task_desc[:60]}",
                                 action="walk_to_meeting")
        await _emit_agent_status(tenant_id, agent_id, "running",
                                 current_task=task_desc,
                                 action="walk_to_meeting")

    try:
        connected = paperclip_connected()
        paperclip_id = get_paperclip_agent_id(agent_id) if connected else None
        _log.warning(
            "[ceo-dispatch] agent=%s paperclip_connected=%s paperclip_id=%s delay=%s",
            agent_id, connected, paperclip_id, cumulative_delay,
        )

        if connected and paperclip_id:
            _safe_background(
                _dispatch_paperclip_and_watch_to_inbox(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    task_desc=task_desc,
                    session_id=session_id,
                    task_id=saved_task_id,
                    priority=d.get("priority", "medium"),
                ),
                label=f"paperclip-watch-{agent_id}",
            )
        else:
            _log.warning(
                "[ceo-dispatch] FALLING BACK to local for %s (connected=%s, paperclip_id=%s)",
                agent_id, connected, paperclip_id,
            )
            agent_module = AGENT_REGISTRY.get(agent_id)
            if agent_module:
                _safe_background(
                    _run_agent_to_inbox(
                        agent_module, agent_id, tenant_id, task_desc,
                        session_id,
                        saved_task_id,
                        d.get("priority", "medium"),
                    ),
                    label=f"local-agent-{agent_id}",
                )
            else:
                _log.error(
                    "[ceo-dispatch] no agent_module for %s in AGENT_REGISTRY", agent_id,
                )
    except Exception as _disp_exc:
        import traceback
        _log.error(
            "[ceo-dispatch] FAILED to dispatch %s: %s\n%s",
            agent_id, _disp_exc, traceback.format_exc(),
        )


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
            await _emit_task_completed(
                tenant_id,
                inbox_item_id=placeholder_id,
                agent_id=agent_id,
                title=title,
                content_type=content_type,
                status=item_status,
            )
            n_type = "approval_needed" if item_status == "draft_pending_approval" else "inbox_new_item"
            # Deep-link the notification directly to this inbox row.
            # resource_type + resource_id is the universal shape; href
            # is auto-derived in _notify when absent.
            await _notify(
                tenant_id, n_type, title,
                body=content[:200] if content else "",
                category="inbox",
                priority=priority,
                resource_type="inbox_item",
                resource_id=placeholder_id or "",
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


async def _media_safety_net(
    tenant_id: str,
    placeholder_id: str | None,
    task_desc: str,
    task_id: str | None,
) -> None:
    """Safety net for Media delegations.

    The primary path goes through Paperclip: the Media Designer agent
    refines the prompt and curls POST /api/media/<tenant>/generate,
    which triggers Pollinations → Supabase → inbox update. When that
    works, this helper exits silently without doing anything.

    When the Paperclip agent fails to curl (writes a prompt and stops,
    or hangs, or drifts off the MD), the placeholder stays in
    `processing` state indefinitely. This helper waits 45s, then
    checks the placeholder status. If it's still processing, we call
    media_agent.run() locally with the task description as the prompt
    — preserving the Pollinations pipeline but bypassing the
    unreliable Paperclip step. The user never sees a stuck row.
    """
    if not placeholder_id:
        return
    _log = logging.getLogger("aria.media_safety")
    # Wait for the Paperclip Media Designer to do its job. 45s covers
    # typical prompt refine (~3-8s) + Pollinations generation (~10-20s)
    # + Supabase upload (~2-3s) with plenty of headroom.
    await asyncio.sleep(45)

    sb = _get_supabase()
    # Is the placeholder already resolved? If so, Paperclip path won.
    try:
        row = await asyncio.to_thread(
            lambda: sb.table("inbox_items")
            .select("id, status, content")
            .eq("id", placeholder_id)
            .limit(1)
            .execute()
        )
        if not row.data:
            _log.info("[media-safety] placeholder %s gone — assume Paperclip path succeeded", placeholder_id)
            return
        data = row.data[0]
        status = (data.get("status") or "").lower()
        content = data.get("content") or ""
        # "processing" + no image URL embedded in content = stuck.
        # If the row was already updated (status != processing) OR the
        # content contains an image URL, the Paperclip path won.
        has_image = bool(re.search(r"https?://\S+\.(?:png|jpg|jpeg|webp|gif)", content, re.IGNORECASE))
        if status != "processing" or has_image:
            _log.info(
                "[media-safety] placeholder %s resolved via Paperclip (status=%s, has_image=%s) — skip fallback",
                placeholder_id, status, has_image,
            )
            return
    except Exception as e:
        _log.debug("[media-safety] placeholder lookup failed: %s", e)
        return

    # Paperclip path stalled. Run the local media_agent as a fallback.
    # Pull chat_session_id off the placeholder row so the fallback's
    # inbox row stays session-scoped (same as the Paperclip-path row
    # would have been). Without this, the safety-net path loses session
    # identity and downstream sub-agents in the same chat can't scope
    # their image lookup.
    _log.warning(
        "[media-safety] Paperclip Media Designer did not produce an image after 45s — "
        "falling back to local media_agent.run() for %s", placeholder_id,
    )
    placeholder_session_id: str | None = None
    try:
        ph_row = await asyncio.to_thread(
            lambda: sb.table("inbox_items")
            .select("chat_session_id")
            .eq("id", placeholder_id)
            .limit(1)
            .execute()
        )
        if ph_row.data:
            placeholder_session_id = ph_row.data[0].get("chat_session_id")
    except Exception:
        placeholder_session_id = None
    try:
        from backend.agents import media_agent
        result = await media_agent.run(tenant_id, {
            "prompt": task_desc,
            "chat_session_id": placeholder_session_id,
        })
    except Exception as e:
        _log.error("[media-safety] media_agent.run failed: %s", e)
        result = None

    image_url = None
    if isinstance(result, dict):
        r = result.get("result") or {}
        if isinstance(r, dict):
            image_url = r.get("image_url")

    now_iso = datetime.now(timezone.utc).isoformat()
    if image_url:
        # media_agent.run() already created its own inbox row. Delete
        # the placeholder so the canonical agent row is the one the
        # user sees.
        try:
            await asyncio.to_thread(
                lambda: sb.table("inbox_items").delete().eq("id", placeholder_id).execute()
            )
            if tenant_id:
                try:
                    await sio.emit("inbox_updated", {"action": "deleted", "id": placeholder_id}, room=tenant_id)
                except Exception:
                    pass
        except Exception as e:
            _log.warning("[media-safety] placeholder delete failed: %s", e)
        if task_id:
            try:
                await asyncio.to_thread(lambda: sb.table("tasks").update({
                    "status": "done",
                    "updated_at": now_iso,
                }).eq("id", task_id).execute())
                if tenant_id:
                    try:
                        await sio.emit("task_updated", {
                            "id": task_id, "agent": "media",
                            "status": "done", "task": task_desc,
                        }, room=tenant_id)
                    except Exception:
                        pass
            except Exception:
                pass
    else:
        # Both paths failed. Surface the error so the user isn't stuck.
        try:
            await asyncio.to_thread(lambda: sb.table("inbox_items").update({
                "title": f"Failed to generate image: {task_desc[:80]}",
                "content": "Image generation failed via both Paperclip and the local fallback. Check backend logs for the Pollinations / Gemini / Supabase error.",
                "status": "needs_review",
                "updated_at": now_iso,
            }).eq("id", placeholder_id).execute())
        except Exception as e:
            _log.warning("[media-safety] failure update errored: %s", e)
        if task_id:
            try:
                await asyncio.to_thread(lambda: sb.table("tasks").update({
                    "status": "failed",
                    "updated_at": now_iso,
                }).eq("id", task_id).execute())
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

    # Media special-case: placeholder goes up instantly for UX, then
    # the Paperclip Media Designer runs (refines the prompt + curls
    # /api/media/<tenant>/generate → Pollinations → Supabase → inbox
    # update). A 45s safety-net runs in the background: if the
    # placeholder is still "processing" after the deadline, we
    # assume the Paperclip agent skipped the curl step and call
    # media_agent.run() locally with the task description as the
    # prompt. That way the primary Paperclip path is unchanged, but
    # the user never sees a stuck "Media is working on..." row.
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

        _safe_background(
            _media_safety_net(
                tenant_id=tenant_id,
                placeholder_id=placeholder["id"] if placeholder else None,
                task_desc=task_desc,
                task_id=task_id,
            ),
            label="media-safety-net",
        )
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
        # Close the Kanban task as failed so it doesn't sit forever in
        # "in_progress" after a timeout or agent crash.
        if task_id:
            try:
                sb = _get_supabase()
                await asyncio.to_thread(lambda: sb.table("tasks").update({
                    "status": "failed",
                    "updated_at": now_iso,
                }).eq("id", task_id).execute())
                if tenant_id:
                    await sio.emit("task_updated", {
                        "id": task_id, "agent": agent_id,
                        "status": "failed", "task": task_desc,
                    }, room=tenant_id)
            except Exception:
                pass
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
            # Normalize the agent's markdown reply into the canonical
            # `{"action": "adapt_content", "posts": [...]}` JSON the
            # frontend's parseSocialPosts expects. Without this, the
            # agent's raw reply ("## Social posts created / **Twitter:**
            # ... / **LinkedIn:** ...") lands in the content column
            # verbatim and the frontend falls back to rendering raw
            # markdown instead of the platform-card UI. Uses the same
            # _parse_posts the local social_manager_agent path uses,
            # including its markdown fallback.
            try:
                from backend.agents.social_manager_agent import (
                    _parse_posts as _sm_parse_posts,
                )
                normalized_posts = _sm_parse_posts(output)
                if normalized_posts:
                    # Attach a recent media image if one exists and the
                    # task hinted at needing one — same logic the local
                    # path applies after the JSON is ready.
                    attached_img = None
                    try:
                        from backend.services.asset_lookup import (
                            get_latest_image_url as _get_img,
                            find_referenced_asset as _find_ref,
                            extract_image_url_from_row as _extract_img,
                            task_has_reference as _has_ref,
                        )
                        _task_l = (task_desc or "").lower()
                        wants_image = (
                            any(k in _task_l for k in (
                                "image", "photo", "picture", "banner", "visual",
                                "graphic", "illustration", "thumbnail",
                                "with an image", "with a picture",
                            ))
                            or _has_ref(task_desc or "")
                        )
                        if wants_image:
                            attached_img = _get_img(tenant_id, within_minutes=360)
                            if not attached_img and _has_ref(task_desc or ""):
                                for row in _find_ref(
                                    tenant_id, text_hint=task_desc or "",
                                    agent="media", types=["image"], limit=3,
                                ):
                                    u = _extract_img(row)
                                    if u:
                                        attached_img = u
                                        break
                    except Exception as _img_err:
                        _logger.debug("[paperclip-watch] image attach skipped: %s", _img_err)
                    # Last-ditch image rescue: if the agent leaked the
                    # Supabase URL into one of the post bodies and we
                    # don't have an image attached yet, promote the
                    # leaked URL to image_url so the card renders and
                    # the sanitizer scrubs it out of the visible text.
                    if not attached_img:
                        try:
                            from backend.agents.social_manager_agent import (
                                _extract_supabase_url as _sm_extract_url,
                            )
                            for p in normalized_posts:
                                leaked = _sm_extract_url(p.get("text", ""))
                                if leaked:
                                    attached_img = leaked
                                    break
                        except Exception:
                            pass
                    if attached_img:
                        for p in normalized_posts:
                            p["image_url"] = attached_img
                    # Replace the raw-markdown output with canonical JSON
                    # so every frontend path (detail view + thumbnail
                    # extract + content_index embed) sees the same shape.
                    import json as _json_inner
                    output = _json_inner.dumps({
                        "action": "adapt_content",
                        "posts": normalized_posts,
                    })
                    _logger.info(
                        "[paperclip-watch] normalized %d social posts for issue %s (image=%s)",
                        len(normalized_posts), paperclip_issue_id, bool(attached_img),
                    )
                else:
                    # Degraded path — no parseable posts. Clean the raw
                    # output so the "what the agent wrote instead" panel
                    # in the inbox doesn't leak status/deliverables/
                    # supabase-url noise to the user.
                    try:
                        from backend.agents.social_manager_agent import (
                            _sanitize_social_text as _sm_sanitize,
                        )
                        cleaned = _sm_sanitize(output)
                        if cleaned:
                            output = cleaned
                    except Exception:
                        pass
            except Exception as _norm_err:
                _logger.debug(
                    "[paperclip-watch] social-post normalization skipped: %s", _norm_err,
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

    # Index the finalized row for long-term recall (content_library
    # mirror + Qdrant embedding). Covers the watcher path — the skill-
    # curl path is indexed from create_inbox_item, and the safety-net
    # poller from poll_completed_issues. One of the three fires per
    # delegation; index_inbox_row is idempotent via its
    # contains(metadata, inbox_item_id) pre-check.
    if placeholder_id and tenant_id:
        try:
            from backend.services.content_index import index_inbox_row
            finalized = {
                "id": placeholder_id,
                "tenant_id": tenant_id,
                "agent": agent_id,
                "type": content_type,
                "title": title,
                "content": output,
                "status": inbox_status,
                "email_draft": email_draft,
            }
            await asyncio.to_thread(index_inbox_row, finalized)
        except Exception as ix_err:
            _logger.debug("[paperclip-watch] content_index skipped: %s", ix_err)

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
            await _emit_task_completed(
                tenant_id,
                inbox_item_id=placeholder_id,
                agent_id=agent_id,
                title=title,
                content_type=content_type,
                status=inbox_status,
            )
            await _notify(
                tenant_id, "inbox_new_item", title,
                body=output[:200],
                category="inbox",
                priority=priority,
                resource_type="inbox_item",
                resource_id=placeholder_id or "",
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


def _merge_into_recent_social_row(tenant_id: str, body: "CreateInboxItem") -> dict | None:
    """Merge a new social_post into a recent social_post row for the
    same tenant+agent, if one exists within the last 90 seconds.

    Agents sometimes split platforms into multiple POSTs (one for X,
    one for LinkedIn, one for Facebook). The frontend's platform-card
    UI only renders when all platforms live in one row's `content`
    JSON posts array. This helper finds the existing row and appends
    the incoming platforms to its posts array — no duplicate rows,
    all platforms render as cards inside a single inbox entry.

    Returns the updated row dict on successful merge, or None when
    no recent row exists and the caller should proceed with a normal
    insert.
    """
    try:
        sb = _get_supabase()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        recent = (
            sb.table("inbox_items")
            .select("id, content, title, status")
            .eq("tenant_id", tenant_id)
            .eq("agent", "social_manager")
            .eq("type", "social_post")
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not recent.data:
            return None
        existing = recent.data[0]
    except Exception as e:
        logging.getLogger("aria.inbox").debug(
            "[social-merge] recent-row lookup failed: %s", e,
        )
        return None

    # Parse posts out of BOTH rows, merge by platform (new wins when
    # both have the same platform — the newer payload is fresher).
    import json as _json_inner

    def _extract_posts(text: str) -> list[dict]:
        if not text:
            return []
        try:
            s = text.find("{")
            e = text.rfind("}") + 1
            if s >= 0 and e > s:
                data = _json_inner.loads(text[s:e])
                posts = data.get("posts") or []
                if isinstance(posts, list):
                    return [p for p in posts if isinstance(p, dict)]
        except Exception:
            pass
        try:
            s = text.find("[")
            e = text.rfind("]") + 1
            if s >= 0 and e > s:
                parsed = _json_inner.loads(text[s:e])
                if isinstance(parsed, list):
                    return [p for p in parsed if isinstance(p, dict)]
        except Exception:
            pass
        return []

    existing_posts = _extract_posts(existing.get("content") or "")
    new_posts = _extract_posts(body.content or "")
    if not new_posts:
        # The new POST isn't parseable as structured posts — bail, let
        # the normal path handle it (it'll end up as plain content).
        return None

    # Merge by platform (case-insensitive). New platform text wins
    # when both rows have the same platform (latest data is freshest).
    by_platform: dict[str, dict] = {}
    for p in existing_posts:
        plat = (p.get("platform") or "").lower() or "unknown"
        by_platform[plat] = p
    for p in new_posts:
        plat = (p.get("platform") or "").lower() or "unknown"
        by_platform[plat] = p
    merged_posts = list(by_platform.values())

    merged_content = _json_inner.dumps({
        "action": "adapt_content",
        "posts": merged_posts,
    })

    # Use a neutral title that covers all platforms if the existing
    # title was platform-specific (e.g. "LinkedIn Post: X" — the exact
    # symptom of a split delegation).
    title = existing.get("title") or body.title
    if title and any(t in title.lower() for t in (
        "linkedin post:", "twitter post:", "x post:", "facebook post:",
    )):
        # Strip the platform prefix so the row title represents ALL
        # the platforms it now contains.
        for prefix in ("LinkedIn Post:", "Twitter Post:", "X Post:", "Facebook Post:"):
            if title.lower().startswith(prefix.lower()):
                title = "Social posts:" + title[len(prefix):]
                break

    try:
        sb = _get_supabase()
        updated = (
            sb.table("inbox_items")
            .update({
                "content": merged_content,
                "title": title,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", existing["id"])
            .execute()
        )
        logging.getLogger("aria.inbox").info(
            "[social-merge] merged %d new platforms into row %s (total platforms: %d)",
            len(new_posts), existing["id"], len(merged_posts),
        )
        if updated.data:
            return updated.data[0]
        return existing
    except Exception as e:
        logging.getLogger("aria.inbox").warning(
            "[social-merge] merge update failed: %s", e,
        )
        return None


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

    # ── Pre-insert gates ──────────────────────────────────────────────
    # Cheap, content-only checks that can reject the write outright before
    # we spend time parsing / looking things up. Each returns a terminal
    # response so the endpoint stays a flat sequence of guards.
    if _looks_like_confirmation_message(body.content):
        logging.getLogger("aria.inbox").info(
            "[inbox-create] rejecting confirmation/status message from %s "
            "(content=%r)", body.agent, (body.content or "")[:120],
        )
        return {"item": None, "skipped": "confirmation_message"}

    if _is_duplicate_media_write(tenant_id, body):
        # Legacy skill curl writing a media summary AFTER the canonical
        # row was already created by /api/media/.../generate.
        return {"item": None, "skipped": "duplicate_media_write"}

    # Normalize the agent slug in place so everything downstream (dedup
    # queries, stored row, emitted socket payload) sees one canonical
    # form regardless of whether the skill curl sent "email-marketer",
    # "Email Marketer", or "email_marketer".
    body.agent = _canon_agent_slug(body.agent) or body.agent

    # Strip agent meta-commentary from content before anything else so
    # leaks like "Posts delivered to ARIA inbox item: <uuid>" never
    # land in the row. Sanitizer patterns cover "saved to ARIA inbox",
    # "(inbox item <uuid>)", "Status: needs_review", **Post summary:**
    # headers, and the other known meta phrases.
    sanitized = _sanitize_social_post_text(body.content or "")
    if sanitized and sanitized != body.content:
        body.content = sanitized

    # ── Social-post merge-window dedup ────────────────────────────────
    # If the same tenant+agent posted a social_post row in the last 90
    # seconds, merge the new platforms into the existing row's posts
    # array instead of creating a new row. This is the safety net for
    # agents that split platforms into multiple POSTs (LinkedIn row,
    # X row, Facebook row) — the frontend can only render the
    # platform-card UI when all platforms live in a single row.
    if body.agent == "social_manager" and body.type == "social_post":
        merged = _merge_into_recent_social_row(tenant_id, body)
        if merged:
            return {"item": merged, "merged": True}

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
        # Scrub internal-plumbing leaks from skill-curl submissions too.
        # The agent's `aria-backend-api` skill posts its raw reply here
        # when it takes the direct-write path (Path A in CLAUDE.md), so
        # the same sanitizer the watcher path runs needs to fire here or
        # the "delivery summary" / Supabase URL noise survives to the UI.
        if content_type == "social_post":
            try:
                from backend.agents.social_manager_agent import (
                    _parse_posts as _sm_parse,
                    _sanitize_social_text as _sm_sanitize,
                )
                parsed_posts = _sm_parse(body.content)
                if parsed_posts:
                    import json as _json_inline
                    body.content = _json_inline.dumps({
                        "action": "adapt_content",
                        "posts": parsed_posts,
                    })
                else:
                    cleaned = _sm_sanitize(body.content)
                    if cleaned:
                        body.content = cleaned
            except Exception:
                pass

    # ── Best-effort dedupe based on recent activity ────────────────────
    # When the agent doesn't send paperclip_issue_id (which is most of
    # the time today), still try to avoid creating obvious duplicates
    # within the same delegation. Two match strategies, in order:
    #
    #   1. A recent placeholder (status='processing', same tenant +
    #      agent) — this is the row the watcher created when dispatch
    #      started. Its content is a "X is working on..." stub, so a
    #      content-prefix compare will never match the real agent
    #      output. We still want to treat it as the target to update,
    #      otherwise we get the hyphenated-slug-style double row:
    #      placeholder stays as "Email Marketer is working on...", the
    #      skill curl creates a second row with the real email. Always
    #      update the most recent processing placeholder.
    #
    #   2. A recent completed row (same tenant + agent + first 100
    #      chars of content match) — handles the agent POSTing the
    #      same content twice.
    try:
        # 5-minute window catches the watcher placeholder that gets
        # updated 60-90s after the agent's skill curl posts. Previously
        # set to 60s and missed by 11s in production.
        recent_window = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        recent = (
            sb.table("inbox_items")
            .select("id,content,type,status,title")
            .eq("tenant_id", tenant_id)
            .eq("agent", body.agent)
            .gte("created_at", recent_window)
            .order("created_at", desc=True)
            .limit(8)
            .execute()
        )
        rows = list(recent.data or [])

        # Strategy 1: processing placeholder — always upgrade it.
        # Content isn't substantive enough to compare; the whole point
        # of the placeholder is to be a stub that the skill curl fills in.
        for r in rows:
            r_title = (r.get("title") or "").lower()
            is_placeholder = (
                r.get("status") == "processing"
                or " is working on" in r_title
            )
            if not is_placeholder:
                continue
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
                "[inbox-create] upgraded processing placeholder %s "
                "(agent=%s) with real agent output",
                r["id"], body.agent,
            )
            item_data = {"id": r["id"], "tenant_id": tenant_id, **update_data}
            if tenant_id:
                try:
                    await sio.emit("inbox_updated", {"action": "updated", "item": item_data}, room=tenant_id)
                except Exception:
                    pass
            return {"item": item_data, "deduped": True, "merged_placeholder": True}

        # Strategy 2: content-prefix match for double-POSTs of the same draft.
        for r in rows:
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

    # Completion log — so the Virtual Office Recent Activity panel shows
    # "task done" after the agent's earlier paperclip_dispatch row. Fire
    # for skill-curl writes that land here (path 1 in the docstring).
    if body.agent and item:
        try:
            from backend.orchestrator import log_agent_action as _log_agent_action
            await _log_agent_action(
                tenant_id, body.agent, "paperclip_completed",
                {"task": (item.get("title") or "")[:200], "inbox_item_id": item.get("id")},
            )
        except Exception:
            pass

    # Index the finalized row for long-term cross-session recall
    # (content_library mirror + Qdrant embedding). Best-effort.
    if item:
        try:
            from backend.services.content_index import index_inbox_row
            await asyncio.to_thread(index_inbox_row, {**item, "tenant_id": tenant_id})
        except Exception:
            pass

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
        filter_used = result.get("filter_used") or {}
        tenant_total = result.get("tenant_total")
        recent_fallback = result.get("recent_fallback") or []

        # Helper — render rows with the ID visible so the CEO can feed
        # it straight into schedule_task. The ID is what the scheduler
        # uses to link payload.inbox_item_id → the right draft; without
        # it the CEO has no way to reference a specific item.
        def _render_item_row(i: int, it: dict) -> str:
            title = it.get("title") or it.get("type") or "Item"
            iid = it.get("id") or "—"
            agent = it.get("agent") or "—"
            status = it.get("status") or "—"
            itype = it.get("type") or "—"
            return (
                f"{i}. **{title}** — {status} · {itype} · from {agent}\n"
                f"   id: `{iid}`"
            )

        if items:
            filter_note = ""
            if filter_used:
                parts = [f"{k}={v}" for k, v in filter_used.items()]
                filter_note = f" (filter: {', '.join(parts)})"
            lines = [f"**Inbox** — {len(items)} items{filter_note}\n"]
            for i, item in enumerate(items[:15], 1):
                lines.append(_render_item_row(i, item))
            return "\n".join(lines)

        # Empty — three cases, all in plain language. The CEO reads
        # whatever comes back and speaks to the user in-character;
        # never expose "tenant", "lookup", "records", or filter names.
        if recent_fallback:
            # Something exists in the inbox — just not what the strict
            # filter asked for. Show the recent list so the CEO can
            # identify the right one without having to re-query.
            lines = [
                f"Here are your {len(recent_fallback)} most recent inbox items — "
                "one of these is likely what you meant:\n",
            ]
            for i, item in enumerate(recent_fallback, 1):
                lines.append(_render_item_row(i, item))
            return "\n".join(lines)

        if tenant_total == 0:
            # Genuinely empty — no items ever.
            return "Your inbox doesn't have any drafts yet. Want me to have one of the agents create one?"

        # Inbox has items but both the filter and the fallback came
        # back empty in this moment (rare — usually a transient DB
        # blip). Keep the voice friendly and actionable.
        return (
            "I'm having a little trouble pulling up the latest drafts right now. "
            "Give me a moment and ask again, or tell me what you'd like me to check for specifically."
        )

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

    if action_name == "schedule_pending_draft":
        if result.get("error"):
            return f"**Couldn't queue the schedule:** {result['error']}"
        when = result.get("scheduled_at", "")
        when_human = when[:16].replace("T", " ") if when else "the requested time"
        return (
            f"**Locked in:** I'll schedule the draft for **{when_human}** "
            "the moment the Email Marketer's output lands. No need to ask me again."
        )

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


# ── Pending schedules — "create it AND schedule it" intents ───────────
#
# When a user says "write an email to X AND schedule it for April 18",
# the CEO can't emit `schedule_task` immediately because the inbox row
# doesn't exist yet (the sub-agent hasn't run). Instead the CEO emits
# `schedule_pending_draft` with the intended scheduled_at + agent, and
# we stash it here keyed by session_id. A background watcher polls for
# the resulting inbox row and fires `sched_service.create_task` the
# moment it shows up — so from the user's point of view, one request
# does both things without any follow-up prompt.
#
# Entry shape: {
#   tenant_id, agent, scheduled_at, task_type, platform, session_id,
#   created_at (iso), task_hint (optional substring match)
# }
_pending_schedules: dict[str, list[dict]] = {}


async def _watch_and_fire_pending_schedule(pending: dict) -> None:
    """Background coroutine: poll for the sub-agent's inbox row, then
    insert a scheduled_tasks row linking to it.

    Lives up to `PENDING_SCHEDULE_TIMEOUT` seconds (2 min). Polls every
    3s. On success emits a socket event so the chat / calendar UI
    updates without a refresh. On timeout silently gives up — the CEO
    prompt tells users to check back if the draft is very slow, so
    we don't need a loud failure path.
    """
    PENDING_TIMEOUT_S = 120
    POLL_INTERVAL_S = 3

    tenant_id = pending["tenant_id"]
    agent = pending["agent"]
    created_at = pending["created_at"]  # ISO string
    scheduled_at = pending["scheduled_at"]
    task_type = pending.get("task_type") or _task_type_for_agent(agent)
    platform = pending.get("platform", "")
    session_id = pending.get("session_id", "")
    task_hint = (pending.get("task_hint") or "").lower().strip()

    sb = _get_supabase()
    _log = logging.getLogger("aria.pending_schedule")
    deadline = datetime.now(timezone.utc).timestamp() + PENDING_TIMEOUT_S

    while datetime.now(timezone.utc).timestamp() < deadline:
        await asyncio.sleep(POLL_INTERVAL_S)
        try:
            rows = (
                sb.table("inbox_items")
                .select("id, title, content, type, status, created_at")
                .eq("tenant_id", tenant_id)
                .eq("agent", agent)
                .gte("created_at", created_at)
                .neq("status", "processing")  # ignore the watcher's placeholder
                .order("created_at", desc=True)
                .limit(5)
                .execute()
            )
            data = rows.data or []
            # Optional narrow-down by task_hint (e.g. "Hanz") so a
            # delegation that's unrelated to the pending schedule
            # doesn't steal it. ILIKE semantics — case-insensitive,
            # matches title or content.
            picked = None
            if task_hint:
                for r in data:
                    haystack = f"{r.get('title','')} {r.get('content','')}".lower()
                    if task_hint in haystack:
                        picked = r
                        break
            if not picked and data:
                picked = data[0]
            if not picked:
                continue

            # Insert the scheduled_tasks row. Reuse the same
            # sched_service the `schedule_task` action uses so the
            # Calendar page picks it up immediately.
            from backend.services import scheduler as sched_service
            payload = {"inbox_item_id": picked["id"]}
            if platform:
                payload["platform"] = platform
            title_for_schedule = picked.get("title") or f"Scheduled {task_type}"
            try:
                created = sched_service.create_task(
                    tenant_id=tenant_id,
                    task_type=task_type,
                    title=title_for_schedule,
                    scheduled_at=scheduled_at,
                    payload=payload,
                    created_by="ceo",
                    triggered_by_agent="ceo",
                )
                _log.info(
                    "[pending-schedule] fired: tenant=%s agent=%s item=%s at=%s",
                    tenant_id, agent, picked["id"], scheduled_at,
                )
                # Emit scheduled_task_created so the Calendar page
                # refetches via the same event every other scheduling
                # path uses.
                if isinstance(created, dict):
                    await _emit_scheduled_task_created(tenant_id, created.get("task"))
                await sio.emit("scheduled_pending_fired", {
                    "inbox_item_id": picked["id"],
                    "scheduled_at": scheduled_at,
                    "task_type": task_type,
                    "title": title_for_schedule,
                    "session_id": session_id,
                    "scheduled_task_id": (created or {}).get("task", {}).get("id") if isinstance(created, dict) else None,
                }, room=tenant_id)
                # Also write a notification so the CEO chat + sidebar
                # surface the confirmation without a manual refetch.
                await _notify(
                    tenant_id, "scheduled",
                    f"Scheduled '{title_for_schedule[:60]}' for {scheduled_at[:16].replace('T',' ')}",
                    body="",
                    href="/calendar",
                    category="status",
                    priority="normal",
                )
            except Exception as e:
                _log.warning("[pending-schedule] create_task failed: %s", e)
            # Done — whether success or failure, we've acted; stop polling.
            # Remove this entry from _pending_schedules so a second
            # matching inbox row doesn't double-fire.
            if session_id and session_id in _pending_schedules:
                _pending_schedules[session_id] = [
                    p for p in _pending_schedules[session_id] if p is not pending
                ]
            return
        except Exception as e:
            _log.debug("[pending-schedule] poll iteration failed: %s", e)

    # Timeout path — release the pending entry so it doesn't leak.
    if session_id and session_id in _pending_schedules:
        _pending_schedules[session_id] = [
            p for p in _pending_schedules[session_id] if p is not pending
        ]
    _log.info(
        "[pending-schedule] timed out waiting for %s agent=%s tenant=%s",
        session_id, agent, tenant_id,
    )


def _task_type_for_agent(agent: str) -> str:
    """Default task_type for a pending schedule based on which agent is
    producing the asset. Lets the CEO omit `task_type` in the common
    cases — email_marketer → send_email, social_manager → publish_post,
    everything else → reminder (user will see it in the Inbox).
    """
    mapping = {
        "email_marketer": "send_email",
        "social_manager": "publish_post",
    }
    return mapping.get(agent, "reminder")
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
    from backend.tools.claude_cli import call_claude, MODEL_OPUS
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

    # ── Recent activity injection ────────────────────────────────────
    # The CEO's killer failure mode: "I delegated an email, now I want
    # to schedule it" requires the CEO to know the inbox row's ID, but
    # the delegation is async — by the time the user follows up, the
    # ID exists in the DB but not in the CEO's context window. Fix:
    # on every chat turn, pre-fetch the last 5 inbox rows for this
    # tenant from the past 30 minutes and inline them into the system
    # prompt with their ids. Now "schedule THAT email" resolves without
    # the CEO needing to call read_inbox at all — the id is literally
    # visible in its own context.
    recent_activity = ""
    if tenant_id:
        try:
            _ra_sb = _get_supabase()
            _ra_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            _ra_rows = (
                _ra_sb.table("inbox_items")
                .select("id, agent, type, title, status, created_at, email_draft")
                .eq("tenant_id", tenant_id)
                .gte("created_at", _ra_cutoff)
                .order("created_at", desc=True)
                .limit(5)
                .execute()
            )
            if _ra_rows.data:
                _ra_lines = [
                    "\n## Recent Inbox Activity (last 30 min)",
                    "These are the items your team just produced. When the user says "
                    "\"it / that / the email / the draft / the last one\", pick the id "
                    "from this list — DO NOT call read_inbox again.",
                ]
                for r in _ra_rows.data:
                    title = (r.get("title") or r.get("type") or "Item")[:80]
                    agent = r.get("agent") or "—"
                    rtype = r.get("type") or "—"
                    status = r.get("status") or "—"
                    # Include recipient for email rows — helps the CEO
                    # match "schedule the Hanz email" to the right id.
                    recipient = ""
                    draft = r.get("email_draft") or {}
                    if isinstance(draft, dict) and draft.get("to"):
                        recipient = f" → {draft['to']}"
                    _ra_lines.append(
                        f"- id: `{r['id']}` · {title}{recipient} · {rtype} · {status} · from {agent}"
                    )
                recent_activity = "\n".join(_ra_lines) + "\n"
        except Exception as e:
            logging.getLogger("aria.ceo_chat").debug(
                "[ceo-chat] recent_activity fetch failed for %s: %s", tenant_id, e,
            )

    # ── Stagnation Monitor: stale items awaiting user action ──────────
    # Drafts that have been sitting in needs_review / draft_pending_approval
    # / ready for >24h and aren't currently snoozed. The CEO references
    # these on the first message of a session ("Hey, your LinkedIn draft
    # from yesterday is still waiting...") so buried tasks don't get
    # forgotten when newer work piles on top. Per spec we only nudge when
    # the user is already active in the app — this injection only fires
    # when they actually open a chat session.
    stale_items_block = ""
    if tenant_id and is_first_message:
        try:
            from backend.services.projects import find_stale_items, format_stale_for_ceo_prompt
            _stale_rows = await asyncio.to_thread(find_stale_items, tenant_id, limit=8)
            stale_items_block = format_stale_for_ceo_prompt(_stale_rows)
        except Exception as e:
            logging.getLogger("aria.ceo_chat").debug(
                "[ceo-chat] stale_items fetch failed for %s: %s", tenant_id, e,
            )

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

    # Current date/time — injected so the CEO can resolve natural-language
    # scheduling like "tomorrow at 1 PM", "next Monday", "in 2 hours" to the
    # absolute ISO 8601 timestamp required by the schedule_task action.
    _now = datetime.now(timezone.utc)
    _today_str = _now.strftime("%A, %B %d, %Y (%Y-%m-%d)")
    _now_iso = _now.isoformat()

    system_prompt = f"""{_CEO_MD}
{business_context}{crm_context}{recent_activity}{stale_items_block}
## Current Date & Time
Today is {_today_str}. Current UTC time: {_now_iso}.
When the user says "tomorrow", "next Monday", "in 2 hours", "April 18", etc., compute the absolute ISO 8601 timestamp from this reference point and use it verbatim in `scheduled_at` fields.

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

### Pipeline delegations (when the user asks for a multi-step chain)
For asks that naturally span two agents in one breath ("create a product image AND use it in a launch email", "write a blog post AND post it to social"), emit ONE delegate block with a `then` field. The dispatcher runs step 1 immediately, waits 90 seconds (configurable via `delay_seconds` on the follow-up), then runs step 2 — by which time the upstream agent's output is in the inbox and the downstream agent will find it automatically via asset_lookup (images, blog posts, email hooks).

```delegate
{{"agent": "media", "task": "product hero image: ...", "then": {{"agent": "email_marketer", "task": "launch email with the hero image", "delay_seconds": 90}}}}
```

Valid pipeline patterns:
- `media` → `email_marketer` (image-in-email)
- `media` → `social_manager` (image-in-post)
- `media` → `ad_strategist` (image-in-ad)
- `content_writer` → `email_marketer` (blog digest email)
- `content_writer` → `social_manager` (blog → thread)
- Campaign bundles (up to 6 chained steps): `media` → `content_writer` → `email_marketer` → `social_manager`, etc. When the user asks for a "launch", "campaign", or "full bundle", emit a multi-step pipeline with `delay_seconds: 60-120` between steps so each agent's output is indexed before the next runs.

This is the ONLY way to emit more than one agent in a single turn — two separate `delegate` blocks in the same reply is still a bug (it triggers the "accidentally fired two agents" alert). Use `then` for intentional chains; otherwise stick to one block.

### Referencing prior work (source_inbox_item_ids)
When the user refers to something already in the Inbox — "the banner from earlier", "my last email to Hanz", "combine the banner and the blog post we wrote yesterday" — attach the specific id(s) to the delegation via `source_inbox_item_ids`. The backend will fetch each row, extract the image URL / email subject / blog body, and append it to the task description so the sub-agent has the concrete asset alongside its own task.

How to find the id:
1. Check the "Recent Inbox Activity" block FIRST (it lists the last 5 items with ids).
2. If the referenced asset isn't in Recent Activity, call `read_inbox` with `params.search="<keyword>"` to fuzzy-match the title/content.

Example — user: "Write a LinkedIn post using the SMAPS banner from this morning"
Recent Activity shows: `id: 7af3... | type: image | title: SMAPS banner`
```delegate
{{"agent": "social_manager", "task": "LinkedIn post about SMAPS launch", "source_inbox_item_ids": ["7af3..."]}}
```

Example — user: "Turn the blog we wrote last week and the SMAPS banner into a Facebook ad"
Call `read_inbox` with `params.search="SMAPS"` to get the banner id, and `params.search="blog"` (or scan Recent Activity) for the blog id. Then:
```delegate
{{"agent": "ad_strategist", "task": "Facebook ad combining the blog talking points with the banner as hero image", "source_inbox_item_ids": ["7af3...", "blog-9c2..."]}}
```

Pass up to 5 ids per delegation. Omit the field entirely when the user is asking for fresh generation with no back-reference — the agent's own short-window lookups cover the "just made it, use it now" case.

### One Delegate Per Message — HARD RULE
Each user message gets EXACTLY ONE delegate block, never two. Do NOT chain delegations like "media for the image AND content_writer for a caption". If the user asked for ONLY an image, delegate ONLY to media. Bonus content the user did not ask for (captions, blog copy, social posts about the image) is forbidden — never auto-add a content_writer/social_manager delegate alongside a media one.

Concrete example of the violation — DO NOT DO THIS:
User: "create an image of a cat"
❌ WRONG: `{{"agent": "media", "task": "cat image", "then": {{"agent": "content_writer", "task": "blog post about cats"}}}}`  (user did not ask for a blog post)
✅ CORRECT: `{{"agent": "media", "task": "cat image"}}`

The `then` field is ONLY valid when the user's message contains an explicit compound request with a text companion word: "blog", "post", "email", "caption", "write", "social", "launch", "campaign". Absent those, NO `then` field.

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

### Create-AND-schedule in ONE turn (schedule_pending_draft)
When the user says BOTH things in the same message — "create X AND schedule it for Y" — emit TWO blocks in your reply:

1. The normal `delegate` block for the create (media / email_marketer / etc.).
2. An `action` block for `schedule_pending_draft` with `scheduled_at` (ISO 8601) and `agent` (same one you just delegated to).

The backend auto-fires the scheduled_task row the moment the sub-agent's inbox output lands — you do NOT need to wait for a follow-up turn from the user. Works for ALL sub-agents (email_marketer, content_writer, social_manager, ad_strategist, media).

Optional but helpful: pass `task_hint` with a distinctive substring from the user's ask (e.g. "Hanz", "SMAPS", "product launch"). If there are several concurrent drafts, the hint narrows the match to the right one.

Example — user says "create a marketing email for Hanz and schedule it for April 18 at 11 AM":
```delegate
{{"agent": "email_marketer", "task": "SEND: marketing email to Hanz (hdlcruz03@gmail.com) about SMAPS-SIS", "priority": "medium"}}
```
```action
{{"action": "schedule_pending_draft", "params": {{"agent": "email_marketer", "scheduled_at": "2026-04-18T11:00:00+00:00", "task_hint": "Hanz"}}}}
```

Response to the user: "Got it — I'll have the Email Marketer draft the email now and lock in April 18 at 11 AM. It'll schedule automatically the moment the draft lands. No need to remind me."

### Scheduling workflow (schedule_task / reschedule_task)
The user may ask "schedule that email for tomorrow at 1 PM", "remind me next Monday", "send this Friday 9 AM", etc.

**HARD RULE:** A scheduling request MUST be answered with a `schedule_task` action block. A prose-only reply like "Got it, I'll schedule it" will NOT create the calendar entry — the DB write only happens when the action block runs. Never confirm a schedule in words without also emitting the block in the same reply.

Example — user says "schedule the latest email for 10 AM April 18" and Recent Inbox Activity shows `id: 7af3... | title: Marketing email to Hanz (SMAPS-SIS)`:
```action
{{"action": "schedule_task", "params": {{"task_type": "send_email", "title": "Marketing email to Hanz (SMAPS-SIS)", "scheduled_at": "2026-04-18T10:00:00+00:00", "payload": {{"inbox_item_id": "7af3..."}}}}}}
```
Response: "Locked in — the Hanz email will send April 18 at 10:00 AM."

Steps:
1. Resolve the natural-language time using the "Current Date & Time" block above. Output format MUST be ISO 8601 with timezone (e.g. `2026-04-18T13:00:00+00:00`). Never use placeholders.
2. **Check the "Recent Inbox Activity" block FIRST.** If the user said "it / that / the email / the last one / the draft / the latest", pick the most recent matching row's id and schedule it immediately. You should NOT call `read_inbox` when the answer is already in your context — the activity list above is the source of truth for everything produced in the last 30 minutes.
3. Only call `read_inbox` when:
   (a) The Recent Activity block is empty (nothing happened in the last 30 min), OR
   (b) The user referenced something specific that isn't in the recent list ("the Hanz email from yesterday"). For targeted lookups use `read_inbox` with `params.search = "<name or topic>"` — it fuzzy-matches against title and content so "the Hanz one" finds the row with Hanz in it without needing the full title.
4. If exactly ONE candidate matches (either in Recent Activity or in the read_inbox result), assume that's what the user meant and schedule it. Don't ask. Disambiguation is only needed when >=2 plausible candidates exist.
5. task_type values: `send_email` (payload needs inbox_item_id), `publish_post` (payload needs inbox_item_id + platform), `reminder` (payload needs inbox_item_id + title + body).
6. If the Recent Activity block AND `read_inbox` both come back empty, the draft is likely still being written. Say (warmly, in your own words): "I'm just waiting for the draft to land in the Inbox. I'll schedule it the second it arrives — want me to go ahead and lock in the time of April 18, 11 AM so it fires the moment it's ready?" Then emit `schedule_pending_draft` with the time + agent so the backend auto-fires when the draft arrives. Take ownership; never blame a sub-agent.
7. Never fabricate an id. If read_inbox returns 2+ plausible candidates, name them briefly ("the Checking-in email to Hanz or the SMAPS-SIS demo?") and let the user pick.

### Voice + language (founder ↔ CEO)
You are speaking to a founder about their marketing team. Keep the tone peer-to-peer, warm, and concrete. DO NOT use these words in user-facing replies, ever:
- "tenant", "tenant_id", "records", "rows", "query", "lookup", "endpoint", "null", "fallback", "filter", "500ms", "Supabase", "Paperclip", "orchestrator", "payload", "cascade", "the database"
- "The Email Marketer hasn't finished" / any phrasing that blames a sub-agent. The agents are your team — speak for them.

When something goes wrong internally, rephrase it as your own temporary hiccup and offer to keep trying. Examples:
- ✅ "Give me a sec — I'm pulling up the latest drafts."
- ✅ "I'm having a little trouble accessing the latest drafts right now. Let me try again for you."
- ✅ "I can't see that draft in your Inbox yet. Want me to ask the Email Marketer to write it now, or should I keep checking?"
- ❌ "The lookup came back empty."
- ❌ "The tenant has records but the filter returned nothing."
- ❌ "Try again in a moment."

### Cross-agent: images in emails
If the user asks for an email WITH an image/photo/banner/visual, the
Email Marketer automatically attaches the most recent Media Agent image
(if one was generated in the last 30 minutes for this tenant). So:

- "Create a product launch email with a hero image" → if you generated an
  image in the last turn, delegate ONLY to email_marketer with "include
  image" in the task text. The email_marketer will find the image and
  inline it at the top of the HTML body.
- "Create an email with an image" (no prior image exists) → delegate to
  `media` FIRST to generate the image. Tell the user: "I'll create the
  image first, then you can ask me to put it in an email." Do NOT emit a
  second delegate in the same turn — one delegate per message.
- If the user pastes an image URL into chat, include it verbatim in the
  email_marketer task ("...with image: https://.../hero.png") and the
  agent will embed that exact URL.

### Email reply workflow (draft_email_reply)
When the user asks you to REPLY to an existing email ("reply to X's email", "write back to Y saying ...", "respond to the last email from Z"):

1. Call `read_email_threads` first to locate the thread. Match on contact email or, if the user references "the last reply", use the thread whose `status` is `needs_review` or has the most recent `last_message_at`.
2. Emit `draft_email_reply` with `params.thread_id` set to the matched thread's id. If the user gave specific instructions ("tell them we can meet Friday", "decline politely"), pass them as `params.custom_instructions`.
3. The draft goes to the Inbox as `draft_pending_approval`. Tell the user where to find it and that approving it will send on the ORIGINAL Gmail thread (not a new conversation).
4. Never skip step 1 — you cannot guess `thread_id`. If `read_email_threads` returns nothing matching, tell the user instead of making one up.
5. Reply requests NEVER go through the `delegate` block. `draft_email_reply` is a business action, not a sub-agent delegation.

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
            model=MODEL_OPUS,
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

    # Parse delegation blocks.
    #
    # Pipeline support: a delegate block can carry an optional `then`
    # field pointing at a follow-up delegation. The CEO uses this for
    # media -> email / media -> social / content_writer -> email kinds
    # of chains in a single turn — the prompt's "one delegate per
    # message" rule treats a pipeline as ONE intentional delegation
    # even though it produces multiple sub-agent runs.
    #
    # Shape the CEO emits for a chain:
    #   {"agent":"media","task":"...", "then":{"agent":"email_marketer","task":"...","delay_seconds":90}}
    #
    # We flatten that into sequential entries on the `delegations` list,
    # each tagged with `_delay_seconds` (cumulative) so the dispatcher
    # below knows when each step should fire. Later steps rely on the
    # earlier steps' outputs being already indexed — asset_lookup's
    # get_latest_image_url / get_recent_blog_post / get_recent_email_hook
    # will find them once the inbox row has landed.
    _VALID_AGENTS = ("content_writer", "email_marketer", "social_manager", "ad_strategist", "media")
    delegations = []
    clean_response = raw
    if "```delegate" in raw:
        for block in _DELEGATE_BLOCK_RE.findall(raw):
            d = _parse_codeblock_json(block, "delegate")
            if not d or d.get("agent") not in _VALID_AGENTS:
                continue
            # Unroll the .then chain. Cap at 6 steps so a malformed
            # response can't produce an arbitrarily long pipeline — 6 is
            # the high end of a realistic campaign bundle (hero image +
            # blog + landing page + launch email + 2-3 social posts).
            chain: list[dict] = []
            current = d
            for _ in range(6):
                chain.append({k: v for k, v in current.items() if k != "then"})
                nxt = current.get("then")
                if not isinstance(nxt, dict) or nxt.get("agent") not in _VALID_AGENTS:
                    break
                current = nxt
            # Guard: trim auto-chained follow-ups when the user asked for
            # a single deliverable. The CEO prompt forbids chaining
            # content_writer/social_manager onto a media delegation when
            # the user only asked for an image, but the model violates
            # that rule fairly often — adding a caption, blog post, or
            # social variant the user never requested. Detect image-only
            # intent in the user message and drop chain[1:] in that case.
            if len(chain) > 1 and chain[0].get("agent") == "media":
                msg_lower = (body.message or "").lower()
                _IMAGE_WORDS = ("image", "picture", "photo", "banner", "logo",
                                "illustration", "graphic", "mockup", "thumbnail",
                                "header", "visual", "drawing", "artwork", "png",
                                "jpg", "jpeg")
                _TEXT_COMPANIONS = ("blog", "post", "email", "caption", "tweet",
                                    "social", "write ", "draft ", "newsletter",
                                    " ad ", "launch", "campaign", "bundle",
                                    " also", " plus ", " then ", "description",
                                    " copy ", "content", "article")
                has_image = any(w in msg_lower for w in _IMAGE_WORDS)
                has_companion = any(w in msg_lower for w in _TEXT_COMPANIONS)
                if has_image and not has_companion:
                    dropped = [s.get("agent") for s in chain[1:]]
                    logging.getLogger("aria.ceo_chat").warning(
                        "[delegate-guard] trimming media->%s chain for image-only ask: %r",
                        "+".join(dropped), body.message[:120],
                    )
                    chain = chain[:1]
            # Pipeline image-flag: when any earlier step in the chain is
            # `media`, tag subsequent steps so the downstream agents
            # pull the freshly-generated image even if the CEO's task
            # text didn't explicitly say "image". Without this, a chain
            # like `media -> social_manager` only attaches the image
            # when the social task happened to contain the word "image".
            saw_media = False
            cumulative = 0
            for i, step in enumerate(chain):
                if i > 0:
                    cumulative += int(step.get("delay_seconds") or 90)
                step["_delay_seconds"] = cumulative
                if saw_media and step.get("agent") != "media":
                    step["_pipeline_has_media_image"] = True
                if step.get("agent") == "media":
                    saw_media = True
                delegations.append(step)
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
            # Auto-inject session_id for actions that need it — the CEO
            # doesn't know its own session_id, so we stamp it in at
            # dispatch time. Currently only schedule_pending_draft uses
            # it (to scope pending schedules to this specific chat).
            if action_name == "schedule_pending_draft" and body.session_id:
                params = {**params, "session_id": body.session_id}
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

                # Calendar sync: when the CEO's action actually inserted
                # a scheduled_tasks row, fire the socket event so the
                # Calendar page refetches immediately. Handles the direct
                # `schedule_task` create path; the pending-schedule
                # watcher fires its own emit from
                # _watch_and_fire_pending_schedule.
                if action_name == "schedule_task" and isinstance(result, dict):
                    task_payload = result.get("result", {}).get("task") if "result" in result else result.get("task")
                    if task_payload:
                        await _emit_scheduled_task_created(tenant_id, task_payload)

    # Append formatted action results to the response so data appears in chat
    for ar in action_results:
        if ar.get("status") not in ("executed", "error"):
            continue
        action_name = ar.get("action", "")
        data = ar.get("result", {}) if ar["status"] == "executed" else {"error": ar.get("message", "Unknown error")}
        formatted = _format_action_result(action_name, data)
        if formatted:
            clean_response = clean_response.rstrip() + "\n\n" + formatted

    # Persist delegations on the in-memory turn too, not just in the DB.
    # The /history endpoint prefers the in-memory cache for speed, so if
    # we only wrote delegations to Postgres the user would see the
    # delegation chips on the initial reply but lose them on refresh.
    session.append({
        "role": "assistant",
        "content": clean_response,
        "delegations": delegations or [],
    })

    # Persist assistant message to DB
    _save_chat_message(body.session_id, tenant_id, "assistant", clean_response, delegations)

    # No delegations — CEO meeting is over, return to idle
    if not delegations and tenant_id:
        await _emit_agent_status(tenant_id, "ceo", "idle",
                                 action="chat_response_sent")

    # Save delegations as tasks, emit status events, and execute in background.
    #
    # Delegations tagged with `_delay_seconds > 0` are pipeline follow-up
    # steps — we defer their dispatch (task row insert, status emit, and
    # agent run) until the delay expires so the upstream agent has time
    # to land its output in the inbox first. This is what makes
    # "media -> email" work as a single-turn chain: the email step runs
    # after the media step's image row is already queryable.
    saved_tasks: list[dict] = []

    for d in delegations:
        delay = int(d.get("_delay_seconds") or 0)
        if delay > 0:
            # Pipeline follow-up — run the whole body in the background
            # after the delay so the HTTP response doesn't block. The
            # `saved_tasks` list doesn't catch the follow-up's task row
            # (it fires after the HTTP response), which is fine — the
            # Kanban UI picks it up via the socket event anyway.
            _safe_background(
                _execute_delegation(tenant_id, body.session_id, d, delay, None),
                label=f"pipeline-{d.get('agent')}-{delay}s",
            )
        else:
            await _execute_delegation(tenant_id, body.session_id, d, 0, saved_tasks)

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


class BulkDeleteSessionsRequest(BaseModel):
    session_ids: list[str]


@app.post("/api/ceo/chat/sessions/{tenant_id}/bulk-delete")
async def bulk_delete_chat_sessions(tenant_id: str, body: BulkDeleteSessionsRequest):
    """Bulk-delete multiple chat sessions in a single Supabase round-trip.

    Uses `.in_("id", session_ids)` so the whole operation is one DELETE
    regardless of how many rows are being removed. chat_messages cascade
    via the existing ON DELETE CASCADE FK.

    Tenant-scoped: the query filters by tenant_id so a caller can't
    delete sessions that don't belong to them even if they guessed the
    ids. Idempotent on "already gone" — the response reports the count
    of rows that actually matched at delete-time so the UI can show
    accurate feedback.
    """
    sb = _get_supabase()
    ids = [sid for sid in (body.session_ids or []) if isinstance(sid, str) and sid]
    if not ids:
        return {"ok": True, "deleted": 0}

    # Verify every id we're about to delete belongs to this tenant.
    # Filter the incoming ids down to the ones that actually match,
    # then run the DELETE against that safe list. That way a forged id
    # for another tenant is silently dropped (not 403'd) so a bulk
    # request with one bad id still processes the good ones.
    owned = (
        sb.table("chat_sessions")
        .select("id")
        .eq("tenant_id", tenant_id)
        .in_("id", ids)
        .execute()
    )
    safe_ids = [r["id"] for r in (owned.data or [])]
    if not safe_ids:
        return {"ok": True, "deleted": 0}

    sb.table("chat_sessions").delete().in_("id", safe_ids).execute()

    # Drop in-memory session state for deleted sessions.
    for sid in safe_ids:
        try:
            _chat_sessions.pop(sid, None)
            _chat_session_locks.pop(sid, None)
        except Exception:
            pass

    return {"ok": True, "deleted": len(safe_ids), "deleted_ids": safe_ids}


@app.delete("/api/ceo/chat/sessions/{tenant_id}/{session_id}")
async def delete_chat_session(tenant_id: str, session_id: str):
    """Hard-delete a CEO chat session.

    The chat_messages table has ON DELETE CASCADE on its session_id
    foreign key (see backend/sql/create_chat_tables.sql), so dropping
    the session row also drops every message attached to it. No
    orphan messages are left behind.

    Scoped by tenant_id so a caller can't delete another tenant's
    session even if they guessed the session_id. Also clears any
    in-process chat lock for that session_id so the next fresh
    session can take its slot without a stale mutex.
    """
    sb = _get_supabase()

    # Verify tenant ownership before deleting — session_ids are tenant-
    # prefixed (`chat_{tenant_id}_{ts}`) but we still double-check.
    row = (
        sb.table("chat_sessions")
        .select("id,tenant_id")
        .eq("id", session_id)
        .limit(1)
        .execute()
    )
    if not row.data:
        # Idempotent: if the row is already gone, treat as success so
        # double-clicks from the UI don't surface a 404.
        return {"ok": True, "deleted": 0}
    if row.data[0].get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")

    sb.table("chat_sessions").delete().eq("id", session_id).execute()

    # Drop the in-memory session lock + history for this session so the
    # backend doesn't keep stale state around after the DB row is gone.
    try:
        _chat_sessions.pop(session_id, None)
        _chat_session_locks.pop(session_id, None)
    except Exception:
        pass

    return {"ok": True, "deleted": 1}


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


# ─── Stagnation Monitor / "Buried Task" API ───
@app.get("/api/projects/stale/{tenant_id}")
async def list_stale_projects(tenant_id: str, hours: int = 24, limit: int = 20):
    """Return inbox drafts that have been waiting on the user for more
    than `hours` (default 24h), excluding rows that are currently
    snoozed. Powers the Priority Actions section on the Projects page
    and the sidebar pulse badge.

    Also returns `recent_count` (items created in the last 24h) so the
    frontend can decide whether the stale items are "buried" (per spec:
    when there are 5+ newer items, the sidebar should pulse harder)."""
    from backend.services.projects import find_stale_items, count_recent_items

    rows = await asyncio.to_thread(
        find_stale_items, tenant_id, hours_old=max(1, hours), limit=min(max(1, limit), 50),
    )
    recent_count = await asyncio.to_thread(count_recent_items, tenant_id, hours=24)
    return {
        "stale_items": rows,
        "stale_count": len(rows),
        "recent_count": recent_count,
        "is_buried": len(rows) > 0 and recent_count >= 5,
        "hours_threshold": hours,
    }


@app.post("/api/projects/{tenant_id}/snooze/{item_id}")
async def snooze_stale_project(tenant_id: str, item_id: str, payload: dict = Body(default={})):
    """Snooze a stale row for `hours` (default 24, capped at 168 = 1
    week so the user can't accidentally hide a draft forever). The row
    isn't marked done — just hidden from the stagnation feed until the
    snooze expires. Per spec: 'they must remain Incomplete until the
    user explicitly acts.'"""
    from backend.services.projects import snooze_item

    hours = int((payload or {}).get("hours", 24))
    hours = max(1, min(hours, 168))
    result = await asyncio.to_thread(snooze_item, tenant_id, item_id, hours=hours)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Snooze failed")
    return result


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
