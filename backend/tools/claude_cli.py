"""Claude API client — calls Anthropic API directly.

All ARIA agents use this as their LLM interface.
Requires ANTHROPIC_API_KEY in environment.
Usage is persisted to Supabase so limits survive server restarts.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import anthropic

logger = logging.getLogger("aria.claude")

# ── Model constants ────────────────────────────────────────────────────────────
MODEL_SONNET = "claude-sonnet-4-20250514"
MODEL_HAIKU = "claude-haiku-4-5-20251001"

# ── Anthropic client (initialized once) ─────────────────────────────────────
_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = anthropic.AsyncAnthropic(api_key=api_key)
    return _client


# ── Supabase helper ─────────────────────────────────────────────────────────

def _get_supabase():
    from backend.config.loader import _get_supabase
    return _get_supabase()


# ── Usage tracking (persisted to Supabase) ──────────────────────────────────

# Configurable limits (per tenant, per hour)
HOURLY_REQUEST_LIMIT = int(os.getenv("ARIA_HOURLY_REQUEST_LIMIT", "60"))
HOURLY_TOKEN_LIMIT = int(os.getenv("ARIA_HOURLY_TOKEN_LIMIT", "200000"))

# Per-agent hourly limits (requests, tokens) — keeps any single agent from hogging the budget
AGENT_HOURLY_LIMITS: dict[str, dict] = {
    "ceo": {"requests": 30, "tokens": 80000},
    "content_writer": {"requests": 10, "tokens": 50000},
    "email_marketer": {"requests": 15, "tokens": 40000},
    "social_manager": {"requests": 10, "tokens": 30000},
    "ad_strategist": {"requests": 10, "tokens": 30000},
}
DEFAULT_AGENT_LIMIT = {"requests": 15, "tokens": 40000}

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

    total_tokens = usage["input_tokens"] + usage["output_tokens"]
    if total_tokens >= HOURLY_TOKEN_LIMIT:
        raise RuntimeError(
            f"Token limit exceeded: {HOURLY_TOKEN_LIMIT} tokens/hour. "
            "Please wait before sending more messages."
        )


def get_usage(tenant_id: str = "global") -> dict:
    """Return current usage stats for a tenant."""
    return _load_usage(tenant_id)


# ── Per-agent usage tracking (in-memory, resets each hour) ─────────────────

_agent_usage: dict[str, dict[str, dict]] = {}  # {tenant_id: {agent_id: {hour, requests, tokens}}}


def _get_agent_usage(tenant_id: str, agent_id: str) -> dict:
    hour = _current_hour()
    tenant_agents = _agent_usage.setdefault(tenant_id, {})
    entry = tenant_agents.get(agent_id)
    if entry and entry.get("hour") == hour:
        return entry
    entry = {"hour": hour, "requests": 0, "input_tokens": 0, "output_tokens": 0}
    tenant_agents[agent_id] = entry
    return entry


def _check_agent_limits(tenant_id: str, agent_id: str) -> None:
    if not agent_id:
        return
    limits = AGENT_HOURLY_LIMITS.get(agent_id, DEFAULT_AGENT_LIMIT)
    usage = _get_agent_usage(tenant_id, agent_id)
    if usage["requests"] >= limits["requests"]:
        raise RuntimeError(f"Agent '{agent_id}' rate limit: {limits['requests']} requests/hour reached.")
    total = usage["input_tokens"] + usage["output_tokens"]
    if total >= limits["tokens"]:
        raise RuntimeError(f"Agent '{agent_id}' token limit: {limits['tokens']} tokens/hour reached.")


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
            "input_tokens": entry["input_tokens"],
            "output_tokens": entry["output_tokens"],
            "total_tokens": entry["input_tokens"] + entry["output_tokens"],
            "token_limit": limits["tokens"],
        }
    return summary


# ── Main API call ───────────────────────────────────────────────────────────

DEFAULT_MODEL = os.getenv("ARIA_MODEL", MODEL_SONNET)


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
    """Call Anthropic API with a system prompt and user message.

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

    client = _get_client()
    use_model = model or DEFAULT_MODEL

    # Build messages array — multi-turn if provided, else single-turn
    msg_array = messages if messages else [{"role": "user", "content": user_message}]

    # Prompt caching: wrap system prompt so repeated calls reuse cached tokens
    system_block = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]

    try:
        response = await client.messages.create(
            model=use_model,
            max_tokens=max_tokens,
            system=system_block,
            messages=msg_array,
        )
    except anthropic.RateLimitError:
        logger.warning("Anthropic API rate limit hit")
        raise RuntimeError("API rate limit reached. Please try again in a moment.")
    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        raise RuntimeError(f"API error: {e.message}")

    # Update local cache
    usage = _load_usage(tenant_id)
    usage["input_tokens"] += response.usage.input_tokens
    usage["output_tokens"] += response.usage.output_tokens
    usage["requests"] += 1
    _usage_cache[tenant_id] = usage

    # Persist to Supabase
    _save_usage(tenant_id, usage)

    # Track per-agent usage
    if agent_id:
        agent_usage = _get_agent_usage(tenant_id, agent_id)
        agent_usage["requests"] += 1
        agent_usage["input_tokens"] += response.usage.input_tokens
        agent_usage["output_tokens"] += response.usage.output_tokens

    # Also track global totals
    if tenant_id != "global":
        global_usage = _load_usage("global")
        global_usage["input_tokens"] += response.usage.input_tokens
        global_usage["output_tokens"] += response.usage.output_tokens
        global_usage["requests"] += 1
        _usage_cache["global"] = global_usage
        _save_usage("global", global_usage)

    result = response.content[0].text
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
    logger.info(
        "API call: %d in (%d cached) + %d out tokens (model=%s, tenant=%s)",
        response.usage.input_tokens,
        cache_read,
        response.usage.output_tokens,
        use_model,
        tenant_id,
    )
    return result
