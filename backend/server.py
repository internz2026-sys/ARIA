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

from backend.auth import get_current_user, get_verified_tenant, check_rate_limit, get_user_id_from_jwt

load_dotenv()

# Install the global log-redaction filter as early as possible so any
# secrets that surface in startup logs (e.g. a connection string in an
# exception during a DB warmup) are scrubbed before stdout flush.
from backend.services.log_redaction import install_global_filter as _install_log_redaction
_install_log_redaction()

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
from backend.onboarding_agent import OnboardingAgent, FIELD_QUESTIONS
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
from backend.paperclip_office_sync import (
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
# Plans: self-service + admin override. Split into two routers so the
# self-service surface doesn't accidentally inherit the /api/admin/* role
# gate, while the admin override does.
app.include_router(plans_profile_router)
app.include_router(plans_admin_router)

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
    "/api/tenant/by-email/", # Tenant lookup during login (returns only tenant_id)
    "/api/email/inbound",   # Inbound mail webhook (Postmark/Resend/SendGrid → /api/email/inbound)
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
# Maps session_id -> (user_id, OnboardingAgent). user_id is the Supabase auth
# `sub` claim from the JWT bound at /start time. Every subsequent endpoint
# (message, skip, extract-config, save-config) verifies the caller's JWT
# user_id matches the bound user_id before touching the agent — anti-replay
# defense per security audit (2026-05-07).
onboarding_sessions: dict[str, tuple[str, OnboardingAgent]] = {}

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
        from backend.services.safe_errors import safe_detail
        raise HTTPException(status_code=400, detail=safe_detail(error_msg, "Publish failed"))

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
        from backend.services.safe_errors import safe_detail
        raise HTTPException(status_code=400, detail=safe_detail(profile["error"], "Connection test failed"))

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


# _AGENT_DISPLAY_NAMES, _agent_display_name, _emit_task_completed all
# moved to backend/services/realtime.py. Aliased above so call sites
# in this file keep working.


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


# ── Onboarding session helpers (auth-bound + resumable) ──────────────────
# These endpoints are now JWT-bound (removed from _PUBLIC_PATHS on
# 2026-05-07). Two new behaviors:
#   1. Each session is bound to the JWT user_id at /start time. Subsequent
#      calls verify the caller's JWT matches the session's bound user, so
#      a leaked session_id can't be replayed by a different user.
#   2. /start can resume from a persisted onboarding_drafts row keyed by
#      user_id. If a row with conversation_history exists, the agent is
#      rehydrated via OnboardingAgent.from_dict and the LAST AI message
#      is replayed so the user picks up at the same question.
#   3. /message persists the agent's full state to onboarding_drafts on
#      every turn so progress survives restarts and tab closures.
#   4. /save-config deletes the draft row once the tenant config is saved.


def _get_session_for_user(session_id: str, user_id: str) -> OnboardingAgent:
    """Look up the agent by session_id and verify the JWT user owns it.

    Raises 404 if the session_id is unknown (in-memory store wiped on
    restart — frontend should call /start to rehydrate from the DB row),
    403 if the JWT user_id doesn't match the bound user.
    """
    entry = onboarding_sessions.get(session_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Session not found")
    bound_user_id, agent = entry
    # Allow dev-mode fallthrough — get_current_user returns "dev-user"
    # when SUPABASE_JWT_SECRET isn't set; the bound user_id will also be
    # "dev-user" in that case, so the equality check still holds.
    if bound_user_id != user_id:
        logger.warning(
            "Onboarding session replay rejected: session=%s bound_to=%s but caller=%s",
            session_id, bound_user_id, user_id,
        )
        raise HTTPException(status_code=403, detail="Session does not belong to this user")
    return agent


def _persist_onboarding_draft(user_id: str, session_id: str, agent: OnboardingAgent) -> None:
    """Best-effort persistence of the agent's state to onboarding_drafts.

    Called on every /message turn. Failures are logged but don't break the
    chat flow — losing a snapshot is preferable to 500ing the user mid-
    conversation. The DB row is keyed by user_id (UNIQUE), so this is an
    UPSERT that always points at the same row per user.
    """
    try:
        sb = _get_supabase()
        row = {
            "user_id": user_id,
            "session_id": session_id,
            "extracted_config": agent._extracted_config or {},
            "skipped_topics": agent.skipped_topics,
            "conversation_history": agent.to_dict(),
        }
        sb.table("onboarding_drafts").upsert(row, on_conflict="user_id").execute()
    except Exception as e:
        logger.warning("Failed to persist onboarding draft for user=%s: %s", user_id, e)


def _load_onboarding_draft(user_id: str) -> dict | None:
    """Fetch the user's persisted onboarding draft row, or None if absent.

    Returns the raw row dict (with conversation_history, session_id,
    extracted_config, skipped_topics). Any error is logged and treated
    as "no draft" so a transient DB blip falls through to fresh-start
    instead of breaking onboarding entirely.
    """
    try:
        sb = _get_supabase()
        result = (
            sb.table("onboarding_drafts")
            .select("user_id,session_id,extracted_config,skipped_topics,conversation_history")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if result.data:
            row = result.data[0]
            # Defense-in-depth (2026-05-07, HIGH audit fix): the .eq()
            # filter above already restricts the query, but we still
            # explicitly assert the returned row's user_id matches the
            # caller's user_id before handing it back. Catches any
            # future regression where the filter is removed/refactored
            # without updating callers, plus defends against a hostile
            # row injected via a different code path.
            row_user_id = row.get("user_id")
            if row_user_id and str(row_user_id) != str(user_id):
                logger.error(
                    "Onboarding draft load rejected: row user_id=%s != caller=%s",
                    row_user_id, user_id,
                )
                raise HTTPException(
                    status_code=403,
                    detail="Draft does not belong to this user",
                )
            return row
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Failed to load onboarding draft for user=%s: %s", user_id, e)
    return None


def _delete_onboarding_draft(user_id: str) -> None:
    """Best-effort delete of the user's draft row after save-config."""
    try:
        sb = _get_supabase()
        sb.table("onboarding_drafts").delete().eq("user_id", user_id).execute()
    except Exception as e:
        logger.warning("Failed to delete onboarding draft for user=%s: %s", user_id, e)


def _last_assistant_message(agent: OnboardingAgent) -> str:
    """Return the last 'assistant' role message from the agent's history,
    or fall back to the next-question prompt if the history is empty."""
    for msg in reversed(agent.messages):
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    # Fallback: ask the current pending question.
    cur = agent.current_field
    if cur:
        return FIELD_QUESTIONS[cur]
    return agent._build_final_summary()


@app.post("/api/onboarding/start")
async def start_onboarding(body: OnboardingStart, user_id: str = Depends(get_user_id_from_jwt)):
    # Try to resume from a persisted draft first.
    # Note: only the new from_dict snapshot shape (a dict with messages/
    # field_state keys, written by this server's _persist_onboarding_draft)
    # supports resume. Legacy drafts written by /api/onboarding/save-draft
    # store conversation_history as a list of chat messages — those don't
    # carry enough state to rehydrate, so we let those users start fresh.
    draft = _load_onboarding_draft(user_id)
    history = (draft or {}).get("conversation_history")
    if isinstance(history, dict) and history.get("messages"):
        try:
            agent = OnboardingAgent.from_dict(history)
            # Reuse stored session_id if present, else mint a new one.
            session_id = draft.get("session_id") or str(uuid.uuid4())
            onboarding_sessions[session_id] = (user_id, agent)
            resumed_message = _last_assistant_message(agent)
            return {
                "session_id": session_id,
                "message": resumed_message,
                "is_resumed": True,
                "is_complete": agent.is_complete(),
                "questions_answered": agent.questions_answered,
                "validated_fields": sorted(agent.validated_fields),
                "skipped_topics": agent.skipped_topics,
            }
        except Exception as e:
            logger.warning(
                "Failed to rehydrate onboarding session for user=%s: %s — starting fresh",
                user_id, e,
            )

    # Fresh start.
    session_id = body.session_id or str(uuid.uuid4())
    agent = OnboardingAgent()
    greeting = agent.start_conversation()
    onboarding_sessions[session_id] = (user_id, agent)
    # Persist the empty draft so /draft GET / future /start calls see it.
    _persist_onboarding_draft(user_id, session_id, agent)
    return {
        "session_id": session_id,
        "message": greeting,
        "is_resumed": False,
        "is_complete": False,
        "questions_answered": 0,
        "validated_fields": [],
        "skipped_topics": [],
    }


@app.post("/api/onboarding/message")
async def onboarding_message(body: OnboardingMessage, user_id: str = Depends(get_user_id_from_jwt)):
    agent = _get_session_for_user(body.session_id, user_id)
    response = await agent.process_message(body.message)
    # Persist after every turn so progress survives restarts/tab closes.
    _persist_onboarding_draft(user_id, body.session_id, agent)
    return {
        "message": response,
        "is_complete": agent.is_complete(),
        "questions_answered": agent.questions_answered,
        "validated_fields": sorted(agent.validated_fields),
    }


@app.post("/api/onboarding/skip")
async def onboarding_skip(body: OnboardingStart, user_id: str = Depends(get_user_id_from_jwt)):
    if not body.session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    agent = _get_session_for_user(body.session_id, user_id)
    skipped = agent.skip_current_topic()
    current = agent.get_current_topic()
    # Persist skip immediately.
    _persist_onboarding_draft(user_id, body.session_id, agent)
    return {
        "skipped_topic": skipped,
        "current_topic": current,
        "questions_answered": agent.questions_answered,
        "is_complete": agent.is_complete(),
        "skipped_topics": agent.skipped_topics,
    }


@app.post("/api/onboarding/extract-config")
async def extract_config(body: OnboardingStart, user_id: str = Depends(get_user_id_from_jwt)):
    if not body.session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    agent = _get_session_for_user(body.session_id, user_id)
    try:
        config_data = await agent.extract_config()
    except Exception as e:
        logger.error("extract_config failed: %s", e)
        # Return the fallback config so the frontend still works
        config_data = agent._fallback_config_from_messages()
    # Persist the fresh extracted_config snapshot too.
    _persist_onboarding_draft(user_id, body.session_id, agent)
    return {"config": config_data}


class SaveConfig(BaseModel):
    session_id: str
    owner_email: str
    owner_name: str
    active_agents: list[str] | None = None
    existing_tenant_id: str | None = None  # If set, overwrite this tenant


@app.post("/api/onboarding/save-config")
async def save_config(body: SaveConfig, user_id: str = Depends(get_user_id_from_jwt)):
    from backend.config.brief import generate_agent_brief

    agent = _get_session_for_user(body.session_id, user_id)
    tenant_id = body.existing_tenant_id or str(uuid.uuid4())
    config = await agent.build_tenant_config(tenant_id, body.owner_email, body.owner_name, body.active_agents)

    # Generate condensed brief — all agents use this instead of full context
    try:
        config.agent_brief = await generate_agent_brief(config)
    except Exception as e:
        logger.warning("Brief generation failed (will use full context): %s", e)

    save_tenant_config(config)
    # Drop the in-memory session and the persisted draft row so future
    # /start calls for this user begin from scratch.
    onboarding_sessions.pop(body.session_id, None)
    _delete_onboarding_draft(user_id)
    return {"tenant_id": tenant_id, "config": config.model_dump(mode="json")}


class SaveConfigDirect(BaseModel):
    """Accept the raw extracted config JSON (cached on the frontend) to save
    directly — no backend session needed.

    NOTE: any client-supplied owner_email is IGNORED — owner_email is derived
    from the JWT email claim. Field kept for backwards-compat with existing
    frontend code that still sends it (and so 4xx-on-extra isn't tripped).
    """
    config: dict
    owner_email: str | None = None  # ignored — derived from JWT
    owner_name: str
    active_agents: list[str] | None = None
    skipped_topics: list[str] | None = None
    existing_tenant_id: str | None = None  # If set, overwrite this tenant


@app.post("/api/onboarding/save-config-direct")
async def save_config_direct(
    body: SaveConfigDirect,
    request: Request,
):
    """Save a tenant config directly from the cached frontend extraction.

    JWT-bound (2026-05-07): owner_email is derived from the JWT email claim,
    NOT trusted from the request body. Previously this endpoint was public
    and accepted whatever owner_email the client sent, which let any caller
    overwrite any user's tenant config (or impersonate a brand-new tenant
    under someone else's email). Now:

      - owner_email comes from the JWT (`email` claim)
      - if existing_tenant_id is set, the JWT user must own that tenant
        (ownership check via `get_verified_tenant`)
    """
    from backend.config.tenant_schema import (
        TenantConfig, ICPConfig, ProductConfig, GTMPlaybook, BrandVoice, GTMProfile,
    )
    from backend.config.brief import generate_agent_brief

    # Derive owner identity from the JWT — ignore body fields entirely.
    user = await get_current_user(request)
    jwt_email = (user.get("email") or user.get("user_metadata", {}).get("email") or "").lower().strip()
    if not jwt_email:
        # Dev-mode fallthrough has email="dev@localhost"; real prod tokens
        # always carry an email claim. If we got here without one, fail.
        raise HTTPException(status_code=401, detail="Invalid token: no email claim")

    # If the caller is overwriting an existing tenant, verify ownership
    # before letting them clobber it. Reuses the same predicate everything
    # else in the system uses (`tenant_configs.owner_email` match).
    if body.existing_tenant_id:
        await get_verified_tenant(request, body.existing_tenant_id)

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
        owner_email=jwt_email,
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
    # NOTE: any client-supplied user_id is IGNORED — we always derive
    # user_id from the JWT. Field kept here only for backwards-compat
    # with existing frontend code that still sends it.
    user_id: str | None = None
    session_id: str | None = None
    extracted_config: dict
    skipped_topics: list | None = None
    conversation_history: list | None = None


@app.post("/api/onboarding/save-draft")
async def save_onboarding_draft(
    body: OnboardingDraftPayload,
    user_id: str = Depends(get_user_id_from_jwt),
):
    """Upsert the authenticated user's in-progress onboarding draft.

    JWT-bound (2026-05-07, CRITICAL audit fix): user_id always comes from
    the JWT. Any user_id field in the request body is ignored. Previously
    this endpoint was public and trusted whatever user_id the client sent,
    which defeated the /start auth-binding (an attacker could overwrite
    any user's draft by knowing their UUID).
    """
    try:
        sb = _get_supabase()
        row = {
            "user_id": user_id,
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
        logger.warning("Failed to save onboarding draft for user=%s: %s", user_id, e)
        return {"saved": False, "error": str(e)[:200]}


@app.get("/api/onboarding/draft")
async def get_onboarding_draft(user_id: str = Depends(get_user_id_from_jwt)):
    """Return the authenticated user's most recent in-progress onboarding
    draft, or 404 if none exists. Used by /select-agents on mount before
    falling back to localStorage.

    JWT-bound (2026-05-07, CRITICAL audit fix): user_id always comes from
    the JWT. Any ?user_id= query param is ignored. Previously this was
    public and let any caller read any user's draft by guessing UUIDs.
    """
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
        logger.warning("Failed to load onboarding draft for user=%s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Could not load draft")


@app.delete("/api/onboarding/draft")
async def delete_onboarding_draft(user_id: str = Depends(get_user_id_from_jwt)):
    """Clean up the authenticated user's draft after successful save-config.
    Best-effort: if the delete fails the row will just expire naturally
    over time.

    JWT-bound (2026-05-07, CRITICAL audit fix): user_id always comes from
    the JWT. Any ?user_id= query param is ignored. Previously this was
    public and let any caller delete any user's draft.
    """
    try:
        sb = _get_supabase()
        sb.table("onboarding_drafts").delete().eq("user_id", user_id).execute()
        return {"deleted": True}
    except Exception as e:
        logger.warning("Failed to delete onboarding draft for user=%s: %s", user_id, e)
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
        from backend.services.safe_errors import safe_detail
        raise HTTPException(status_code=400, detail=safe_detail(e, "Google token save failed"))


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

    try:
        result = await dispatch_agent(tenant_id, agent_name)
    except PlanQuotaExceeded as exc:
        # Expected user-facing wall, not a system error. Return a 429
        # JSON the frontend can render as an "Upgrade to continue"
        # modal. Drop the agent's "working" status back to idle so the
        # office sprite doesn't sit stuck at the desk.
        from starlette.responses import JSONResponse
        await _emit_agent_status(tenant_id, agent_name, "idle",
                                 action="task_complete")
        return JSONResponse(
            status_code=429,
            content={
                "status": "quota_exceeded",
                "reason": exc.reason,
                "plan": exc.plan,
                "used": exc.used,
                "limit": exc.limit,
            },
        )
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
async def generate_media_image(tenant_id: str, request: Request, payload: dict = Body(default={})):
    """Direct image-generation endpoint for the Paperclip Media Designer agent.

    Bypasses Paperclip dispatch and calls media_agent.run() locally so the agent
    actually produces a real PNG via Pollinations -> Supabase Storage -> inbox.
    Public (no JWT) so the Paperclip-spawned Claude CLI can curl it from inside
    the container — same pattern as /api/inbox/.

    Auth gate: the Paperclip Media Designer is the only legitimate caller,
    but the endpoint reaches a paid AI image API. Without auth, anyone could
    drain the API budget with a curl loop. We gate via a shared internal
    token (ARIA_INTERNAL_AGENT_TOKEN) sent in the `X-Aria-Agent-Token`
    header. The Paperclip skill MD on the agent side must include this
    header on the curl call. Production refuses requests when the token
    isn't configured (fail-closed); dev still allows unauth'd with a
    warning to keep local smoke tests working.
    """
    expected_token = (os.environ.get("ARIA_INTERNAL_AGENT_TOKEN") or "").strip()
    received_token = (request.headers.get("X-Aria-Agent-Token") or "").strip()
    if expected_token:
        if not received_token or received_token != expected_token:
            logger.warning(
                "[media] /api/media/%s/generate rejected: bad/missing X-Aria-Agent-Token",
                tenant_id,
            )
            raise HTTPException(status_code=401, detail="Invalid agent token")
    elif _is_production():
        logger.error(
            "[media] ARIA_INTERNAL_AGENT_TOKEN not configured in production — refusing"
        )
        raise HTTPException(
            status_code=503,
            detail="Internal agent token not configured",
        )
    else:
        logger.warning(
            "[media] ARIA_INTERNAL_AGENT_TOKEN unset (dev mode) — accepting unauth'd request"
        )

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

    # Also check tasks table for agents with in_progress tasks.
    # Soft-deleted tasks should not count as active — exclude them.
    task_statuses: dict[str, str] = {}
    try:
        sb = _get_supabase()
        result = sb.table("tasks").select("agent,task").eq(
            "tenant_id", tenant_id
        ).eq("status", "in_progress").is_("deleted_at", "null").execute()
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
        # Skip soft-deleted tasks so the analytics totals reflect the
        # user's actual active workload, not their trash.
        tasks_res = (
            sb.table("tasks")
            .select("status")
            .eq("tenant_id", tenant_id)
            .gte("created_at", cutoff_iso)
            .is_("deleted_at", "null")
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
    from backend.ceo_actions import get_action_descriptions as _ceo_action_descriptions_fn
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


# PATCH /api/tasks/{task_id} + DELETE /api/tasks/{task_id} +
# the TaskUpdate model moved to backend/routers/tasks.py.


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
