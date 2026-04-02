"""ARIA Orchestrator — dispatches and coordinates all AI agents via Paperclip AI.

Paperclip AI (localhost:3100) handles multi-agent orchestration:
- Agent registration, heartbeats, and lifecycle management
- Org chart hierarchy and task delegation
- Budget tracking and cost control
- Atomic task checkout to prevent double-work

When Paperclip is unavailable, falls back to direct local dispatch.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from supabase import create_client

from backend.agents import AGENT_REGISTRY, DEPARTMENT_MAP
from backend.config.loader import get_tenant_config, get_active_tenants
from backend.paperclip_sync import (
    get_paperclip_agent_id,
    get_company_id,
    is_connected as paperclip_connected,
    PAPERCLIP_URL,
)
from backend.tasks.task_definitions import CRON_SCHEDULES, WORKFLOW_TEMPLATES

logger = logging.getLogger("aria.orchestrator")

_sb = None


def _get_sb():
    global _sb
    if _sb is None:
        _sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    return _sb


async def log_agent_action(tenant_id: str, agent_name: str, action: str, result: dict, status: str = "completed"):
    """Log every agent action to Supabase for dashboard activity feed."""
    try:
        _get_sb().table("agent_logs").insert({
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

    if agent_name not in config.active_agents:
        logger.warning(f"Agent {agent_name} not active for tenant {tenant_id}")
        return {"status": "skipped", "reason": "agent_not_active"}

    agent_module = AGENT_REGISTRY.get(agent_name)
    if not agent_module:
        logger.error(f"Agent {agent_name} not found in registry")
        return {"status": "error", "reason": "agent_not_found"}

    # ── Route through Paperclip ──
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
    - Agent uses ARIA API skill to save results to inbox
    - Budget tracking and cost logging handled by Paperclip
    """
    from backend.paperclip_sync import get_company_id

    company_id = get_company_id()
    if not company_id:
        return None

    task_desc = ""
    if context and isinstance(context, dict):
        task_desc = context.get("task", context.get("description", ""))
    if not task_desc:
        task_desc = f"Run {agent_name} agent for tenant {tenant_id}"

    # Create an issue in Paperclip assigned to this agent
    from backend.paperclip_sync import _urllib_request
    issue = _urllib_request("POST", f"/api/companies/{company_id}/issues", data={
        "title": task_desc[:200],
        "body": (
            f"## Task\n{task_desc}\n\n"
            f"## Context\n"
            f"- **Tenant ID**: `{tenant_id}`\n"
            f"- **Agent**: {agent_name}\n"
            f"- **API Base URL**: {os.environ.get('API_URL', 'http://172.17.0.1:8000')}\n\n"
            f"## Instructions\n"
            f"1. Read tenant config: `GET /api/dashboard/{tenant_id}/config`\n"
            f"2. Execute the task using your agent instructions\n"
            f"3. Save results to inbox: `POST /api/inbox/{tenant_id}/items`\n"
        ),
        "assigneeAgentId": paperclip_id,
        "priority": context.get("priority", "medium") if context else "medium",
    })

    if not issue or not issue.get("id"):
        logger.warning(f"Failed to create Paperclip issue for {agent_name}")
        return None

    issue_id = issue["id"]
    identifier = issue.get("identifier", issue_id)
    logger.info(f"Paperclip issue created for {agent_name}: {identifier}")

    # Trigger heartbeat using the agent's own API key
    agent_key = AGENT_API_KEYS.get(agent_name, "")
    resp = await _paperclip_api("POST", f"/api/agents/{paperclip_id}/heartbeat/invoke", agent_key=agent_key, json={
        "metadata": {
            "tenant_id": tenant_id,
            "agent_name": agent_name,
            "issue_id": issue_id,
            "triggered_at": datetime.now(timezone.utc).isoformat(),
        },
    })
    if resp and resp.status_code in (200, 201, 202):
        logger.info(f"Heartbeat triggered for {agent_name} (issue {identifier})")
    else:
        status = resp.status_code if resp else "no response"
        logger.warning(f"Heartbeat trigger failed for {agent_name} ({status}) — agent will pick up issue on next timer")

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
            _get_sb()
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
