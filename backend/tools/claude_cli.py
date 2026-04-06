"""Claude CLI client — calls Claude via the local CLI subprocess.

All ARIA agents use this as their LLM interface.
Uses the Claude CLI (authenticated via Max subscription) — no ANTHROPIC_API_KEY needed.
Usage is persisted to Supabase so limits survive server restarts.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("aria.claude")

# ── Model constants ────────────────────────────────────────────────────────────
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5"

# ── Supabase helper ─────────────────────────────────────────────────────────

def _get_supabase():
    from backend.services.supabase import get_db
    return get_db()


# ── Usage tracking (persisted to Supabase) ──────────────────────────────────

# Configurable limits (per tenant, per hour)
HOURLY_REQUEST_LIMIT = int(os.getenv("ARIA_HOURLY_REQUEST_LIMIT", "60"))

# Per-agent hourly limits (requests) — keeps any single agent from hogging the budget
AGENT_HOURLY_LIMITS: dict[str, dict] = {
    "ceo": {"requests": 30},
    "content_writer": {"requests": 10},
    "email_marketer": {"requests": 15},
    "social_manager": {"requests": 10},
    "ad_strategist": {"requests": 10},
}
DEFAULT_AGENT_LIMIT = {"requests": 15}

# Local cache to avoid hitting Supabase on every single check
_usage_cache: dict[str, dict] = {}


def _current_hour() -> str:
    """Return current UTC hour as 'YYYY-MM-DD-HH' string for bucketing."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")


def _load_usage(tenant_id: str) -> dict:
    """Load usage from Supabase for the current hour. Returns cached if fresh."""
    hour = _current_hour()
    cached = _usage_cache.get(tenant_id)
    if cached and cached.get("hour") == hour:
        return cached

    try:
        sb = _get_supabase()
        result = (
            sb.table("api_usage")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("hour", hour)
            .maybe_single()
            .execute()
        )
        if result.data:
            usage = {
                "hour": hour,
                "input_tokens": result.data["input_tokens"],
                "output_tokens": result.data["output_tokens"],
                "requests": result.data["requests"],
            }
        else:
            usage = {"hour": hour, "input_tokens": 0, "output_tokens": 0, "requests": 0}
    except Exception as e:
        logger.warning("Failed to load usage from Supabase: %s — using cache/defaults", e)
        usage = cached or {"hour": hour, "input_tokens": 0, "output_tokens": 0, "requests": 0}

    _usage_cache[tenant_id] = usage
    return usage


def _save_usage(tenant_id: str, usage: dict) -> None:
    """Persist usage counters to Supabase (upsert by tenant_id + hour)."""
    try:
        sb = _get_supabase()
        sb.table("api_usage").upsert(
            {
                "tenant_id": tenant_id,
                "hour": usage["hour"],
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "requests": usage["requests"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="tenant_id,hour",
        ).execute()
    except Exception as e:
        logger.warning("Failed to save usage to Supabase: %s", e)


def _check_limits(tenant_id: str) -> None:
    """Raise if tenant has exceeded hourly limits."""
    usage = _load_usage(tenant_id)

    if usage["requests"] >= HOURLY_REQUEST_LIMIT:
        raise RuntimeError(
            f"Rate limit exceeded: {HOURLY_REQUEST_LIMIT} requests/hour. "
            "Please wait before sending more messages."
        )


def get_usage(tenant_id: str = "global") -> dict:
    """Return current usage stats for a tenant."""
    return _load_usage(tenant_id)


# ── Per-agent usage tracking (in-memory, resets each hour) ─────────────────

_agent_usage: dict[str, dict[str, dict]] = {}


def _get_agent_usage(tenant_id: str, agent_id: str) -> dict:
    hour = _current_hour()
    tenant_agents = _agent_usage.setdefault(tenant_id, {})
    entry = tenant_agents.get(agent_id)
    if entry and entry.get("hour") == hour:
        return entry
    entry = {"hour": hour, "requests": 0}
    tenant_agents[agent_id] = entry
    return entry


def _check_agent_limits(tenant_id: str, agent_id: str) -> None:
    if not agent_id:
        return
    limits = AGENT_HOURLY_LIMITS.get(agent_id, DEFAULT_AGENT_LIMIT)
    usage = _get_agent_usage(tenant_id, agent_id)
    if usage["requests"] >= limits["requests"]:
        raise RuntimeError(f"Agent '{agent_id}' rate limit: {limits['requests']} requests/hour reached.")


def get_agent_usage_summary(tenant_id: str) -> dict:
    """Return per-agent usage for the current hour."""
    hour = _current_hour()
    tenant_agents = _agent_usage.get(tenant_id, {})
    summary = {}
    for agent_id, entry in tenant_agents.items():
        if entry.get("hour") != hour:
            continue
        limits = AGENT_HOURLY_LIMITS.get(agent_id, DEFAULT_AGENT_LIMIT)
        summary[agent_id] = {
            "requests": entry["requests"],
            "request_limit": limits["requests"],
        }
    return summary


# ── Main CLI call ─────────────────────────────────────────────────────────

DEFAULT_MODEL = os.getenv("ARIA_MODEL", MODEL_SONNET)
CLI_TIMEOUT = int(os.getenv("ARIA_CLI_TIMEOUT", "120"))


async def call_claude(
    system_prompt: str,
    user_message: str = "",
    *,
    max_tokens: int = 4000,
    tenant_id: str = "global",
    model: str | None = None,
    messages: list[dict] | None = None,
    agent_id: str = "",
) -> str:
    """Call Claude via the CLI subprocess.

    Args:
        system_prompt: Instructions for Claude's behavior
        user_message: The user's message (ignored if messages is provided)
        max_tokens: Maximum response tokens (default 4000)
        tenant_id: For per-tenant rate limiting
        model: Override model (defaults to ARIA_MODEL env or Sonnet)
        messages: Multi-turn message list; if provided, replaces user_message
        agent_id: Agent slug for per-agent usage tracking and limits
    """
    _check_limits(tenant_id)
    _check_agent_limits(tenant_id, agent_id)

    use_model = model or DEFAULT_MODEL

    # Build the prompt from messages or user_message
    if messages:
        prompt_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                prompt_parts.append(f"User: {content}")
            elif role == "assistant":
                prompt_parts.append(f"Assistant: {content}")
        prompt = "\n\n".join(prompt_parts)
    else:
        prompt = user_message

    # Build claude CLI command
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "text",
        "--model", use_model,
        "--max-turns", "1",
    ]

    # Add system prompt via --append-system-prompt
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])

    logger.info("CLI call: model=%s, tenant=%s, agent=%s", use_model, tenant_id, agent_id)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=CLI_TIMEOUT
        )
    except asyncio.TimeoutError:
        process.kill()
        raise RuntimeError(f"Claude CLI timed out after {CLI_TIMEOUT}s")
    except FileNotFoundError:
        raise RuntimeError(
            "Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
        )

    if process.returncode != 0:
        err = stderr.decode().strip()
        logger.error("Claude CLI error (exit %d): %s", process.returncode, err)
        raise RuntimeError(f"Claude CLI error: {err}")

    result = stdout.decode().strip()

    # Update usage tracking
    usage = _load_usage(tenant_id)
    usage["requests"] += 1
    _usage_cache[tenant_id] = usage
    _save_usage(tenant_id, usage)

    if agent_id:
        agent_usage = _get_agent_usage(tenant_id, agent_id)
        agent_usage["requests"] += 1

    if tenant_id != "global":
        global_usage = _load_usage("global")
        global_usage["requests"] += 1
        _usage_cache["global"] = global_usage
        _save_usage("global", global_usage)

    logger.info("CLI call complete: %d chars returned (model=%s, tenant=%s)", len(result), use_model, tenant_id)
    return result
