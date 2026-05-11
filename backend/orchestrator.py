"""ARIA Orchestrator — dispatches and coordinates all AI agents via Paperclip AI.

Paperclip AI (localhost:3100) handles multi-agent orchestration:
- Agent registration, heartbeats, and lifecycle management
- Org chart hierarchy and task delegation
- Budget tracking and cost control
- Atomic task checkout to prevent double-work

When Paperclip is unavailable, falls back to direct local dispatch.

Paperclip lookup helpers (PAPERCLIP_URL, _urllib_request, get_company_id,
get_paperclip_agent_id, is_connected) used to live in backend/paperclip_sync.py
alongside ~320 lines of startup automation that re-registered agents on every
restart. That automation kept fighting with the user's manual Paperclip
configuration (re-flipping adapters, re-attaching skills, getting agents stuck
in error states), so we deleted the file and inlined the lookup helpers here.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone

import httpx

from backend.agents import AGENT_REGISTRY, DEPARTMENT_MAP
from backend.config.loader import get_tenant_config, get_active_tenants
from backend.services.supabase import get_db
from backend.tasks.task_definitions import CRON_SCHEDULES, WORKFLOW_TEMPLATES

logger = logging.getLogger("aria.orchestrator")


# Cache the SSL context once at module load. ssl.create_default_context()
# walks the system cert bundle on each call (~1-3ms on Windows) and we hit
# this on every Paperclip urllib request — easy win in the watcher hot path.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# ── Paperclip lookup helpers (formerly in backend/paperclip_sync.py) ─────

PAPERCLIP_URL = os.environ.get("PAPERCLIP_API_URL", "http://127.0.0.1:3100")

# Module-level httpx.AsyncClient singleton — opening a fresh client per request
# pays the TCP+TLS handshake cost on every Paperclip GET, which adds up fast in
# get_agent_status (now parallelized) and the office sync loop. Reuses
# connections via the underlying connection pool.
_httpx_client: httpx.AsyncClient | None = None


def _get_httpx_client() -> httpx.AsyncClient:
    global _httpx_client
    if _httpx_client is None:
        _httpx_client = httpx.AsyncClient(timeout=15.0)
    return _httpx_client


async def close_httpx_client() -> None:
    """Close the shared httpx client. Call from app shutdown."""
    global _httpx_client
    if _httpx_client is not None:
        try:
            await _httpx_client.aclose()
        finally:
            _httpx_client = None

# Hardcoded fallback IDs from the production Paperclip company. These are
# used when no live sync has populated the cache. Override via env vars per
# agent if you spin up a new Paperclip instance.
# Paperclip company + agent IDs were previously hardcoded here as
# fallbacks. They've been moved to .env (PAPERCLIP_COMPANY_ID,
# PAPERCLIP_<AGENT>_AGENT_ID per agent) so production-specific
# identifiers don't sit in source. Empty-string defaults below let
# `paperclip_connected()` correctly return False on a missing config
# rather than silently using stale baked-in values.
_KNOWN_COMPANY_ID = ""
_KNOWN_AGENT_IDS: dict[str, str] = {}


class PaperclipUnreachable(Exception):
    """Raised when Paperclip is unreachable at the network level (connection
    refused, DNS, timeout). Distinct from "Paperclip returned an empty list"
    so callers can fail-fast on outages instead of polling for 10 minutes."""


class PlanQuotaExceeded(Exception):
    """Raised by dispatch_agent when the tenant's plan blocks a dispatch.

    Carries the QuotaResult so callers (chat handler, cron runner, REST
    /run endpoint) can surface plan + used + limit + reason without
    re-querying. Catch-and-surface, never log as an error — exceeding
    quota is expected user behaviour, not a system fault.

    Stringifies to the reason message so a bare ``str(exc)`` is safe to
    show the user.
    """

    def __init__(self, reason: str, *, plan: str = "free", used: int = 0, limit: int = 0):
        super().__init__(reason)
        self.reason = reason
        self.plan = plan
        self.used = used
        self.limit = limit

    def __str__(self) -> str:
        return self.reason

    def as_dict(self) -> dict:
        return {
            "status": "quota_exceeded",
            "reason": self.reason,
            "plan": self.plan,
            "used": self.used,
            "limit": self.limit,
        }


def _urllib_request(method: str, path: str, data: dict | None = None, *, strict: bool = False) -> dict | list | None:
    """Make a request to Paperclip using urllib (bypasses httpx cookie issues).

    Used by the Paperclip poller, watcher, and skill manager. Authenticates
    via the PAPERCLIP_SESSION_COOKIE or PAPERCLIP_API_TOKEN env var.

    Returns None on any failure by default (backward-compatible). When
    `strict=True`, connection-level failures (network unreachable, DNS,
    timeout) raise PaperclipUnreachable so callers can distinguish an
    outage from "no data yet" -- the watcher uses this to fail-fast.
    JSON decode errors and HTTP 4xx/5xx still return None even in strict
    mode because those mean Paperclip *is* reachable, just answering
    badly for this particular request.
    """
    session_cookie = os.environ.get("PAPERCLIP_SESSION_COOKIE", "")
    token = os.environ.get("PAPERCLIP_API_TOKEN", "")
    url = f"{PAPERCLIP_URL}{path}"
    body = _json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    if session_cookie:
        req.add_header("Cookie", f"__Secure-better-auth.session_token={session_cookie}")
        req.add_header("Origin", PAPERCLIP_URL)
        req.add_header("Referer", PAPERCLIP_URL + "/")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        r = urllib.request.urlopen(req, timeout=15, context=_SSL_CTX)
        try:
            return _json.loads(r.read().decode())
        except (_json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("urllib %s %s returned non-JSON: %s", method, path, e)
            return None
    except urllib.error.HTTPError as e:
        # 4xx / 5xx -- Paperclip IS reachable, just unhappy with this
        # specific request. Don't treat as outage.
        logger.warning("urllib %s %s HTTP %s: %s", method, path, e.code, e.reason)
        return None
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError, ssl.SSLError) as e:
        # Network-level failure -- Paperclip is unreachable or DNS broken
        # or the connection died. THIS is the case the watcher needs to
        # bail on instead of waiting 10 minutes for nothing.
        logger.warning("urllib %s %s unreachable: %s: %s", method, path, type(e).__name__, e)
        if strict:
            raise PaperclipUnreachable(f"{type(e).__name__}: {e}") from e
        return None
    except Exception as e:
        # Catch-all for anything we didn't anticipate. Log loudly so we
        # notice new failure modes; never raise for non-strict callers.
        logger.warning("urllib %s %s failed: %s: %s", method, path, type(e).__name__, e)
        if strict:
            raise PaperclipUnreachable(f"{type(e).__name__}: {e}") from e
        return None


def get_company_id() -> str | None:
    """Return the configured Paperclip company ID, or the production fallback."""
    return os.environ.get("PAPERCLIP_COMPANY_ID", _KNOWN_COMPANY_ID)


def get_paperclip_agent_id(agent_name: str) -> str | None:
    """Return the Paperclip agent UUID for an ARIA agent slug."""
    env_key = f"PAPERCLIP_{agent_name.upper()}_AGENT_ID"
    return os.environ.get(env_key) or _KNOWN_AGENT_IDS.get(agent_name)


def paperclip_connected() -> bool:
    """Return True if at least one Paperclip agent API key is configured.

    This is the only signal we have for 'is Paperclip set up?' without
    making a live HTTP call on every check. The orchestrator uses this
    to decide whether to route through Paperclip or fall back to local
    dispatch.
    """
    return any(
        os.environ.get(f"PAPERCLIP_{k}_KEY")
        for k in ("CEO", "CONTENT_WRITER", "EMAIL_MARKETER", "SOCIAL_MANAGER", "AD_STRATEGIST", "MEDIA")
    )


# Backwards-compat alias for code that imports `is_connected` directly
is_connected = paperclip_connected


async def log_agent_action(tenant_id: str, agent_name: str, action: str, result: dict, status: str = "completed"):
    """Log every agent action to Supabase for dashboard activity feed."""
    try:
        get_db().table("agent_logs").insert({
            "tenant_id": tenant_id,
            "agent_name": agent_name,
            "action": action,
            "result": result,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.error("Failed to log agent action: %s", e)


# Per-agent API keys for Paperclip
AGENT_API_KEYS = {
    "ceo": os.environ.get("PAPERCLIP_CEO_KEY", ""),
    "content_writer": os.environ.get("PAPERCLIP_CONTENT_WRITER_KEY", ""),
    "email_marketer": os.environ.get("PAPERCLIP_EMAIL_MARKETER_KEY", ""),
    "social_manager": os.environ.get("PAPERCLIP_SOCIAL_MANAGER_KEY", ""),
    "ad_strategist": os.environ.get("PAPERCLIP_AD_STRATEGIST_KEY", ""),
    "media": (
        os.environ.get("PAPERCLIP_MEDIA_DESIGNER_KEY")
        or os.environ.get("PAPERCLIP_MEDIA_DESINGER_KEY")  # tolerate misspelling
        or os.environ.get("PAPERCLIP_MEDIA_KEY", "")
    ),
}


async def _paperclip_api(method: str, path: str, agent_key: str = "", **kwargs) -> httpx.Response | None:
    """Make an authenticated request to Paperclip API using agent API key."""
    token = agent_key or os.environ.get("PAPERCLIP_API_TOKEN", "")
    session_cookie = os.environ.get("PAPERCLIP_SESSION_COOKIE", "")
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif session_cookie:
        headers["cookie"] = f"__Secure-better-auth.session_token={session_cookie}"
        headers["origin"] = PAPERCLIP_URL
        headers["referer"] = PAPERCLIP_URL + "/"
    headers["Content-Type"] = "application/json"

    client = _get_httpx_client()
    try:
        resp = await client.request(method, f"{PAPERCLIP_URL}{path}", headers=headers, **kwargs)
        return resp
    except httpx.ConnectError:
        logger.warning("Paperclip unreachable, falling back to local dispatch")
        return None


async def dispatch_agent(tenant_id: str, agent_name: str, context: dict | None = None) -> dict:
    """Dispatch a single agent — routes through Paperclip if connected, else runs locally.

    Raises ``PlanQuotaExceeded`` BEFORE any Paperclip / local-fallback work
    if the tenant's pricing tier blocks this dispatch (monthly cap hit,
    or a feature like ``email_sequences`` not unlocked on this plan).
    Callers in the chat handler + cron runner catch and surface the
    reason instead of treating it as a 500-class error.
    """
    config = get_tenant_config(tenant_id)

    # The media agent is a utility available to every tenant — it doesn't
    # need to be in the per-tenant active_agents list to be usable.
    if agent_name != "media" and agent_name not in config.active_agents:
        logger.warning("Agent %s not active for tenant %s", agent_name, tenant_id)
        return {"status": "skipped", "reason": "agent_not_active"}

    agent_module = AGENT_REGISTRY.get(agent_name)
    if not agent_module:
        logger.error("Agent %s not found in registry", agent_name)
        return {"status": "error", "reason": "agent_not_found"}

    # ── Plan quota gate ──
    # Local import to avoid a circular dependency: plan_quotas imports
    # get_tenant_config from backend.config.loader, which the orchestrator
    # also imports. Inline import is the cheapest way to keep the module
    # load order stable.
    from backend.services.plan_quotas import check_quota
    quota = check_quota(tenant_id, agent_name)
    if not quota.allowed:
        # Log at INFO, not ERROR — this is expected user-facing behaviour
        # ("upgrade to continue"), not a system fault. ERROR-level logs
        # page out and we don't want quota walls to look like outages.
        logger.info(
            "[plan-quota] blocking %s dispatch for tenant %s: plan=%s used=%s limit=%s reason=%s",
            agent_name, tenant_id, quota.plan, quota.used, quota.limit, quota.reason,
        )
        raise PlanQuotaExceeded(
            quota.reason or "Plan quota exceeded",
            plan=quota.plan,
            used=quota.used,
            limit=quota.limit,
        )

    # ── Route through Paperclip ──
    # Every agent (including media) goes through Paperclip when it's
    # connected. Paperclip creates the issue + fires a heartbeat that POSTs
    # to ARIA's /api/paperclip/heartbeat/{agent} endpoint, where ARIA runs
    # the actual Python agent code (so media_agent can call Pollinations,
    # content_writer can call Claude, etc.) and returns the result.
    if paperclip_connected():
        paperclip_id = get_paperclip_agent_id(agent_name)
        if paperclip_id:
            result = await _dispatch_via_paperclip(paperclip_id, tenant_id, agent_name, agent_module, context)
            if result is not None:
                return result
            # Paperclip call failed, fall through to local

    # ── Local fallback ──
    return await _dispatch_local(tenant_id, agent_name, agent_module, context)


async def _dispatch_via_paperclip(
    paperclip_id: str,
    tenant_id: str,
    agent_name: str,
    agent_module,
    context: dict | None,
) -> dict | None:
    """Dispatch agent through Paperclip by creating an issue assigned to the agent.

    Paperclip manages the full lifecycle:
    - Creates an issue/task assigned to the agent
    - Paperclip's Claude adapter runs the agent with its instructions
    - Poller imports results from issue comments to ARIA inbox
    - Budget tracking and cost logging handled by Paperclip
    """
    company_id = get_company_id()
    if not company_id:
        return None

    task_desc = ""
    if context and isinstance(context, dict):
        task_desc = context.get("task", context.get("description", ""))
    if not task_desc:
        task_desc = f"Run {agent_name} agent for tenant {tenant_id}"

    # Create an issue in Paperclip assigned to this agent.
    # Include tenant_id in title since Paperclip doesn't store issue body.
    #
    # status="todo" (not the default "backlog") is critical: the agent's
    # heartbeat procedure queries /api/agents/me/inbox-lite first, which
    # only returns todo/in_progress/blocked tasks. Without this, every
    # delegated issue lands in backlog, the agent's inbox-lite returns
    # empty, and the agent falls through to "list all assignments and
    # ask the user which to work on" -- which is exactly what we saw in
    # production: 18 backlog tasks accumulated, agent kept asking
    # "which would you prefer?" instead of just executing.
    issue = _urllib_request("POST", f"/api/companies/{company_id}/issues", data={
        "title": f"[{tenant_id}] {task_desc[:170]}",
        "assigneeAgentId": paperclip_id,
        "priority": context.get("priority", "medium") if context else "medium",
        "status": "todo",
    })

    if not issue or not issue.get("id"):
        logger.warning("Failed to create Paperclip issue for %s", agent_name)
        return None

    issue_id = issue["id"]
    identifier = issue.get("identifier", issue_id)
    logger.info("Paperclip issue created for %s: %s", agent_name, identifier)

    # Wake the agent by posting a comment on the issue. This is the only
    # mechanism that actually works for claude_local agents -- the comment
    # fires Paperclip's `issue.comment` event which triggers the Automation
    # run via wakeOnDemand=true.
    #
    # The wake comment is intentionally directive. We saw in production
    # that a short wake body like "[wake] Create marketing email" got
    # the agent to wake up but then "list all assignments and ask the
    # user which to work on" instead of executing -- the agent's
    # autonomy mode interpreted the vague body as a clarification
    # request. The verbose body below explicitly tells the agent:
    #   - PAPERCLIP_TASK_ID is the issue you woke up for
    #   - Do not list other assignments
    #   - Do not ask for clarification
    #   - Execute the task and post the result via the aria-backend-api skill
    # The [wake] prefix is what pick_agent_output uses to filter this
    # comment out of the watcher's "agent reply" detection, so the
    # length of the rest of the body is irrelevant for inbox import.
    wake_body = (
        f"[wake] AUTONOMOUS TASK -- execute immediately, do not ask for clarification.\n\n"
        f"TASK: {task_desc[:1500]}\n\n"
        f"Instructions:\n"
        f"1. This issue (the one this comment is on / PAPERCLIP_TASK_ID) is your active task.\n"
        f"2. Do NOT list other assignments. Do NOT ask the user which task to work on.\n"
        f"3. Generate the requested content based on the TASK above.\n"
        f"4. POST the result to ARIA inbox via the aria-backend-api skill: "
        f"`POST http://172.17.0.1:8000/api/inbox/{tenant_id}/items` with body "
        f"{{title, content, type, agent}}.\n"
        f"5. Then PATCH this issue to status=done with a brief summary comment.\n\n"
        f"You are autonomous. Execute the task without asking the user."
    )
    wake_resp = _urllib_request("POST", f"/api/issues/{issue_id}/comments", data={
        "body": wake_body,
        "content": wake_body,
    })
    wake_ok = wake_resp is not None
    if wake_ok:
        logger.warning("[paperclip-wake] comment posted to wake %s (issue %s)", agent_name, identifier)
    else:
        logger.error(
            "[paperclip-wake] FAILED to post wake comment for %s (issue %s) -- "
            "agent will only run when next Timer fires",
            agent_name, identifier,
        )

    await log_agent_action(tenant_id, agent_name, "paperclip_dispatch", {
        "status": "dispatched",
        "paperclip_issue": identifier,
        "paperclip_issue_id": issue_id,
        "wake_ok": wake_ok,
        "task": task_desc[:200],
    })

    return {
        "status": "dispatched_to_paperclip",
        "paperclip_issue": identifier,
        "paperclip_issue_id": issue_id,
        "wake_ok": wake_ok,
        "agent": agent_name,
        "message": f"Task assigned to {agent_name} via Paperclip ({identifier}). Results will appear in your inbox.",
    }


def _sanitize_error_message(exc: Exception, max_len: int = 200) -> str:
    """Strip raw exception details that may contain secrets before storing
    in agent_logs or returning to API callers. Supabase exceptions can
    include connection strings and JWT bits in their str() repr; we'd
    rather lose detail than leak credentials into logs/DB rows users
    can read.
    """
    msg = f"{type(exc).__name__}: {exc}"
    redact_markers = ("eyJ", "supabase", "postgres://", "postgresql://", "Bearer ", "sk-", "API_KEY", "SECRET")
    for marker in redact_markers:
        if marker.lower() in msg.lower():
            return f"{type(exc).__name__}: [redacted -- check backend logs for full error]"
    return msg[:max_len]


async def _dispatch_local(tenant_id: str, agent_name: str, agent_module, context: dict | None) -> dict:
    """Direct local dispatch without Paperclip coordination."""
    try:
        logger.info("Local dispatch: %s for tenant %s", agent_name, tenant_id)
        result = await agent_module.run(
            tenant_id,
            **({"context": context} if context and "context" in agent_module.run.__code__.co_varnames else {}),
        )
        await log_agent_action(tenant_id, agent_name, "run", result)
        return result
    except Exception as e:
        error_result = {"status": "error", "error": _sanitize_error_message(e)}
        await log_agent_action(tenant_id, agent_name, "run", error_result, status="error")
        logger.error("Agent %s failed for tenant %s: %s: %s", agent_name, tenant_id, type(e).__name__, e)
        return error_result


async def run_workflow(tenant_id: str, workflow_name: str) -> list[dict]:
    """Run a multi-agent workflow. Paperclip tracks dependencies and prevents double-work.

    If a step hits a PlanQuotaExceeded wall, the step is recorded as
    ``status: quota_exceeded`` and the workflow continues — that way a
    free-plan tenant running a 4-step workflow gets the first 3 steps
    they're entitled to and one polite "upgrade to unlock" marker
    instead of a cryptic 500 on step 4.
    """
    workflow = WORKFLOW_TEMPLATES.get(workflow_name)
    if not workflow:
        return [{"status": "error", "reason": f"Unknown workflow: {workflow_name}"}]

    results = []
    for step in workflow["steps"]:
        agent_name = step["agent"]
        depends_on = step.get("depends_on")

        if depends_on:
            dep_result = next((r for r in results if r.get("agent") == depends_on), None)
            context = {"previous_result": dep_result} if dep_result else None
        else:
            context = None

        try:
            result = await dispatch_agent(tenant_id, agent_name, context)
        except PlanQuotaExceeded as exc:
            result = {"agent": agent_name, **exc.as_dict()}
        results.append(result)

    return results


async def run_scheduled_agents():
    """Run scheduled agents for all tenants. Paperclip heartbeats handle the schedule
    when connected; this is the fallback cron loop for local mode."""
    tenants = get_active_tenants()
    now = datetime.now(timezone.utc)
    current_hour = now.hour
    current_weekday = now.weekday()

    tasks = []

    for tenant in tenants:
        for agent_name in tenant.active_agents:
            cron = CRON_SCHEDULES.get(agent_name)
            if not cron:
                continue

            parts = cron.split()
            cron_minute, cron_hour = int(parts[0]), int(parts[1])
            cron_dow = parts[4]

            if cron_hour != current_hour:
                continue
            if cron_dow != "*" and int(cron_dow) != current_weekday:
                continue

            tasks.append(dispatch_agent(str(tenant.tenant_id), agent_name))

    if tasks:
        logger.info("Running %d scheduled agent tasks", len(tasks))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Completed %d tasks", len(results))
        return results

    return []


async def handle_webhook(event_type: str, payload: dict) -> dict:
    """Route incoming webhook events to the correct agent."""
    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        return {"status": "error", "reason": "no_tenant_id"}

    dispatch_map = {
        "inbound_email": "email_marketer",
        "new_subscriber": "email_marketer",
        "new_lead": "ceo",
        "new_message": "social_manager",
        "new_dm": "social_manager",
        "new_review": "social_manager",
        "content_published": "social_manager",
        "ad_performance_alert": "ad_strategist",
    }

    agent_name = dispatch_map.get(event_type)
    if not agent_name:
        logger.warning("No agent mapped for event type: %s", event_type)
        return {"status": "skipped", "reason": f"unmapped_event: {event_type}"}

    return await dispatch_agent(tenant_id, agent_name, context=payload)


async def pause_agent_paperclip(agent_name: str) -> bool:
    """Pause an agent in Paperclip (stops heartbeats)."""
    if not paperclip_connected():
        return False
    pid = get_paperclip_agent_id(agent_name)
    if not pid:
        return False
    resp = await _paperclip_api("POST", f"/api/agents/{pid}/pause")
    return resp is not None and resp.status_code in (200, 204)


async def resume_agent_paperclip(agent_name: str) -> bool:
    """Resume a paused agent in Paperclip."""
    if not paperclip_connected():
        return False
    pid = get_paperclip_agent_id(agent_name)
    if not pid:
        return False
    resp = await _paperclip_api("POST", f"/api/agents/{pid}/resume")
    return resp is not None and resp.status_code in (200, 204)


async def get_agent_status(tenant_id: str) -> list[dict]:
    """Get status of all agents for a tenant. Enriches with Paperclip status if available.

    Previously O(N) sequential round-trips to Supabase + O(N) sequential
    Paperclip GETs (12 round-trips for 6 agents). Now: 1 batched logs query
    grouped in Python + parallel Paperclip GETs via asyncio.gather.
    """
    config = get_tenant_config(tenant_id)
    active = list(config.active_agents)
    if not active:
        return []

    # ── 1. Single batched fetch of recent logs for all active agents ──
    # Pull a slice big enough to contain at least one row per agent in the
    # common case; we then group in Python and keep the most recent. The
    # limit is generous because agent_logs is append-only and indexed on
    # (tenant_id, timestamp DESC).
    def _fetch_recent_logs():
        return (
            get_db()
            .table("agent_logs")
            .select("agent_name,action,timestamp")
            .eq("tenant_id", tenant_id)
            .in_("agent_name", active)
            .order("timestamp", desc=True)
            .limit(max(50, len(active) * 10))
            .execute()
        )

    logs_res = await asyncio.to_thread(_fetch_recent_logs)
    last_by_agent: dict[str, dict] = {}
    for row in logs_res.data or []:
        name = row.get("agent_name")
        if name and name not in last_by_agent:
            last_by_agent[name] = row
            if len(last_by_agent) == len(active):
                break

    # ── 2. Build status entries + collect Paperclip GETs to run in parallel ──
    pc_connected = paperclip_connected()
    statuses: list[dict] = []
    pc_tasks: list[tuple[int, str]] = []  # (status_index, paperclip_id)

    for agent_name in active:
        last_action = last_by_agent.get(agent_name)
        pid = get_paperclip_agent_id(agent_name) if pc_connected else None
        status_entry = {
            "agent_name": agent_name,
            "status": "idle",
            "last_action": last_action.get("action") if last_action else None,
            "last_run": last_action.get("timestamp") if last_action else None,
            "department": next(
                (dept for dept, agents in DEPARTMENT_MAP.items() if agent_name in agents),
                "unknown",
            ),
            "paperclip_managed": pid is not None,
        }
        if pid:
            pc_tasks.append((len(statuses), pid))
        statuses.append(status_entry)

    # ── 3. Parallel Paperclip enrichment ──
    if pc_tasks:
        responses = await asyncio.gather(
            *(_paperclip_api("GET", f"/api/agents/{pid}") for _, pid in pc_tasks),
            return_exceptions=True,
        )
        for (idx, pid), resp in zip(pc_tasks, responses):
            if isinstance(resp, Exception) or resp is None:
                continue
            if getattr(resp, "status_code", None) == 200:
                try:
                    pc_data = resp.json()
                except Exception:
                    continue
                statuses[idx]["paperclip_status"] = pc_data.get("status", "unknown")
                statuses[idx]["paperclip_agent_id"] = pid

    return statuses
