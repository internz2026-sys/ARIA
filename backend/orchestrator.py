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
import urllib.request
from datetime import datetime, timezone

import httpx

from backend.agents import AGENT_REGISTRY, DEPARTMENT_MAP
from backend.config.loader import get_tenant_config, get_active_tenants
from backend.services.supabase import get_db
from backend.tasks.task_definitions import CRON_SCHEDULES, WORKFLOW_TEMPLATES

logger = logging.getLogger("aria.orchestrator")


# ── Paperclip lookup helpers (formerly in backend/paperclip_sync.py) ─────

PAPERCLIP_URL = os.environ.get("PAPERCLIP_API_URL", "http://127.0.0.1:3100")

# Hardcoded fallback IDs from the production Paperclip company. These are
# used when no live sync has populated the cache. Override via env vars per
# agent if you spin up a new Paperclip instance.
_KNOWN_COMPANY_ID = "a33b6679-9b72-44ed-9b73-92035f32d887"
_KNOWN_AGENT_IDS = {
    "ceo": "1b64e9b0-4bb3-4aca-b8ad-d1eb9a7ffa7f",
    "content_writer": "f9e9abcc-e51f-4a41-8e67-7bc8111230c5",
    "email_marketer": "da5109c3-2ab5-4a50-988e-896f078a712c",
    "social_manager": "37f25bf9-8dfa-4943-9cf8-f6eb1e5157f7",
    "ad_strategist": "8f827b80-b441-4065-bc50-fe3b470790af",
    "media": "25c7a6f4-34ff-4846-b149-502be12b836d",
}


def _urllib_request(method: str, path: str, data: dict | None = None) -> dict | list | None:
    """Make a request to Paperclip using urllib (bypasses httpx cookie issues).

    Used by the Paperclip poller and skill manager. Authenticates via the
    PAPERCLIP_SESSION_COOKIE or PAPERCLIP_API_TOKEN env var.
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
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        r = urllib.request.urlopen(req, timeout=15, context=ctx)
        return _json.loads(r.read().decode())
    except Exception as e:
        logger.warning(f"urllib {method} {path} failed: {type(e).__name__}: {e}")
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
        logger.error(f"Failed to log agent action: {e}")


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

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.request(method, f"{PAPERCLIP_URL}{path}", headers=headers, **kwargs)
            return resp
        except httpx.ConnectError:
            logger.warning("Paperclip unreachable, falling back to local dispatch")
            return None


async def dispatch_agent(tenant_id: str, agent_name: str, context: dict | None = None) -> dict:
    """Dispatch a single agent — routes through Paperclip if connected, else runs locally."""
    config = get_tenant_config(tenant_id)

    # The media agent is a utility available to every tenant — it doesn't
    # need to be in the per-tenant active_agents list to be usable.
    if agent_name != "media" and agent_name not in config.active_agents:
        logger.warning(f"Agent {agent_name} not active for tenant {tenant_id}")
        return {"status": "skipped", "reason": "agent_not_active"}

    agent_module = AGENT_REGISTRY.get(agent_name)
    if not agent_module:
        logger.error(f"Agent {agent_name} not found in registry")
        return {"status": "error", "reason": "agent_not_found"}

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

    # Create an issue in Paperclip assigned to this agent
    # Include tenant_id in title since Paperclip doesn't store issue body
    issue = _urllib_request("POST", f"/api/companies/{company_id}/issues", data={
        "title": f"[{tenant_id}] {task_desc[:170]}",
        "assigneeAgentId": paperclip_id,
        "priority": context.get("priority", "medium") if context else "medium",
    })

    if not issue or not issue.get("id"):
        logger.warning(f"Failed to create Paperclip issue for {agent_name}")
        return None

    issue_id = issue["id"]
    identifier = issue.get("identifier", issue_id)
    logger.info(f"Paperclip issue created for {agent_name}: {identifier}")

    # Trigger heartbeat — try the agent's own API key first, then fall back
    # to the master session cookie / token if Paperclip rejects with 401/403.
    # The per-agent key is the cleanest path but only works if the user
    # actually pasted a valid agent key into .env.
    heartbeat_payload = {
        "metadata": {
            "tenant_id": tenant_id,
            "agent_name": agent_name,
            "issue_id": issue_id,
            "triggered_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    agent_key = AGENT_API_KEYS.get(agent_name, "")
    resp = await _paperclip_api(
        "POST",
        f"/api/agents/{paperclip_id}/heartbeat/invoke",
        agent_key=agent_key,
        json=heartbeat_payload,
    )
    if resp is not None and resp.status_code in (401, 403):
        logger.warning(
            f"Heartbeat for {agent_name} rejected with {resp.status_code} using agent key — "
            f"retrying with master session/token"
        )
        # Retry with no agent key — _paperclip_api will fall through to
        # PAPERCLIP_API_TOKEN or PAPERCLIP_SESSION_COOKIE.
        resp = await _paperclip_api(
            "POST",
            f"/api/agents/{paperclip_id}/heartbeat/invoke",
            agent_key="",
            json=heartbeat_payload,
        )

    if resp and resp.status_code in (200, 201, 202):
        logger.warning(f"[paperclip-heartbeat] OK {agent_name} (issue {identifier})")
    else:
        status = resp.status_code if resp else "no response"
        body = resp.text[:300] if resp else ""
        logger.error(
            f"[paperclip-heartbeat] FAILED {agent_name} ({status}) body={body} — "
            f"agent will pick up issue on next scheduled timer instead"
        )

    await log_agent_action(tenant_id, agent_name, "paperclip_dispatch", {
        "status": "dispatched",
        "paperclip_issue": identifier,
        "paperclip_issue_id": issue_id,
        "task": task_desc[:200],
    })

    return {
        "status": "dispatched_to_paperclip",
        "paperclip_issue": identifier,
        "paperclip_issue_id": issue_id,
        "agent": agent_name,
        "message": f"Task assigned to {agent_name} via Paperclip ({identifier}). Results will appear in your inbox.",
    }


async def _dispatch_local(tenant_id: str, agent_name: str, agent_module, context: dict | None) -> dict:
    """Direct local dispatch without Paperclip coordination."""
    try:
        logger.info(f"Local dispatch: {agent_name} for tenant {tenant_id}")
        result = await agent_module.run(
            tenant_id,
            **({"context": context} if context and "context" in agent_module.run.__code__.co_varnames else {}),
        )
        await log_agent_action(tenant_id, agent_name, "run", result)
        return result
    except Exception as e:
        error_result = {"status": "error", "error": str(e)}
        await log_agent_action(tenant_id, agent_name, "run", error_result, status="error")
        logger.error(f"Agent {agent_name} failed for tenant {tenant_id}: {e}")
        return error_result


async def run_workflow(tenant_id: str, workflow_name: str) -> list[dict]:
    """Run a multi-agent workflow. Paperclip tracks dependencies and prevents double-work."""
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

        result = await dispatch_agent(tenant_id, agent_name, context)
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
        logger.info(f"Running {len(tasks)} scheduled agent tasks")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"Completed {len(results)} tasks")
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
        logger.warning(f"No agent mapped for event type: {event_type}")
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
    """Get status of all agents for a tenant. Enriches with Paperclip status if available."""
    config = get_tenant_config(tenant_id)
    statuses = []

    for agent_name in config.active_agents:
        last_log = (
            get_db()
            .table("agent_logs")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("agent_name", agent_name)
            .order("timestamp", desc=True)
            .limit(1)
            .execute()
        )
        last_action = last_log.data[0] if last_log.data else None

        status_entry = {
            "agent_name": agent_name,
            "status": "idle",
            "last_action": last_action.get("action") if last_action else None,
            "last_run": last_action.get("timestamp") if last_action else None,
            "department": next(
                (dept for dept, agents in DEPARTMENT_MAP.items() if agent_name in agents),
                "unknown",
            ),
            "paperclip_managed": paperclip_connected() and get_paperclip_agent_id(agent_name) is not None,
        }

        # Enrich with Paperclip live status
        if status_entry["paperclip_managed"]:
            pid = get_paperclip_agent_id(agent_name)
            resp = await _paperclip_api("GET", f"/api/agents/{pid}")
            if resp and resp.status_code == 200:
                pc_data = resp.json()
                status_entry["paperclip_status"] = pc_data.get("status", "unknown")
                status_entry["paperclip_agent_id"] = pid

        statuses.append(status_entry)

    return statuses
