"""Claude API client — calls Anthropic API directly.

All ARIA agents use this as their LLM interface.
Requires ANTHROPIC_API_KEY in environment.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict

import anthropic

logger = logging.getLogger("aria.claude")

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


# ── Usage tracking ──────────────────────────────────────────────────────────
# Simple in-memory tracker. Resets on restart.
# For production, persist to Supabase.

_usage: dict[str, dict] = defaultdict(lambda: {
    "input_tokens": 0,
    "output_tokens": 0,
    "requests": 0,
    "last_reset": time.time(),
})

# Configurable limits (per tenant, per hour)
HOURLY_REQUEST_LIMIT = int(os.getenv("ARIA_HOURLY_REQUEST_LIMIT", "60"))
HOURLY_TOKEN_LIMIT = int(os.getenv("ARIA_HOURLY_TOKEN_LIMIT", "200000"))


def _check_limits(tenant_id: str) -> None:
    """Raise if tenant has exceeded hourly limits."""
    usage = _usage[tenant_id]
    now = time.time()

    # Reset counters every hour
    if now - usage["last_reset"] > 3600:
        usage["input_tokens"] = 0
        usage["output_tokens"] = 0
        usage["requests"] = 0
        usage["last_reset"] = now
        return

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
    return dict(_usage[tenant_id])


# ── Main API call ───────────────────────────────────────────────────────────

MODEL = os.getenv("ARIA_MODEL", "claude-sonnet-4-20250514")


async def call_claude(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 4000,
    tenant_id: str = "global",
) -> str:
    """Call Anthropic API with a system prompt and user message.

    Args:
        system_prompt: Instructions for Claude's behavior
        user_message: The user's message / conversation
        max_tokens: Maximum response tokens (default 4000)
        tenant_id: For per-tenant rate limiting
    """
    _check_limits(tenant_id)

    client = _get_client()

    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.RateLimitError:
        logger.warning("Anthropic API rate limit hit")
        raise RuntimeError("API rate limit reached. Please try again in a moment.")
    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        raise RuntimeError(f"API error: {e.message}")

    # Track usage
    usage = _usage[tenant_id]
    usage["input_tokens"] += response.usage.input_tokens
    usage["output_tokens"] += response.usage.output_tokens
    usage["requests"] += 1

    # Also track global totals
    if tenant_id != "global":
        global_usage = _usage["global"]
        global_usage["input_tokens"] += response.usage.input_tokens
        global_usage["output_tokens"] += response.usage.output_tokens
        global_usage["requests"] += 1

    result = response.content[0].text
    logger.info(
        "API call: %d in + %d out tokens (tenant=%s)",
        response.usage.input_tokens,
        response.usage.output_tokens,
        tenant_id,
    )
    return result
