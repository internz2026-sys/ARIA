"""Paperclip Router — webhook callbacks from Paperclip's HTTP adapter.

When an agent in Paperclip uses the `http` adapter, Paperclip POSTs to
`/api/paperclip/heartbeat/{agent_name}` whenever it wants to invoke that
agent. ARIA runs the agent's Python code, returns the result, and lets
Paperclip record it as a successful run.

This route is exempt from JWT auth (it's listed in server.py's
_PUBLIC_PREFIXES) so Paperclip's HTTP adapter can call it without a user
session. PAPERCLIP_WEBHOOK_SECRET can be set in the env to require a
shared secret in the X-Paperclip-Webhook-Secret header.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger("aria.routers.paperclip")

router = APIRouter(tags=["Paperclip"])

# Headers Paperclip may use to send the shared secret
_SECRET_HEADER_CANDIDATES = (
    "X-Paperclip-Webhook-Secret",
    "X-Webhook-Secret",
)


@router.post("/api/paperclip/heartbeat/{agent_name}")
async def paperclip_heartbeat(agent_name: str, request: Request) -> dict:
    """Run an ARIA agent on behalf of a Paperclip heartbeat invocation."""
    _verify_webhook_secret(request)

    payload = await _read_json_body(request)
    logger.warning(
        "[paperclip-webhook-in] agent=%s payload=%s",
        agent_name,
        json.dumps(payload)[:600],
    )

    context = _extract_context(payload)
    run_id = _extract_run_id(request, payload)
    tenant_id = _resolve_tenant_id(payload)
    if not tenant_id:
        raise HTTPException(
            status_code=400,
            detail="tenant_id could not be resolved from metadata, issue title, or active tenants",
        )

    agent_module = _load_agent(agent_name)

    try:
        result = await _invoke_agent(agent_module, tenant_id, context)
    except Exception as e:
        logger.error("[paperclip-webhook-out] %s FAILED for tenant %s: %s", agent_name, tenant_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    result["paperclip_run_id"] = run_id
    await _emit_agent_event(tenant_id, result)
    logger.warning("[paperclip-webhook-out] %s OK for tenant %s", agent_name, tenant_id)
    return result


# ──────────────────────────────────────────────────────────────────────────
# Request parsing helpers
# ──────────────────────────────────────────────────────────────────────────


def _verify_webhook_secret(request: Request) -> None:
    """If PAPERCLIP_WEBHOOK_SECRET is set, require a matching header."""
    expected = os.environ.get("PAPERCLIP_WEBHOOK_SECRET", "")
    if not expected:
        return
    provided = ""
    for header in _SECRET_HEADER_CANDIDATES:
        provided = request.headers.get(header) or ""
        if provided:
            break
    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing webhook secret")


async def _read_json_body(request: Request) -> dict:
    """Read the request body as JSON, returning an empty dict on parse failure."""
    try:
        return await request.json()
    except Exception:
        return {}


def _extract_context(payload: dict) -> dict:
    """Pull the agent context dict out of either ARIA-shaped or Paperclip-shaped payloads."""
    metadata = payload.get("metadata") or {}
    return metadata.get("context") or payload.get("context") or {}


def _extract_run_id(request: Request, payload: dict) -> str:
    """Find Paperclip's run identifier across the various places it can appear."""
    return (
        request.headers.get("X-Paperclip-Run-Id")
        or payload.get("runId")
        or payload.get("run_id")
        or ""
    )


# ──────────────────────────────────────────────────────────────────────────
# tenant_id resolution chain
# ──────────────────────────────────────────────────────────────────────────


def _resolve_tenant_id(payload: dict) -> str | None:
    """Resolve tenant_id from the webhook payload, trying multiple sources.

    Order:
      1. ARIA-set metadata.tenant_id (when ARIA itself triggered the heartbeat)
      2. Issue title prefix `[tenant_id] task description` (ARIA prefixes
         issues this way when it creates them in Paperclip)
      3. First active tenant (works for single-user installs)
    """
    return (
        _tenant_from_metadata(payload)
        or _tenant_from_issue_title(payload)
        or _tenant_from_first_active()
    )


def _tenant_from_metadata(payload: dict) -> str | None:
    metadata = payload.get("metadata") or {}
    return metadata.get("tenant_id") or payload.get("tenant_id") or None


def _tenant_from_issue_title(payload: dict) -> str | None:
    issue = payload.get("issue") or payload.get("currentIssue") or {}
    title: str = issue.get("title") or payload.get("issueTitle") or ""
    if title.startswith("[") and "]" in title:
        parsed = title[1 : title.index("]")].strip()
        if parsed:
            logger.warning("[paperclip-webhook] resolved tenant_id=%s from issue title", parsed)
            return parsed
    return None


def _tenant_from_first_active() -> str | None:
    """Last-resort fallback. Single-user installs typically have one tenant."""
    try:
        from backend.config.loader import get_active_tenants

        actives = get_active_tenants()
        if not actives:
            return None
        tenant_id = str(actives[0].tenant_id)
        logger.warning(
            "[paperclip-webhook] no tenant_id in payload, defaulting to first active tenant %s",
            tenant_id,
        )
        return tenant_id
    except Exception as e:
        logger.error("[paperclip-webhook] failed to look up active tenants: %s", e)
        return None


# ──────────────────────────────────────────────────────────────────────────
# Agent dispatch
# ──────────────────────────────────────────────────────────────────────────


def _load_agent(agent_name: str) -> Any:
    """Look up an agent module by slug, or raise 404 if unknown."""
    from backend.agents import AGENT_REGISTRY

    agent_module = AGENT_REGISTRY.get(agent_name)
    if not agent_module:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}")
    return agent_module


async def _invoke_agent(agent_module: Any, tenant_id: str, context: dict) -> dict:
    """Call the agent's run() function, passing context only if it accepts it."""
    accepts_context = (
        bool(context)
        and "context" in agent_module.run.__code__.co_varnames
    )
    kwargs = {"context": context} if accepts_context else {}
    return await agent_module.run(tenant_id, **kwargs)


async def _emit_agent_event(tenant_id: str, result: dict) -> None:
    """Push the agent result to the tenant's WebSocket room.

    Imported lazily so this router doesn't take a hard dep on server.py
    (which would create a circular import).
    """
    try:
        from backend.server import sio  # type: ignore[attr-defined]
        await sio.emit("agent_event", result, room=tenant_id)
    except Exception as e:
        logger.warning("Failed to emit agent_event for tenant %s: %s", tenant_id, e)
