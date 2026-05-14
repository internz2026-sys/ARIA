"""ARIA FastAPI Server — webhooks, chat, agent management, dashboard API."""
from __future__ import annotations

import asyncio
import hmac
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

from backend.auth import get_current_user, get_verified_tenant, check_rate_limit, get_user_id_from_jwt

load_dotenv()

# Install the global log-redaction filter as early as possible so any
# secrets that surface in startup logs (e.g. a connection string in an
# exception during a DB warmup) are scrubbed before stdout flush.
from backend.services.log_redaction import install_global_filter as _install_log_redaction
_install_log_redaction()

logger = logging.getLogger("aria.server")

# _safe_oauth_error moved to backend/routers/auth_oauth.py.


from backend.services.approval import requires_approval, validate_execution, ACTION_POLICIES
from backend.config.loader import get_tenant_config, save_tenant_config
from backend.services.supabase import get_db as _get_supabase
# OnboardingAgent + FIELD_QUESTIONS now imported inside backend/routers/onboarding.py
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
    PlanQuotaExceeded,
)
from backend.orchestrator import is_connected as paperclip_connected
from backend.agents import AGENT_REGISTRY
from backend.services.paperclip_chat import normalize_comments, pick_agent_output
from backend.services.paperclip_office_sync import (
    _is_finished,
    _is_failed,
    _add_processed,
    poll_completed_issues,
    sync_agent_statuses,
)

# ── CORS — restrict to known frontend origins ────────────────────────────
# Production sets CORS_ALLOWED_ORIGINS to a comma-separated list. The
# fallback below is dev-only; the previous fallback included a stale
# Vercel preview origin (aria-alpha-weld.vercel.app) that hadn't been
# deployed in months — if the env var ever got cleared, that preview
# would silently become the only allowed cross-origin source. Removed
# in security audit item #15. Localhost stays for local frontend dev.
_allowed_origins = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
] or [
    "http://localhost:3000",
]

# Socket.IO singleton + the stateless emit helpers live in
# backend/services/realtime.py so any router can import them normally
# without circular-load workarounds. Re-exported under their original
# names so existing in-file references keep working.
from backend.services.realtime import (
    sio,
    emit_task_completed as _emit_task_completed,
    agent_display_name as _agent_display_name,
)


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
    """Background loop: sync Gmail inbound replies.

    Disabled by default — we dropped `gmail.readonly` from the OAuth
    scope set to skip the CASA Tier-2 security audit that Google
    requires for Restricted scopes. Inbound replies now come in via
    reply-to routing through an ARIA-owned mailbox + Postmark/SendGrid
    webhook (see docs/email-inbound-routing.md). This loop will fail
    every iteration with a 403 from Gmail's API since tokens no longer
    have read scope, so we no-op it unless GMAIL_READONLY_ENABLED=1
    is explicitly set (e.g., for legacy tenants whose tokens still
    carry readonly grants from before the scope was removed).
    """
    if os.environ.get("GMAIL_READONLY_ENABLED", "").lower() not in ("1", "true", "yes"):
        logging.getLogger("aria.gmail_sync_loop").info(
            "Gmail readonly sync disabled — inbound replies handled via "
            "reply-to routing webhook. Set GMAIL_READONLY_ENABLED=1 to "
            "re-enable polling for legacy tenants."
        )
        return

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
    """Background loop: execute due scheduled tasks every 30 seconds.

    Also scans the `campaigns` table once per minute for 7-day Copy-Paste
    performance review reminders (Task A in the campaigns workstream).
    A campaign is "due" when `metadata.performance_review_at` is in the
    past AND `metadata.performance_review_fired` is not truthy. We fire
    a notification and set the fired flag so we don't re-notify.

    The reminder scan runs every other tick (~60s) instead of every
    tick — the work is cheap, but we don't need second-level
    responsiveness for "you set this 7 days ago" prompts. Counter is
    process-local so a container restart re-aligns it (intentional;
    a brief duplicate-tick window after restart is harmless because
    of the `performance_review_fired` flag).
    """
    from backend.services.scheduler import get_due_tasks, execute_task
    from backend.services.campaigns import (
        list_due_performance_reviews,
        mark_performance_review_fired,
    )
    _log = logging.getLogger("aria.scheduler_executor")
    review_tick = 0
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

        # ── Performance review reminders (every other tick, ~60s) ──
        review_tick += 1
        if review_tick % 2 != 0:
            continue
        try:
            due_reviews = list_due_performance_reviews(limit=100)
        except Exception as e:
            _log.warning("Performance review scan failed: %s", e)
            due_reviews = []
        for camp in due_reviews:
            tid = camp.get("tenant_id", "")
            cid = camp.get("id", "")
            name = (camp.get("campaign_name") or "Untitled Campaign")[:120]
            if not tid or not cid:
                continue
            try:
                sb = _get_supabase()
                sb.table("notifications").insert({
                    "tenant_id": tid,
                    "title": f"Performance Review: {name}",
                    "body": (
                        "It's been 7 days since you launched this campaign — "
                        "time to review the metrics"
                    ),
                    "category": "campaign",
                    "href": f"/campaigns/{cid}",
                }).execute()
            except Exception as e:
                _log.warning(
                    "Failed to write performance review notification for %s: %s",
                    cid, e,
                )
                # Don't flip the fired flag — let the next tick retry.
                continue
            # Flag the campaign so we don't re-notify on the next sweep.
            mark_performance_review_fired(tid, cid)
            try:
                await sio.emit("notification_created", {
                    "category": "campaign",
                    "title": f"Performance Review: {name}",
                    "href": f"/campaigns/{cid}",
                }, room=tid)
            except Exception:
                pass
        if due_reviews:
            _log.info(
                "Scheduler: fired %d performance review reminders",
                len(due_reviews),
            )


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


# _looks_like_confirmation_message moved to backend/routers/inbox.py (slice 5).


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
    # IMAP inbound poller — pulls customer replies from the SMTP mailbox
    # (aria@<domain>) into ARIA's email_threads / email_messages so the
    # Conversations page lights up. No-op when IMAP_HOST / SMTP_USER /
    # SMTP_PASSWORD are not configured.
    from backend.services.imap_inbound import imap_poll_loop
    imap_task = asyncio.create_task(imap_poll_loop())
    yield
    sync_task.cancel()
    scheduler_task.cancel()
    office_sync_task.cancel()
    followup_task.cancel()
    repurpose_task.cancel()
    imap_task.cancel()
    # Close the shared Paperclip httpx client so we don't leak connections
    # on graceful shutdown (uvicorn reload during dev, container stop in prod).
    try:
        from backend.orchestrator import close_httpx_client
        await close_httpx_client()
    except Exception as e:
        logger.warning("Failed to close orchestrator httpx client: %s", e)


# Gate the API documentation surface in production. /docs (Swagger UI),
# /redoc, and /openapi.json expose every route, request schema, response
# shape, and Pydantic validator in the codebase. Useful in dev; pure
# reconnaissance fodder in production. ENABLE_API_DOCS=true overrides
# the production gate when an operator needs them temporarily (e.g.
# debugging a third-party integration).
def _api_docs_enabled() -> bool:
    if (os.environ.get("ENABLE_API_DOCS") or "").lower() in ("1", "true", "yes"):
        return True
    return (os.environ.get("ARIA_ENV") or os.environ.get("ENV") or "").lower() not in ("prod", "production")


_DOCS_ENABLED = _api_docs_enabled()
app = FastAPI(
    title="ARIA API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)

# ── Register routers ──────────────────────────────────────────────────────
from backend.routers.crm import router as crm_router
from backend.routers.inbox import router as inbox_router
from backend.routers.campaigns import router as campaigns_router
from backend.routers.email import router as email_router
from backend.routers.admin import router as admin_router
from backend.routers.tasks import router as tasks_router
from backend.routers.ceo import router as ceo_router
from backend.routers.login_rate_limit import router as login_rate_limit_router
from backend.routers.reports import router as reports_router
from backend.routers.security_review import router as security_review_router
from backend.routers.plans import (
    profile_router as plans_profile_router,
    admin_router as plans_admin_router,
)
# NOTE: backend/routers/paperclip.py was a webhook receiver for the HTTP
# adapter experiment — we reverted to claude_local, so Paperclip never
# calls our webhook anymore. The agents now POST results back to ARIA via
# the aria-backend-api skill (which curls /api/inbox/{tenant}/items).

app.include_router(crm_router)
app.include_router(inbox_router)
app.include_router(campaigns_router)
app.include_router(email_router)
app.include_router(admin_router)
app.include_router(tasks_router)
app.include_router(ceo_router)
app.include_router(login_rate_limit_router)
app.include_router(reports_router)
app.include_router(security_review_router)
# Plans: self-service + admin override. Split into two routers so the
# self-service surface doesn't accidentally inherit the /api/admin/* role
# gate, while the admin override does.
app.include_router(plans_profile_router)
app.include_router(plans_admin_router)

# Routers extracted from server.py during the 2026-05 mechanical split.
from backend.routers import notifications as _notifications_router  # noqa: E402
from backend.routers import onboarding as _onboarding_router  # noqa: E402
from backend.routers import integrations as _integrations_router  # noqa: E402
from backend.routers import auth_oauth as _auth_oauth_router  # noqa: E402
from backend.routers import scheduling as _scheduling_router  # noqa: E402
from backend.routers import social as _social_router  # noqa: E402
from backend.routers import agents_runtime as _agents_runtime_router  # noqa: E402
from backend.routers import dashboard_analytics as _dashboard_analytics_router  # noqa: E402
app.include_router(_notifications_router.router)
app.include_router(_onboarding_router.router)
app.include_router(_integrations_router.router)
app.include_router(_auth_oauth_router.router)
app.include_router(_scheduling_router.router)
app.include_router(_social_router.router)
app.include_router(_agents_runtime_router.router)
app.include_router(_dashboard_analytics_router.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Public paths that don't require authentication ────────────────────────
# NOTE: the /api/onboarding/* endpoints USED to be public. They were moved
# behind JWT auth on 2026-05-07 (security audit MEDIUM finding) so a stolen
# session_id can't be replayed by another user. The session is now bound
# to the JWT user_id at /start time, and every subsequent /message,
# /skip, /extract-config, /save-config call verifies the JWT user matches
# the session's bound user.
#
# The /save-draft, /draft (GET), /draft (DELETE) endpoints were ALSO moved
# behind JWT auth on 2026-05-07 (CRITICAL audit findings #1-3): they
# previously trusted attacker-supplied user_id from the request, which
# defeated the /start hardening. user_id is now derived from the JWT.
#
# /api/onboarding/save-config-direct was the last public onboarding endpoint
# and was locked down on 2026-05-07 — owner_email is now derived from the
# JWT email claim, and writes against an existing_tenant_id verify ownership
# before overwriting. Body's owner_email field is ignored.
_PUBLIC_PATHS = {
    "/health",
    "/api/whatsapp/webhook",
    "/api/cron/run-scheduled",
}

_PUBLIC_PREFIXES = (
    "/api/auth/",           # OAuth callbacks (Twitter, LinkedIn)
    "/api/webhooks/",       # External webhooks (Stripe, SendGrid)
    "/api/inbox/",          # Inbox item creation (used by Paperclip agents)
    "/api/media/",          # Image generation (used by Paperclip Media Designer)
    # /api/tenant/by-email/ removed from public prefixes — it was a public
    # oracle for tenant_id discovery (anyone could enumerate emails and
    # get back the tenant_id, making IDOR trivial). Now requires JWT +
    # email-match (the caller's JWT email must equal the requested email).
    "/api/email/inbound",   # Inbound mail webhook (Postmark/Resend/SendGrid → /api/email/inbound)
    "/api/internal/",       # HMAC-gated internal endpoints (e.g. /security-review for GitHub Actions)
    # /docs + /openapi.json removed from public prefixes in #20 — the
    # FastAPI app constructor now sets docs_url/openapi_url=None in
    # production so the routes don't exist at all there. In dev they
    # don't need an entry here either, since they're under the auth
    # middleware's `not path.startswith("/api/")` early-return.
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
        # Auth not configured. Audit fix 2026-05-07 (HIGH): refuse to
        # fall through in production. A missing JWT secret in prod
        # almost always means the env var rotation lost the value; we'd
        # rather 500 every request than silently disable auth. Dev mode
        # (ARIA_ENV unset / "dev" / "local") still allows through so
        # local development without Supabase keeps working.
        env = os.environ.get("ARIA_ENV", "").lower()
        if env in ("prod", "production"):
            from starlette.responses import JSONResponse
            logger.error(
                "SUPABASE_JWT_SECRET unset in ARIA_ENV=%s — middleware refusing dev-mode fallthrough",
                env,
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "Auth misconfigured: SUPABASE_JWT_SECRET required in production"},
            )
        # Dev mode — allow through
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

    # Ban gate — hard-lock for users whose profiles.banned_at is set.
    # Distinct from the pause gate below (which only blocks "expensive"
    # actions): banned users are bounced off EVERY authenticated route
    # so the frontend can route them to /banned cleanly. Supabase Auth
    # is still the canonical source of truth (a banned JWT will be
    # rejected at refresh time), but this in-process gate closes the
    # window between when ban_user fires and when the access token
    # expires.
    #
    # The 403 carries detail=BANNED so the frontend axios interceptor
    # can detect it without parsing prose, plus the user_id so the
    # /banned page can fetch the reason from /api/auth/ban-status/{uid}
    # (which is public — banned users have no valid session).
    _ban_user_id = (user.get("sub") or "")
    if _ban_user_id and _ban_user_id != "dev-user":
        try:
            from backend.services.profiles import is_user_banned
            if is_user_banned(_ban_user_id):
                from starlette.responses import JSONResponse
                return JSONResponse(
                    status_code=403,
                    content={"detail": "BANNED", "user_id": _ban_user_id},
                    headers=cors_headers,
                )
        except Exception as _ban_exc:
            # Failing-open here is intentional: if the profiles lookup
            # is broken (transient Supabase outage), we'd rather serve
            # the user than mass-lock every account. Supabase Auth's
            # own ban check still runs at JWT refresh time so an
            # actually-banned user can't refresh indefinitely.
            logger.debug("[auth] ban gate lookup failed (allowing through): %s", _ban_exc)

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

        # Per-user rate limit on expensive ops. The IP-level limiter
        # alone lets a logged-in user fan out to the whole 120/min IP
        # cap on chat or agent runs; this adds a per-user backstop so
        # proxy rotation doesn't help. 30 expensive calls per minute
        # per user is generous for a human and tight against scripts.
        from backend.services import rate_limit as _rate_limit_svc
        action = "ceo_chat" if path == "/api/ceo/chat" else "agent_run"
        allowed, _ = _rate_limit_svc.hit(f"user:{action}", user_id, 30, 60)
        if not allowed:
            from starlette.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={"detail": f"Too many {action} requests. Please wait before retrying."},
                headers=cors_headers,
            )

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

    # NOTE: tenant ownership check used to live here as a heuristic that
    # scanned URL segments for "long strings >8 chars" and short-circuited
    # with 403 on mismatch. It was removed in 2026-05-05 (security audit
    # batch B, item #10) because:
    #
    #   1. The heuristic was leaky — it could miss-identify the wrong
    #      segment as the tenant_id when a route had multiple long path
    #      components, or skip the check entirely on routes whose
    #      "skip" segment list was incomplete.
    #   2. The whole DB lookup was wrapped in `try/except: pass` so any
    #      DB hiccup, config-load error, or import failure silently let
    #      the request through (fail-OPEN).
    #   3. It's now redundant. Every per-tenant route in the codebase
    #      runs Depends(get_verified_tenant) at the router or per-route
    #      level (see commits ba6adce + cf0ebb0 + 664d19e). That dep is
    #      authoritative, runs after path resolution (so it has the
    #      actual tenant_id, not a heuristic guess), and fails CLOSED
    #      with 403/404 instead of silently allowing.
    #
    # Removing this block kills 30 lines of false-confidence defense
    # and forces every per-tenant route to depend on the explicit
    # router-level guard, which is what we want.

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
# Auth model: every connect must present a Supabase JWT (via the
# socket.io-client `auth: { token }` option, or as a Bearer header).
# Without it the connection is rejected — closes the cross-tenant
# real-time data leak that came from `cors_allowed_origins="*"` plus
# the empty connect handler. join_tenant additionally verifies that
# the JWT user owns the tenant_id they're trying to subscribe to,
# matching the REST get_verified_tenant logic.

@sio.event
async def connect(sid, environ, auth=None):
    """Authenticate the WebSocket on connect.

    Token sources, in priority order:
      1. socket.io-client `auth: { token }` payload
      2. HTTP `Authorization: Bearer <token>` header

    Dev mode (SUPABASE_JWT_SECRET unset) allows anonymous connections —
    join_tenant in dev mode lets any room through to keep local UX
    working without auth wiring. In production any unauthenticated
    connection is refused.
    """
    import socketio as _socketio
    from backend.auth import _get_jwt_secret, verify_jwt

    secret = _get_jwt_secret()
    if not secret:
        # Dev mode — record a synthetic dev-user so join_tenant can
        # short-circuit ownership checks the same way the REST layer does.
        await sio.save_session(sid, {"user": {"sub": "dev-user", "email": "dev@localhost"}})
        return True

    token = ""
    if isinstance(auth, dict):
        token = (auth.get("token") or "").strip()
    if not token:
        header = environ.get("HTTP_AUTHORIZATION", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()

    if not token:
        logger.warning(
            "[socket] connect rejected: no auth token from %s",
            environ.get("REMOTE_ADDR") or environ.get("HTTP_X_FORWARDED_FOR") or "unknown",
        )
        raise _socketio.exceptions.ConnectionRefusedError("Missing auth token")

    try:
        user = verify_jwt(token)
    except HTTPException as e:
        logger.warning("[socket] connect rejected: invalid token (%s)", e.detail)
        raise _socketio.exceptions.ConnectionRefusedError(f"Invalid token: {e.detail}")

    await sio.save_session(sid, {"user": user})
    return True


@sio.event
async def join_tenant(sid, data):
    """Add this socket to the given tenant's room AFTER verifying that
    the authenticated user owns it. Returns {ok: bool, error?: str}.

    Without this gate, any connected client could subscribe to any
    tenant_id and silently receive that tenant's inbox / CRM / chat
    events — the original critical leak.
    """
    tenant_id = ((data or {}).get("tenant_id") or "").strip()
    if not tenant_id:
        return {"ok": False, "error": "missing_tenant_id"}

    session = await sio.get_session(sid)
    user = (session or {}).get("user") or {}

    # Dev-mode shortcut — REST does the same in get_verified_tenant
    if user.get("sub") == "dev-user":
        await sio.enter_room(sid, tenant_id)
        return {"ok": True, "dev_mode": True}

    user_email = (user.get("email") or "").lower().strip()
    user_id = user.get("sub") or ""
    if not user_email and not user_id:
        return {"ok": False, "error": "unauthenticated"}

    try:
        from backend.config.loader import get_tenant_config
        config = get_tenant_config(tenant_id)
    except Exception as e:
        logger.warning("[socket] join_tenant lookup failed for %s: %s", tenant_id, e)
        return {"ok": False, "error": "tenant_not_found"}

    owner_email = (config.owner_email or "").lower().strip()
    allowed = False
    if owner_email and user_email and owner_email == user_email:
        allowed = True
    elif str(config.tenant_id) == user_id:
        allowed = True
    elif not owner_email:
        # Legacy / migration: tenants without owner_email fall through
        # the same way REST does. Logged loudly so we can backfill.
        logger.warning("[socket] tenant %s has no owner_email — allowing join", tenant_id)
        allowed = True

    if not allowed:
        logger.warning(
            "[socket] join_tenant denied: jwt_email=%s owner_email=%s tenant=%s",
            user_email, owner_email, tenant_id,
        )
        return {"ok": False, "error": "forbidden"}

    await sio.enter_room(sid, tenant_id)
    return {"ok": True}


@sio.event
async def leave_tenant(sid, data):
    """Drop the socket from a tenant room. No ownership check — leaving
    is always safe and the frontend cleanup path emits this on unmount.
    """
    tenant_id = ((data or {}).get("tenant_id") or "").strip()
    if tenant_id:
        await sio.leave_room(sid, tenant_id)
    return {"ok": True}


# Active onboarding sessions
# NOTE: onboarding_sessions dict moved to backend/routers/onboarding.py along
# with the routes that mutate it.

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
    """Liveness probe. Returns the minimum needed to satisfy load
    balancers / uptime checks: a static status string.

    Audit item #19: response is intentionally tiny. No version, no
    git SHA, no DB ping output, no timestamp -- each of those is a
    small recon signal (server clock skew, deploy frequency, library
    fingerprinting) that has no operational value to the caller.
    Operators get health/version info via journalctl + `git log` on
    the VPS, not over the wire.
    """
    return {"status": "ok"}


# ─── Current user profile snapshot (role + status + plan) ───
@app.get("/api/profile/me")
async def profile_me(request: Request):
    """Return the calling user's role + status + current plan + limits.

    Used by the dashboard layout to decide whether to show the
    "account paused" banner AND to render the plan picker / quota
    badges on the usage page. The auth middleware has already verified
    the JWT and stamped request.state.user; this adds the profiles row
    lookup + a single tenant_configs lookup to surface the plan tier.

    Plan/limits lookup is best-effort: a user who hasn't completed
    onboarding (no tenant_configs row yet) gets ``plan: null``,
    ``limits: null`` instead of a 500 -- the frontend should fall
    through to onboarding in that case.
    """
    user = getattr(request.state, "user", None) or {}
    user_id = (user.get("sub") if isinstance(user, dict) else "") or ""
    user_email = (
        (user.get("email") if isinstance(user, dict) else "") or ""
    ).lower().strip()

    # Dev mode shortcut: no profiles lookup, but still surface a sane
    # default plan so the frontend doesn't crash on `data.plan`.
    if not user_id or user_id == "dev-user":
        return {
            "user_id": user_id,
            "role": "user",
            "status": "active",
            "plan": "scale",
            "limits": _limits_dict_for_plan("scale"),
        }

    from backend.services.profiles import get_user_role, get_user_status
    role = get_user_role(user_id)
    status = get_user_status(user_id)

    plan, limits = _lookup_plan_and_limits_for_email(user_email)
    return {
        "user_id": user_id,
        "role": role,
        "status": status,
        "plan": plan,
        "limits": limits,
    }


def _limits_dict_for_plan(plan: str) -> dict:
    """Serialize a PlanLimits dataclass to the JSON shape the frontend
    consumes on /api/profile/me.

    Kept in server.py (not the plan_quotas module) so the dataclass stays
    pure for the orchestrator's quota gate, and any future
    frontend-shape evolution doesn't ripple back into the gate logic.
    Field names mirror the task brief verbatim:
        content_pieces_per_month
        campaign_plans_per_month
        email_sequences_enabled
    """
    from backend.services.plan_quotas import PLAN_LIMITS
    limits = PLAN_LIMITS.get(plan) or PLAN_LIMITS["free"]
    return {
        "content_pieces_per_month": limits.content_pieces,
        "campaign_plans_per_month": limits.campaign_plans,
        "email_sequences_enabled": limits.email_sequences_enabled,
    }


def _lookup_plan_and_limits_for_email(email: str) -> tuple[str | None, dict | None]:
    """Look up the tenant for the given owner_email and return
    (plan_slug, limits_dict).

    Returns ``(None, None)`` when:
      * email is empty
      * no tenant row matches (user hasn't onboarded yet)
      * any DB error occurred -- we'd rather drop the plan field than
        500 the whole /me endpoint

    Best-effort by design: /api/profile/me is on the critical render
    path of every dashboard page, so a transient Supabase blip
    shouldn't paint a "paused account" banner just because the plan
    column didn't load.
    """
    if not email:
        return None, None
    try:
        sb = _get_supabase()
        result = (
            sb.table("tenant_configs")
            .select("plan")
            .eq("owner_email", email)
            .limit(1)
            .execute()
        )
        data = getattr(result, "data", None) or []
        if not data:
            return None, None
        plan = (data[0].get("plan") or "free").strip().lower()
        from backend.services.plan_quotas import PLAN_LIMITS
        if plan not in PLAN_LIMITS:
            logger.warning("Unknown plan slug %r on /me lookup — coercing to free", plan)
            plan = "free"
        return plan, _limits_dict_for_plan(plan)
    except Exception as e:
        logger.warning("profile/me plan lookup failed for %s: %s", email, e)
        return None, None


# ─── Twitter / X OAuth 2.0 ───
# Connect + callback routes for Twitter, LinkedIn, and Google (Gmail) moved
# to backend/routers/auth_oauth.py. _safe_oauth_error and _get_backend_base_url
# also moved. The _linkedin_pending_auth state store moved with the LinkedIn
# routes.


# LinkedIn organization/set-target/post routes, /api/twitter/{tenant_id}/tweet
# + /thread, and the /api/social/{tenant_id}/approve-publish route moved to
# backend/routers/social.py. _sanitize_social_post_text and _SOCIAL_META_PATTERNS
# stay below because backend/routers/inbox.py still imports them from
# backend.server.


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


# Social-approve-publish and all WhatsApp routes (webhook GET/POST + tenant
# send/connect/disconnect) plus the _resolve_whatsapp_tenant helper moved
# to backend/routers/social.py.


# /api/integrations/{tenant_id}/{gmail,twitter,linkedin}-disconnect moved to
# backend/routers/integrations.py.


# ─── Scheduler API ───
# Routes /api/schedule/* and /api/calendar/{tenant_id}/activity moved to
# backend/routers/scheduling.py. _emit_scheduled_task_created stays here
# because the pending-schedule watcher (further below) also uses it; the
# router imports it back via `from backend.server import
# _emit_scheduled_task_created` to avoid two divergent copies.


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


# _AGENT_DISPLAY_NAMES, _agent_display_name, _emit_task_completed all
# moved to backend/services/realtime.py. Aliased above so call sites
# in this file keep working.


# All /api/schedule/* + /api/calendar/* routes moved to backend/routers/scheduling.py.


# ─── Usage API ───

@app.get("/api/usage/{tenant_id}")
async def get_usage_dashboard(tenant_id: str, request: Request):
    """Return usage stats for the dashboard: tenant totals + per-agent
    breakdown + monthly plan quota.

    Window: rolling 1 hour for the hourly rate-limit display, calendar
    month UTC for the plan-quota counters. Counts come from
    ``agent_logs`` (one row per dispatch, status in
    completed/completed_with_warning) -- the canonical record of what
    each agent actually did, surviving container restarts. The previous
    implementation used an in-memory dict that was wiped on every
    deploy, which is why /usage was showing 0s despite the user having
    run many agents.

    Auth: the global middleware has already verified the JWT, but the
    route doesn't sit under a router with ``Depends(get_verified_tenant)``
    so we verify ownership here. Mirrors the pattern in the inbox
    routes for item-id-keyed endpoints.

    Shape is backward-compatible with the existing /usage page
    (frontend reads ``data.tenant.{requests,request_limit,...}`` and
    ``data.agents[<id>]``), with NEW top-level fields per the task
    brief: ``window``, ``total_requests``, ``total_tokens``,
    ``total_input_tokens``, ``total_output_tokens``, ``request_limit``,
    ``token_limit``, ``per_agent``, ``monthly``.
    """
    # Ownership check -- the route isn't router-scoped, so do it here.
    await get_verified_tenant(request, tenant_id)

    from backend.tools.claude_cli import (
        HOURLY_REQUEST_LIMIT, HOURLY_TOKEN_LIMIT,
        AGENT_HOURLY_LIMITS, DEFAULT_AGENT_LIMIT,
    )

    # ── Rolling 1h window for the hourly limits display ──
    now = datetime.now(timezone.utc)
    hour_ago = (now - timedelta(hours=1)).isoformat()

    sb = _get_supabase()
    try:
        result = (
            sb.table("agent_logs")
            .select("agent_name,status,result,timestamp")
            .eq("tenant_id", tenant_id)
            .gte("timestamp", hour_ago)
            .execute()
        )
        rows_1h = list(result.data or [])
    except Exception as e:
        logger.warning("[usage] agent_logs 1h query failed for tenant=%s: %s", tenant_id, e)
        rows_1h = []

    # All six canonical agents -- pre-populate so each appears even if
    # there's been no activity. The frontend renders one card per key.
    AGENT_IDS = ["ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist", "media"]
    per_agent: dict[str, dict] = {}
    for agent_id in AGENT_IDS:
        limits = AGENT_HOURLY_LIMITS.get(agent_id, DEFAULT_AGENT_LIMIT)
        per_agent[agent_id] = {
            "requests": 0,
            "request_limit": limits.get("requests", DEFAULT_AGENT_LIMIT["requests"]),
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "token_limit": limits.get("tokens", DEFAULT_AGENT_LIMIT["tokens"]),
            # Mirror keys per the task brief for callers that read the
            # new shape directly.
            "tokens": 0,
            "limit_requests": limits.get("requests", DEFAULT_AGENT_LIMIT["requests"]),
            "limit_tokens": limits.get("tokens", DEFAULT_AGENT_LIMIT["tokens"]),
        }

    # Count rows + sum tokens (if the agent stashed them in result jsonb).
    total_requests = 0
    total_input = 0
    total_output = 0
    for row in rows_1h:
        status = (row.get("status") or "").lower()
        if status not in ("completed", "completed_with_warning"):
            continue
        agent = row.get("agent_name") or ""
        bucket = per_agent.get(agent)
        if bucket is None:
            # Unknown agent slug -- still count toward tenant totals but
            # don't create a per-agent card for it.
            total_requests += 1
            continue
        bucket["requests"] += 1
        bucket["limit_requests"] = bucket["request_limit"]  # keep aliases in sync
        total_requests += 1
        # Tokens are best-effort: agent_logs.result is a free-form jsonb
        # that some callers populate with usage metadata and others
        # don't. Pull whatever fields look like token counts and treat
        # missing as 0 (the rate-limit display still has the request
        # count, which is the headline number).
        res = row.get("result") or {}
        if isinstance(res, dict):
            in_t = int(res.get("input_tokens") or res.get("prompt_tokens") or 0)
            out_t = int(res.get("output_tokens") or res.get("completion_tokens") or 0)
            bucket["input_tokens"] += in_t
            bucket["output_tokens"] += out_t
            bucket["total_tokens"] = bucket["input_tokens"] + bucket["output_tokens"]
            bucket["tokens"] = bucket["total_tokens"]
            total_input += in_t
            total_output += out_t

    # ── Monthly plan quota counters ──
    # Same agent_logs table but a wider window (start of current month
    # UTC) and aggregated by bucket per plan_quotas.PLAN_LIMITS.
    from backend.services.plan_quotas import month_start_utc, PLAN_LIMITS
    try:
        month_res = (
            sb.table("agent_logs")
            .select("agent_name,status,timestamp")
            .eq("tenant_id", tenant_id)
            .gte("timestamp", month_start_utc().isoformat())
            .execute()
        )
        rows_month = list(month_res.data or [])
    except Exception as e:
        logger.warning("[usage] agent_logs monthly query failed for tenant=%s: %s", tenant_id, e)
        rows_month = []

    CONTENT_AGENTS = {"content_writer", "social_manager", "media"}
    CAMPAIGN_AGENTS = {"ad_strategist"}
    content_used = 0
    campaigns_used = 0
    for row in rows_month:
        if (row.get("status") or "").lower() not in ("completed", "completed_with_warning"):
            continue
        agent = row.get("agent_name") or ""
        if agent in CONTENT_AGENTS:
            content_used += 1
        elif agent in CAMPAIGN_AGENTS:
            campaigns_used += 1

    # Look up the tenant's plan to populate the monthly limits. Fall
    # back to "free" on lookup failure (same conservative default as
    # plan_quotas._plan_for).
    try:
        tenant_cfg = get_tenant_config(tenant_id)
        plan_slug = (getattr(tenant_cfg, "plan", None) or "free").strip().lower()
        if plan_slug not in PLAN_LIMITS:
            plan_slug = "free"
    except Exception:
        plan_slug = "free"
    plan_limits = PLAN_LIMITS[plan_slug]

    # Build the response. Existing-frontend shape (`tenant`/`agents`)
    # is preserved verbatim so the /usage page keeps rendering; new
    # top-level fields match the task brief.
    tenant_block = {
        "requests": total_requests,
        "request_limit": HOURLY_REQUEST_LIMIT,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "token_limit": HOURLY_TOKEN_LIMIT,
    }
    return {
        # ── new top-level shape (task brief) ──
        "tenant_id": tenant_id,
        "window": "1h",
        "total_requests": total_requests,
        "total_tokens": total_input + total_output,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "request_limit": HOURLY_REQUEST_LIMIT,
        "token_limit": HOURLY_TOKEN_LIMIT,
        "per_agent": per_agent,
        "monthly": {
            "plan": plan_slug,
            "content_used": content_used,
            "content_limit": plan_limits.content_pieces,
            "campaigns_used": campaigns_used,
            "campaigns_limit": plan_limits.campaign_plans,
            "email_sequences_enabled": plan_limits.email_sequences_enabled,
        },
        # ── legacy shape (backward compatibility) ──
        "tenant": tenant_block,
        "agents": per_agent,
        "resets_at": now.strftime("%Y-%m-%d-%H"),
    }


# ─── Onboarding API ───
# All onboarding routes (including /api/tenant/by-email/{email},
# /api/tenant/{tenant_id}/onboarding-data, /api/tenant/{tenant_id}/update-onboarding,
# /api/tenants/{tenant_id}/regenerate-brief) plus the OnboardingMessage/Start/
# SaveConfig/SaveConfigDirect/OnboardingDraftPayload/UpdateOnboarding pydantic
# models and the _get_session_for_user/_persist_onboarding_draft/
# _load_onboarding_draft/_delete_onboarding_draft/_last_assistant_message/
# _apply_onboarding_edit helpers and the in-memory onboarding_sessions dict
# moved to backend/routers/onboarding.py.


# ─── Google OAuth Token Storage ───
# GoogleTokens model + POST /api/integrations/{tenant_id}/google-tokens moved
# to backend/routers/integrations.py.


# ─── Google OAuth Connect (dedicated flow, independent of Supabase) ───
# /api/auth/google/connect/{tenant_id} and /api/auth/google/callback (plus the
# GOOGLE_AUTH_URL / GOOGLE_TOKEN_URL / GOOGLE_GMAIL_SCOPES constants) moved to
# backend/routers/auth_oauth.py.



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
# Moved to backend/routers/notifications.py — see app.include_router(notifications_router) above.


# ─── Webhook Endpoints ───
# Every external webhook MUST verify a provider-side signature before
# trusting payload contents. Without this, anyone can curl POST a forged
# event ("invoice.paid", "orders/create", etc.) and fire downstream side
# effects — a critical risk for the Stripe path which handle_webhook can
# act on as a payment confirmation.
#
# Per-provider behavior:
#   - signed and valid     → process the event
#   - signed and invalid   → 401, log, do nothing
#   - secret env var unset → 503 in production, allow in dev with a loud
#     warning. Refusing prod-without-secret prevents the silent
#     dev-fallback from being deployed by accident.

def _is_production() -> bool:
    return (os.environ.get("ARIA_ENV") or os.environ.get("ENV") or "").lower() in ("prod", "production")


@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    import json as _json
    secret = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()
    raw_body = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not secret:
        if _is_production():
            logger.error("[webhook/stripe] STRIPE_WEBHOOK_SECRET not configured — refusing in prod")
            raise HTTPException(status_code=503, detail="Stripe webhook secret not configured")
        logger.warning("[webhook/stripe] STRIPE_WEBHOOK_SECRET unset (dev mode) — accepting unsigned event")
        try:
            payload = _json.loads(raw_body.decode() or "{}")
        except Exception:
            payload = {}
    else:
        try:
            import stripe as _stripe
            event = _stripe.Webhook.construct_event(raw_body, sig_header, secret)
            payload = event if isinstance(event, dict) else dict(event)
        except Exception as e:
            logger.warning("[webhook/stripe] signature verification failed: %s", type(e).__name__)
            raise HTTPException(status_code=401, detail="Invalid Stripe signature")

    event_type = payload.get("type", "")
    tenant_id = (
        payload.get("data", {}).get("object", {}).get("metadata", {}).get("tenant_id", "")
    )
    if "invoice" in event_type:
        result = await handle_webhook("payment_received", {"tenant_id": tenant_id, **payload})
    else:
        result = {"status": "ignored", "event": event_type}
    return result


@app.post("/api/webhooks/sendgrid")
async def sendgrid_webhook(request: Request):
    """SendGrid Event Webhook with HMAC-SHA256 signature verification.

    Setup: in SendGrid → Settings → Mail Settings → Event Webhook,
    enable "Signed Event Webhook Requests". The secret comes from
    `SENDGRID_WEBHOOK_VERIFICATION_KEY` (the public key string from
    the SendGrid UI). Without it set, prod returns 503; dev allows
    unsigned with a warning.
    """
    import json as _json
    secret = (os.environ.get("SENDGRID_WEBHOOK_VERIFICATION_KEY") or "").strip()
    raw_body = await request.body()
    sig = request.headers.get("X-Twilio-Email-Event-Webhook-Signature", "")
    ts = request.headers.get("X-Twilio-Email-Event-Webhook-Timestamp", "")

    if not secret:
        if _is_production():
            logger.error("[webhook/sendgrid] SENDGRID_WEBHOOK_VERIFICATION_KEY not configured — refusing in prod")
            raise HTTPException(status_code=503, detail="SendGrid webhook secret not configured")
        logger.warning("[webhook/sendgrid] secret unset (dev mode) — accepting unsigned event")
    else:
        try:
            import base64
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec, utils as ec_utils
            pub_der = base64.b64decode(secret)
            pub_key = serialization.load_der_public_key(pub_der)
            signed_payload = ts.encode() + raw_body
            signature_bytes = base64.b64decode(sig)
            r, s = ec_utils.decode_dss_signature(signature_bytes)
            pub_key.verify(
                ec_utils.encode_dss_signature(r, s),
                signed_payload,
                ec.ECDSA(hashes.SHA256()),
            )
        except Exception as e:
            logger.warning("[webhook/sendgrid] signature verification failed: %s", type(e).__name__)
            raise HTTPException(status_code=401, detail="Invalid SendGrid signature")

    try:
        payload = _json.loads(raw_body.decode() or "{}")
    except Exception:
        payload = {}
    # SendGrid sends a list of events, not a single dict — preserve old
    # behavior of treating it as one inbound_email by passing through.
    tenant_id = request.headers.get("X-Tenant-Id", "")
    body_for_handler = payload if isinstance(payload, dict) else {"events": payload}
    result = await handle_webhook("inbound_email", {"tenant_id": tenant_id, **body_for_handler})
    if tenant_id:
        await sio.emit("agent_event", result, room=tenant_id)
    return result


@app.post("/api/webhooks/shopify")
async def shopify_webhook(request: Request):
    """Shopify webhook with HMAC-SHA256 verification.

    Setup: in Shopify Admin → Settings → Notifications → Webhooks,
    use the shared secret as `SHOPIFY_WEBHOOK_SECRET`. Without it set,
    prod returns 503; dev allows unsigned with a warning. The
    X-Tenant-Id header is still trusted but only AFTER the signature
    proves the request actually came from Shopify.
    """
    import json as _json
    secret = (os.environ.get("SHOPIFY_WEBHOOK_SECRET") or "").strip()
    raw_body = await request.body()
    received_hmac = request.headers.get("X-Shopify-Hmac-Sha256", "")

    if not secret:
        if _is_production():
            logger.error("[webhook/shopify] SHOPIFY_WEBHOOK_SECRET not configured — refusing in prod")
            raise HTTPException(status_code=503, detail="Shopify webhook secret not configured")
        logger.warning("[webhook/shopify] secret unset (dev mode) — accepting unsigned event")
    else:
        import base64, hashlib, hmac as _hmac
        digest = _hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode()
        if not _hmac.compare_digest(expected, received_hmac):
            logger.warning("[webhook/shopify] HMAC mismatch")
            raise HTTPException(status_code=401, detail="Invalid Shopify signature")

    try:
        payload = _json.loads(raw_body.decode() or "{}")
    except Exception:
        payload = {}
    tenant_id = request.headers.get("X-Tenant-Id", "")
    topic = request.headers.get("X-Shopify-Topic", "")
    event_map = {"orders/create": "new_order", "checkouts/create": "abandoned_cart"}
    event_type = event_map.get(topic, "unknown")
    result = await handle_webhook(event_type, {"tenant_id": tenant_id, **payload})
    return result


# ─── Agent Management API ───
# /api/agents/{tenant_id} (list), /run, /pause, /resume, /api/media/{tenant_id}
# /generate, /api/office/agents/{tenant_id}, and /api/office/agents/{tenant_id}/
# {agent_id}/activity all moved to backend/routers/agents_runtime.py.
# _emit_agent_status and VIRTUAL_OFFICE_AGENTS stay here because many other
# code paths import them.

# ─── Dashboard API ───
# All /api/dashboard/{tenant_id}/* and /api/analytics/{tenant_id} routes
# moved to backend/routers/dashboard_analytics.py.


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
# /api/ceo/triage moved to backend/routers/dashboard_analytics.py.


# ─── Cron trigger endpoint ───
#
# Auth model: shared-secret HMAC-style token in `X-Aria-Cron-Token`
# (or `Authorization: Bearer <secret>`). Mirrors the pattern used by
# `/api/internal/security-review` (HMAC over body) and the inbox-router
# `_check_agent_token` — a single env var (`ARIA_CRON_SECRET`) is the
# only credential needed.
#
# OPERATOR NOTE: VPS cron job + GitHub Actions schedule both call this —
# set `ARIA_CRON_SECRET` in `/opt/aria/.env` and inject as the
# `X-Aria-Cron-Token` header on both invokers. If the var is unset AND
# `ARIA_ENV=prod`, the endpoint fails closed with 503 so a forgotten
# rotation can't accidentally re-open the public surface. In dev mode
# (no ARIA_ENV set, or set to 'dev'/'local'), an unset secret allows
# unauthenticated calls so local cron testing isn't blocked.
@app.post("/api/cron/run-scheduled")
async def cron_trigger(request: Request):
    secret = os.environ.get("ARIA_CRON_SECRET", "").strip()
    env = os.environ.get("ARIA_ENV", "").lower()

    if not secret:
        if env in ("prod", "production"):
            logger.error(
                "ARIA_CRON_SECRET unset in ARIA_ENV=%s — refusing to run cron",
                env,
            )
            raise HTTPException(
                status_code=503,
                detail="cron not configured: ARIA_CRON_SECRET required in production",
            )
        # Dev: allow through with a warning so the missing config is visible.
        logger.warning("ARIA_CRON_SECRET unset; allowing cron trigger in dev mode")
    else:
        # Accept either the dedicated header or an Authorization bearer.
        provided = request.headers.get("x-aria-cron-token", "").strip()
        if not provided:
            auth = request.headers.get("authorization", "").strip()
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        if not provided or not hmac.compare_digest(provided, secret):
            client_host = request.client.host if request.client else "?"
            logger.warning("cron-trigger token mismatch from %s", client_host)
            raise HTTPException(status_code=401, detail="invalid cron token")

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
# _parse_codeblock_json moved to backend/services/chat.py (slice 4b).
# Aliased back to the original name below so call sites keep working.
from backend.services.chat import parse_codeblock_json as _parse_codeblock_json


# _safe_background moved to backend/services/async_utils.py.
# Aliased back so existing call sites keep working.
from backend.services.async_utils import safe_background as _safe_background


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
    except PlanQuotaExceeded as quota_exc:
        # Pricing-tier wall, not a system error. Write a polite inbox
        # row the user sees in their feed instead of a silent failure,
        # and emit a task_completed event so the chat surface reflects
        # the result. Logged at INFO so it doesn't page out.
        _logger.info(
            "[paperclip-watch] plan quota blocked %s for %s: %s",
            agent_id, tenant_id, quota_exc.reason,
        )
        wall_row = _save_inbox_item(
            tenant_id=tenant_id,
            agent=agent_id,
            title=f"Upgrade required: {agent_id.replace('_', ' ').title()}",
            content=(
                f"{quota_exc.reason}.\n\n"
                f"Plan: {quota_exc.plan} (used {quota_exc.used}"
                + (f"/{quota_exc.limit}" if quota_exc.limit > 0 else "")
                + "). "
                f"Upgrade your plan to keep dispatching this agent."
            ),
            content_type="notification",
            priority=priority,
            task_id=task_id,
            chat_session_id=session_id,
            status="quota_blocked",
        )
        if wall_row and tenant_id:
            try:
                await sio.emit("inbox_new_item", {
                    "id": wall_row["id"],
                    "agent": agent_id,
                    "type": "notification",
                    "title": wall_row.get("title", ""),
                    "status": "quota_blocked",
                    "priority": priority,
                    "created_at": wall_row.get("created_at", ""),
                }, room=tenant_id)
            except Exception:
                pass
        # Drop the office sprite back to idle — task is done from the
        # platform's perspective even though no agent ran.
        try:
            await _emit_agent_status(tenant_id, agent_id, "idle",
                                     action="task_complete")
            await _emit_agent_status(tenant_id, "ceo", "idle",
                                     action="return_to_desk")
        except Exception:
            pass
        return
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

    # NOTE: Ad Strategist campaign briefs no longer render [GRAPH_DATA]
    # blocks here. Charts now live in the AI Report flow (see
    # campaign_analyzer.py + routers/campaigns.py:_auto_generate_ai_report)
    # where they render against actual uploaded performance metrics, not
    # imagined budget splits. Any straggler [GRAPH_DATA] blocks in legacy
    # outputs will pass through as raw text — harmless, just unrendered.

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

# CreateInboxItem moved to backend/routers/inbox.py (slice 5).


# POST /api/inbox/{tenant_id}/items + the helpers it depended on
# (_merge_into_recent_social_row, _is_duplicate_media_write,
# _cleanup_media_placeholder) all moved to backend/routers/inbox.py
# (slice 5). The route is registered via app.include_router(inbox_router)
# at the top of this file, same as the other domain routers.



# ─── CEO Actions ───

# Action descriptions are static (built from ACTION_REGISTRY at import time)
# so cache at module load instead of rebuilding the list of strings on every
# CEO chat call. Saves ~1ms + a bunch of garbage allocations per request.
try:
    from backend.services.ceo_actions import get_action_descriptions as _ceo_action_descriptions_fn
    _CEO_ACTION_DESCRIPTIONS = _ceo_action_descriptions_fn()
except Exception:
    _CEO_ACTION_DESCRIPTIONS = ""


def _get_ceo_action_descriptions() -> str:
    """Get compact action descriptions for CEO system prompt (cached)."""
    return _CEO_ACTION_DESCRIPTIONS


# _format_action_result moved to backend/routers/ceo.py (slice 4c1).
# Imported back so the inline _ceo_chat_impl call site keeps working
# until slice 4c2 moves the handler itself.
from backend.routers.ceo import _format_action_result  # noqa: E402


# CEOActionRequest + POST /api/ceo/{tenant_id}/action moved to
# backend/routers/dashboard_analytics.py.


# ─── CEO Chat ───
# CEO prompt-time constants moved to backend/services/ceo_prompt.py so
# routers/ceo.py can import them directly without the lazy-import dance.
# Aliased back to underscore-prefixed names so existing in-file references
# (build_sub_agent_context, the chat handler's CRM heuristic, etc.) keep
# working unchanged.
from backend.services.ceo_prompt import (
    CEO_MD as _CEO_MD,
    CEO_MD_FULL as _CEO_MD_FULL,
    AGENT_MDS as _AGENT_MDS,
    DELEGATE_BLOCK_RE as _DELEGATE_BLOCK_RE,
    ACTION_BLOCK_RE as _ACTION_BLOCK_RE,
    CRM_TRIGGER_PHRASES as _CRM_TRIGGER_PHRASES,
    CRM_NOUN_RE as _CRM_NOUN_RE,
    CRM_VERB_RE as _CRM_VERB_RE,
)

# Chat session state — cache + per-session locks + eviction live in
# `backend/services/chat_state.py` since 2026-04-30 (slice 4a). The
# names are aliased back to their original underscore-prefixed forms
# below so the dozens of call sites in this file keep working
# without further edits.
from backend.services.chat_state import (
    chat_sessions as _chat_sessions,
    session_locks as _chat_session_locks,
    get_session_lock as _get_chat_session_lock,
    evict_old_sessions as _evict_chat_sessions,
    MAX_CACHED_SESSIONS as _MAX_CACHED_SESSIONS,
)


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
# _MAX_CACHED_SESSIONS, _chat_session_locks, _get_chat_session_lock,
# _evict_chat_sessions all moved to backend/services/chat_state.py
# (slice 4a). Imported + aliased at the top of this file alongside
# _chat_sessions.


# _save_chat_message moved to backend/services/chat.py (slice 4b).
# Aliased back to the original name below so call sites keep working.
from backend.services.chat import save_message as _save_chat_message


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


# CEOChatMessage + _summarize_ceo_assistant_message + _format_history_message
# + _last_assistant_index + POST /api/ceo/chat + _ceo_chat_impl all moved
# to backend/routers/ceo.py (slice 4c2). The route is registered via
# app.include_router(ceo_router) at the top of this file. The lazy-import
# block at the start of _ceo_chat_impl pulls back this file's helpers
# (sio, _CEO_MD, _CRM_*_RE, _safe_background, _execute_delegation, etc.)
# without triggering a circular load at startup.




# GET /api/ceo/chat/{session_id}/history
# GET /api/ceo/chat/sessions/{tenant_id}
# POST /api/ceo/chat/sessions/{tenant_id}/bulk-delete
# DELETE /api/ceo/chat/sessions/{tenant_id}/{session_id}
# All moved to backend/routers/ceo.py — see app.include_router(ceo_router) above.
# (POST /api/ceo/chat — the chat handler itself — still lives in this file
#  pending slice 4c which will move it alongside the read endpoints.)


# ─── Project Tasks API ───
# Moved to backend/routers/tasks.py — see app.include_router(tasks_router) above.


# ─── Stagnation Monitor / "Buried Task" API ───
# /api/projects/stale/{tenant_id} + /api/projects/{tenant_id}/snooze/{item_id}
# moved to backend/routers/dashboard_analytics.py.


# PATCH /api/tasks/{task_id} + DELETE /api/tasks/{task_id} +
# the TaskUpdate model moved to backend/routers/tasks.py.


# ─── WebSocket for real-time chat ───
@app.websocket("/ws/chat/{tenant_id}")
async def websocket_chat(websocket: WebSocket, tenant_id: str):
    # Manual auth + tenant ownership check BEFORE accept(). WebSockets
    # don't run through the HTTP auth middleware (path is /ws/...), so
    # without this any client could connect to any tenant's room and
    # send/receive broadcasts. The JWT comes from the ?access_token=
    # query param because browser WebSocket() can't set Authorization
    # headers. Reject (close 4401) on missing/invalid token, 4403 on
    # ownership mismatch — close codes >=4000 propagate the failure
    # reason to the client without ambiguity.
    from backend.auth import verify_jwt
    from backend.config.loader import get_tenant_config

    access_token = websocket.query_params.get("access_token", "")
    if not access_token:
        await websocket.close(code=4401, reason="Missing access_token")
        return
    try:
        user = verify_jwt(access_token)
    except HTTPException:
        await websocket.close(code=4401, reason="Invalid or expired token")
        return

    user_email = (user.get("email") or user.get("user_metadata", {}).get("email") or "").lower().strip()
    user_id = user.get("sub", "")
    # Dev-mode bypass mirrors get_verified_tenant
    if user_id != "dev-user":
        try:
            config = get_tenant_config(tenant_id)
            owner_email = (config.owner_email or "").lower().strip()
            owns = (owner_email and owner_email == user_email) or str(config.tenant_id) == user_id or not owner_email
            if not owns:
                await websocket.close(code=4403, reason="Access denied")
                return
        except Exception:
            await websocket.close(code=4403, reason="Access denied")
            return

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
