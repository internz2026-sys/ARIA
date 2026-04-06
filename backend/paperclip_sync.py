"""Paperclip AI Sync — registers ARIA company and agents with the local Paperclip server.

On startup, ensures the ARIA company exists in Paperclip and the 5 v1 marketing agents are
registered with correct roles, departments, and heartbeat schedules.
Does NOT create new agents if they already exist — matches by name to avoid duplicates.
"""
from __future__ import annotations

import os
import logging

import httpx

from backend.agents import AGENT_REGISTRY, DEPARTMENT_MAP
from backend.tasks.task_definitions import CRON_SCHEDULES

logger = logging.getLogger("aria.paperclip")

PAPERCLIP_URL = os.environ.get("PAPERCLIP_API_URL", "http://127.0.0.1:3100")

# Maps each agent to its description and Claude model for Paperclip metadata
# role must match Paperclip enum: ceo|cto|cmo|cfo|engineer|designer|pm|qa|devops|researcher|general
AGENT_METADATA = {
    "ceo": {"title": "Chief Marketing Strategist", "role": "cmo", "model": "claude-sonnet-4-6", "description": "Builds GTM playbook, coordinates marketing team, reviews performance"},
    "content_writer": {"title": "Content Writer", "role": "general", "model": "claude-sonnet-4-6", "description": "Blog posts, landing pages, Product Hunt copy, case studies"},
    "email_marketer": {"title": "Email Marketer", "role": "general", "model": "claude-sonnet-4-6", "description": "Welcome sequences, newsletters, launch campaigns, re-engagement"},
    "social_manager": {"title": "Social Media Manager", "role": "general", "model": "claude-sonnet-4-6", "description": "X/Twitter, LinkedIn, Facebook posts and content calendar"},
    "ad_strategist": {"title": "Ad Strategist", "role": "general", "model": "claude-sonnet-4-6", "description": "Facebook ad campaigns, audience targeting, step-by-step guides"},
    "media": {"title": "Media Designer", "role": "designer", "model": "claude-haiku-4-5", "description": "Marketing image generation via Google Gemini, visual content for campaigns"},
}


def _get_department(agent_name: str) -> str:
    for dept, agents in DEPARTMENT_MAP.items():
        if agent_name in agents:
            return dept
    return "internal"


async def _api(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> httpx.Response:
    """Make an authenticated request to Paperclip API."""
    token = os.environ.get("PAPERCLIP_API_TOKEN", "")
    session_cookie = os.environ.get("PAPERCLIP_SESSION_COOKIE", "")
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_cookie:
        headers["cookie"] = f"__Secure-better-auth.session_token={session_cookie}"
        headers["origin"] = PAPERCLIP_URL
        headers["referer"] = PAPERCLIP_URL + "/"
    headers["Content-Type"] = "application/json"
    url = f"{PAPERCLIP_URL}{path}"
    logger.debug(f"Paperclip API: {method} {path} cookie={'yes' if session_cookie else 'no'}")
    resp = await client.request(method, url, headers=headers, **kwargs)
    return resp


def _urllib_request(method: str, path: str, data: dict | None = None) -> dict | list | None:
    """Make a request to Paperclip using urllib (bypasses httpx cookie issues)."""
    import urllib.request
    import ssl
    import json as _json
    session_cookie = os.environ.get("PAPERCLIP_SESSION_COOKIE", "")
    token = os.environ.get("PAPERCLIP_API_TOKEN", "")
    url = f"{PAPERCLIP_URL}{path}"
    logger.info(f"urllib {method} {url} cookie_len={len(session_cookie)} token_len={len(token)}")
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
        # Skip SSL verification for internal connections (self-signed certs / Traefik)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        r = urllib.request.urlopen(req, timeout=15, context=ctx)
        return _json.loads(r.read().decode())
    except Exception as e:
        logger.warning(f"urllib {method} {path} failed: {type(e).__name__}: {e}")
        return None


async def ensure_company(client: httpx.AsyncClient) -> str | None:
    """Create or retrieve the ARIA company in Paperclip. Returns company_id."""
    # Use urllib for company lookup (httpx has issues with __Secure- cookies over HTTP)
    # Retry up to 3 times with a small delay (network may not be ready at startup)
    import time
    for attempt in range(3):
        companies = _urllib_request("GET", "/api/companies")
        if companies is not None:
            company_list = companies if isinstance(companies, list) else companies.get("data", companies.get("companies", []))
            logger.info(f"Paperclip returned {len(company_list)} companies")
            for c in company_list:
                name = c.get("name", "")
                if name in ("ARIA", "Hoversight AI Agency", os.environ.get("PAPERCLIP_COMPANY_NAME", "")):
                    company_id = c["id"]
                    logger.info(f"Found company '{name}': {company_id}")
                    return company_id
            logger.warning(f"No matching company found. Available: {[c.get('name') for c in company_list]}")
            return None
        else:
            logger.warning(f"Paperclip company lookup attempt {attempt+1}/3 failed, retrying...")
            time.sleep(2)

    logger.error("Failed to reach Paperclip for company lookup after 3 attempts")
    return None


async def sync_agents(client: httpx.AsyncClient, company_id: str) -> dict[str, str]:
    """Sync the 5 v1 ARIA agents with Paperclip. Returns {agent_name: paperclip_agent_id}.

    Matches existing agents by title (name) to avoid creating duplicates.
    Only creates an agent if no match is found by title or slug.
    """
    # Get existing agents
    resp = await _api(client, "GET", f"/api/companies/{company_id}/agents")
    existing_by_slug = {}
    existing_by_name = {}
    if resp.status_code == 200:
        agent_list = resp.json()
        if isinstance(agent_list, dict):
            agent_list = agent_list.get("data", agent_list.get("agents", []))
        for a in agent_list:
            slug = a.get("slug") or a.get("urlKey") or ""
            name = a.get("name", "")
            if slug:
                existing_by_slug[slug] = a["id"]
            if name:
                existing_by_name[name] = a["id"]

    agent_ids = {}

    for agent_name in AGENT_REGISTRY:
        meta = AGENT_METADATA.get(agent_name, {"title": agent_name, "model": "claude-sonnet-4-6", "description": ""})
        dept = _get_department(agent_name)
        cron = CRON_SCHEDULES.get(agent_name)
        title = meta["title"]

        # Match by slug first, then by exact title name
        agent_id = existing_by_slug.get(agent_name) or existing_by_name.get(title)

        if agent_id:
            # Agent already exists — update heartbeat schedule to match our cron config
            agent_ids[agent_name] = agent_id
            if cron:
                await _api(client, "PATCH", f"/api/agents/{agent_id}", json={
                    "heartbeatSchedule": cron,
                })
                logger.info(f"Updated agent {agent_name} ({title}) heartbeat to {cron}")
            else:
                logger.debug(f"Found existing agent {agent_name} ({title}) -> {agent_id}")
        else:
            # Agent does not exist — create it
            logger.info(f"Agent {agent_name} ({title}) not found in Paperclip, creating...")
            resp = await _api(client, "POST", f"/api/companies/{company_id}/agents", json={
                "name": title,
                "slug": agent_name,
                "description": meta["description"],
                "role": meta.get("role", "general"),
                "department": dept,
                "adapter": "http",
                "model": meta["model"],
                "heartbeatSchedule": cron,
                "webhookUrl": f"http://127.0.0.1:8000/api/paperclip/heartbeat/{agent_name}",
            })
            if resp.status_code in (200, 201):
                agent_id = resp.json().get("id")
                agent_ids[agent_name] = agent_id
                logger.info(f"Registered agent {agent_name} -> {agent_id}")
            else:
                logger.error(f"Failed to register agent {agent_name}: {resp.status_code} {resp.text}")

    return agent_ids


async def sync_org_chart(client: httpx.AsyncClient, company_id: str, agent_ids: dict[str, str]):
    """Set up org chart hierarchy: department leads report to CEO-level analytics agent."""
    # This sets up reporting relationships in Paperclip
    # For now, all agents in a department report to the first agent in that department
    for dept, agent_names in DEPARTMENT_MAP.items():
        if len(agent_names) < 2:
            continue
        lead_id = agent_ids.get(agent_names[0])
        if not lead_id:
            continue
        for subordinate_name in agent_names[1:]:
            sub_id = agent_ids.get(subordinate_name)
            if sub_id:
                await _api(client, "PATCH", f"/api/agents/{sub_id}", json={
                    "reportsTo": lead_id,
                })


# Cached mapping for runtime use
_agent_id_cache: dict[str, str] = {}
_company_id_cache: str | None = None


async def initialize():
    """Run full Paperclip sync on startup. Call this from FastAPI lifespan."""
    global _agent_id_cache, _company_id_cache

    logger.info(f"Syncing with Paperclip at {PAPERCLIP_URL}...")

    # Wait for Docker network and SSL to be ready
    import asyncio
    await asyncio.sleep(10)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Check if Paperclip is reachable
        try:
            resp = await client.get(f"{PAPERCLIP_URL}/api/health")
            if resp.status_code != 200:
                logger.warning(f"Paperclip health check failed ({resp.status_code}). Running without orchestration.")
                return
        except httpx.ConnectError:
            logger.warning("Paperclip not reachable. Start it with: npx paperclipai onboard --yes")
            logger.warning("ARIA will run with local orchestration as fallback.")
            return

        company_id = await ensure_company(client)
        if not company_id:
            logger.error("Could not create/find ARIA company in Paperclip. Falling back to local orchestration.")
            return

        _company_id_cache = company_id
        logger.info(f"Company ID cached: {_company_id_cache}")
        _agent_id_cache = await sync_agents(client, company_id)
        logger.info(f"Agent IDs cached: {list(_agent_id_cache.keys())}")
        await sync_org_chart(client, company_id, _agent_id_cache)

        # Create and attach ARIA API skill to all agents
        try:
            from backend.paperclip_skill import ensure_skill, attach_skill_to_agents
            skill_id = await ensure_skill(company_id)
            if skill_id and _agent_id_cache:
                await attach_skill_to_agents(company_id, skill_id, _agent_id_cache)
                logger.info(f"ARIA API skill attached to {len(_agent_id_cache)} agents")
        except Exception as e:
            logger.warning(f"Failed to set up ARIA API skill: {e}")

        logger.info(f"Paperclip sync complete: {len(_agent_id_cache)} agents registered under company {company_id}")


def get_paperclip_agent_id(agent_name: str) -> str | None:
    # Try cache first, fall back to env-configured agent IDs
    cached = _agent_id_cache.get(agent_name)
    if cached:
        return cached
    # Fallback: check if agent API keys are configured (means Paperclip is set up)
    from backend.orchestrator import AGENT_API_KEYS
    if AGENT_API_KEYS.get(agent_name):
        # Agent IDs from Paperclip (discovered during setup)
        _KNOWN_AGENT_IDS = {
            "ceo": os.environ.get("PAPERCLIP_CEO_AGENT_ID", "1b64e9b0-4bb3-4aca-b8ad-d1eb9a7ffa7f"),
            "content_writer": os.environ.get("PAPERCLIP_CONTENT_WRITER_AGENT_ID", "f9e9abcc-e51f-4a41-8e67-7bc8111230c5"),
            "email_marketer": os.environ.get("PAPERCLIP_EMAIL_MARKETER_AGENT_ID", "da5109c3-2ab5-4a50-988e-896f078a712c"),
            "social_manager": os.environ.get("PAPERCLIP_SOCIAL_MANAGER_AGENT_ID", "37f25bf9-8dfa-4943-9cf8-f6eb1e5157f7"),
            "ad_strategist": os.environ.get("PAPERCLIP_AD_STRATEGIST_AGENT_ID", "8f827b80-b441-4065-bc50-fe3b470790af"),
        }
        return _KNOWN_AGENT_IDS.get(agent_name)
    return None


def get_company_id() -> str | None:
    if _company_id_cache:
        return _company_id_cache
    # Fallback: use known company ID if agent keys are configured
    any_key = any(os.environ.get(f"PAPERCLIP_{k}_KEY") for k in ["CEO", "CONTENT_WRITER", "EMAIL_MARKETER", "SOCIAL_MANAGER", "AD_STRATEGIST"])
    if any_key:
        return os.environ.get("PAPERCLIP_COMPANY_ID", "a33b6679-9b72-44ed-9b73-92035f32d887")
    return None


def is_connected() -> bool:
    if _company_id_cache is not None:
        return True
    # Also connected if agent API keys are configured
    return any(os.environ.get(f"PAPERCLIP_{k}_KEY") for k in ["CEO", "CONTENT_WRITER", "EMAIL_MARKETER", "SOCIAL_MANAGER", "AD_STRATEGIST"])
