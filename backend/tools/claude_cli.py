"""Claude CLI client — calls Claude via the local CLI subprocess.

All ARIA agents use this as their LLM interface.
Uses the Claude CLI (authenticated via Max subscription) — no ANTHROPIC_API_KEY needed.
Usage is persisted to Supabase so limits survive server restarts.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("aria.claude")

# ── Model constants ────────────────────────────────────────────────────────────
MODEL_OPUS = "claude-opus-4-6"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5"

# Cache the resolved `claude` binary path at module load. shutil.which()
# walks PATH on each call (~500us-3ms on Windows, less on Linux), and we
# spawn the CLI on every agent call. Resolving once means subprocess
# invocation skips the PATH lookup. Falls back to "claude" if which()
# can't find it (e.g. before npm install) — exec will surface the error.
_CLAUDE_BIN: str = shutil.which("claude") or "claude"

# ── Supabase helper ─────────────────────────────────────────────────────────

def _get_supabase():
    from backend.services.supabase import get_db
    return get_db()


# ── Usage tracking (persisted to Supabase) ──────────────────────────────────

# Configurable limits (per tenant, per hour)
HOURLY_REQUEST_LIMIT = int(os.getenv("ARIA_HOURLY_REQUEST_LIMIT", "60"))
HOURLY_TOKEN_LIMIT = int(os.getenv("ARIA_HOURLY_TOKEN_LIMIT", "200000"))


def _estimate_tokens(text: str) -> int:
    """Rough token estimate — 4 chars per token is the standard heuristic
    for English Claude prompts. Not exact, but reliable enough for
    rate-limit tracking and dashboard display since the real Claude CLI
    doesn't emit usage metadata in --output-format=text mode (switching
    to JSON would risk breaking every caller that treats the return
    value as plain text)."""
    if not text:
        return 0
    return max(1, len(text) // 4)

# Per-agent hourly limits — keeps any single agent from hogging the budget
AGENT_HOURLY_LIMITS: dict[str, dict] = {
    "ceo": {"requests": 30, "tokens": 80000},
    "content_writer": {"requests": 10, "tokens": 40000},
    "email_marketer": {"requests": 15, "tokens": 40000},
    "social_manager": {"requests": 10, "tokens": 40000},
    "ad_strategist": {"requests": 10, "tokens": 40000},
    "media": {"requests": 15, "tokens": 20000},
}
DEFAULT_AGENT_LIMIT = {"requests": 15, "tokens": 40000}

# Local cache to avoid hitting Supabase on every single check. Bounded so a
# busy multi-tenant deployment can't grow these dicts unbounded over the
# lifetime of the process — eviction is by insertion order (oldest tenant out).
_usage_cache: dict[str, dict] = {}
_USAGE_CACHE_MAX = 1000


def _usage_cache_set(tenant_id: str, usage: dict) -> None:
    """Set usage cache entry with bounded eviction."""
    if tenant_id not in _usage_cache and len(_usage_cache) >= _USAGE_CACHE_MAX:
        oldest = next(iter(_usage_cache), None)
        if oldest is not None:
            _usage_cache.pop(oldest, None)
    _usage_cache[tenant_id] = usage


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
        if result and getattr(result, "data", None):
            usage = {
                "hour": hour,
                "input_tokens": result.data.get("input_tokens") or 0,
                "output_tokens": result.data.get("output_tokens") or 0,
                "requests": result.data.get("requests") or 0,
            }
        else:
            usage = {"hour": hour, "input_tokens": 0, "output_tokens": 0, "requests": 0}
    except Exception as e:
        logger.warning("Failed to load usage from Supabase: %s — using cache/defaults", e)
        usage = cached or {"hour": hour, "input_tokens": 0, "output_tokens": 0, "requests": 0}

    _usage_cache_set(tenant_id, usage)
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
    if tenant_id not in _agent_usage and len(_agent_usage) >= _USAGE_CACHE_MAX:
        oldest = next(iter(_agent_usage), None)
        if oldest is not None:
            _agent_usage.pop(oldest, None)
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
        input_tokens = entry.get("input_tokens") or 0
        output_tokens = entry.get("output_tokens") or 0
        summary[agent_id] = {
            "requests": entry.get("requests", 0),
            "request_limit": limits["requests"],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "token_limit": limits["tokens"],
        }
    return summary


# ── Main CLI call ─────────────────────────────────────────────────────────

DEFAULT_MODEL = os.getenv("ARIA_MODEL", MODEL_SONNET)
CLI_TIMEOUT = int(os.getenv("ARIA_CLI_TIMEOUT", "120"))

# Process-wide lock so concurrent CLI calls don't both try to restore
# ~/.claude.json at the same time. Without this, two callers could both
# see the file missing, both pick the same backup, and one could leave a
# truncated file mid-copy. The lock is RLock so the same thread calling
# the helper twice (e.g. during retry) doesn't deadlock.
_claude_config_restore_lock = threading.RLock()


def _try_restore_claude_config() -> bool:
    """Self-heal a missing ~/.claude.json by copying the most recent backup.

    The Claude CLI rotates its auth config periodically and occasionally
    leaves the live file deleted while only the backup survives (race
    between write-backup and overwrite-live, made worse under load when
    multiple CLI invocations fight over the file). When that happens
    every subsequent call dies with:

        Claude CLI error: Claude configuration file not found at: /root/.claude.json
        A backup file exists at: /root/.claude/backups/.claude.json.backup.<ts>

    Without auto-recovery the only fix is SSH + manual cp. This helper
    runs the same cp programmatically using the most-recent backup, so
    chat self-heals on the next request instead of being broken until
    someone wakes up. Returns True iff a restore actually happened.

    Cross-platform safe: uses Path.home() so the same code works inside
    the docker container (root home = /root) and locally on dev machines.
    """
    # Take the process-wide lock for the whole check+copy sequence so two
    # concurrent CLI calls can't both try to restore from the same backup.
    # Without this lock there's a TOCTOU race where one caller's partial
    # write can be observed by the other as a "valid" file and consumed.
    with _claude_config_restore_lock:
        config = Path.home() / ".claude.json"
        if config.exists() and config.stat().st_size > 0:
            return False  # nothing to restore
        backups_dir = Path.home() / ".claude" / "backups"
        if not backups_dir.exists():
            logger.error(
                "Cannot auto-restore .claude.json: backups dir %s does not exist. "
                "Run `claude /login` to re-authenticate.", backups_dir,
            )
            return False
        backups = sorted(
            backups_dir.glob(".claude.json.backup.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not backups:
            logger.error(
                "Cannot auto-restore .claude.json: no backups found in %s. "
                "Run `claude /login` to re-authenticate.", backups_dir,
            )
            return False
        latest = backups[0]
        # Atomic-ish restore: write to a temp file first, fsync, then rename.
        # If the process dies mid-write, the live config is either the old
        # missing file or the new complete file -- never a half-written one.
        try:
            tmp_path = config.with_suffix(".json.tmp")
            shutil.copy2(latest, tmp_path)
            tmp_path.chmod(0o600)
            os.replace(tmp_path, config)  # atomic on POSIX
            logger.warning(
                "Auto-restored %s from %s (CLI config rotation race recovered)",
                config, latest.name,
            )
            return True
        except Exception as e:
            logger.error("Failed to auto-restore %s from %s: %s", config, latest, e)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return False


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

    # Check semantic cache first — but NOT for the CEO. CEO chat is
    # contextual dialogue where the same words mean different things turn
    # to turn ("create a lead for Hanz" vs "create an email for Hanz" had
    # 0.93 cosine similarity and were collapsing to the same cached reply).
    # Other agents (content_writer, email_marketer, etc.) keep the cache
    # because they generate reusable content where dedup is helpful.
    cache_eligible = agent_id != "ceo"
    if cache_eligible:
        try:
            from backend.services.semantic_cache import search_cache
            cached = search_cache(system_prompt, prompt, use_model, agent_id=agent_id)
            if cached:
                logger.info("Semantic cache hit — skipping CLI call (agent=%s)", agent_id)
                return cached
        except Exception as e:
            logger.debug("Semantic cache unavailable: %s", e)

    # Build claude CLI command. --max-turns 5 (not 1) because the CLI
    # counts internal reasoning + any tool consideration as turns, and
    # `--max-turns 1` was failing on complex chat requests like
    # "create another lead in CRM" with `Reached max turns (1)`. Five
    # gives the model headroom to think + (optionally) use a read-only
    # tool + emit the final reply, while still bounding any runaway
    # tool spiral (most chat replies still finish in 1-2 turns).
    cmd = [
        _CLAUDE_BIN,
        "-p", prompt,
        "--output-format", "text",
        "--model", use_model,
        "--max-turns", "5",
    ]

    # Add system prompt via --append-system-prompt
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])

    logger.info("CLI call: model=%s, tenant=%s, agent=%s", use_model, tenant_id, agent_id)

    async def _run_cli():
        """Spawn the CLI subprocess once. Always reaps the process on exit
        (even on timeout) so we don't leak zombies under concurrent load."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            so, se = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT)
        except asyncio.TimeoutError:
            # Kill + WAIT (the original code only killed). Without the wait
            # the zombie sits in the process table until the next event loop
            # tick reaps it, and under load these stack up until we run out
            # of file descriptors.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
            raise RuntimeError(f"Claude CLI timed out after {CLI_TIMEOUT}s")
        return proc.returncode, so, se

    def _safe_decode(b: bytes) -> str:
        """Decode bytes from CLI output, replacing any invalid UTF-8 instead
        of crashing the whole handler with a UnicodeDecodeError."""
        if not b:
            return ""
        try:
            return b.decode("utf-8", errors="replace")
        except Exception:
            return ""

    try:
        returncode, stdout, stderr = await _run_cli()
    except FileNotFoundError:
        raise RuntimeError(
            "Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
        )

    if returncode != 0:
        # The CLI sometimes prints "configuration file not found" to stdout
        # instead of stderr depending on version, so combine both streams
        # before reporting or matching.
        stdout_str = _safe_decode(stdout)
        stderr_str = _safe_decode(stderr)
        err = (stderr_str + "\n" + stdout_str).strip()
        # Self-heal the "config file not found" race: the CLI rotates its
        # auth file and occasionally leaves only the backup. We call the
        # restore helper unconditionally on any non-zero exit -- it's a
        # no-op when ~/.claude.json already exists, so it's always safe.
        # Only retry if the restore actually did something (returns True).
        if _try_restore_claude_config():
            logger.warning("Retrying CLI call after auto-restore of .claude.json")
            try:
                returncode, stdout, stderr = await _run_cli()
            except FileNotFoundError:
                raise RuntimeError(
                    "Claude CLI not found after restore. Install with: npm install -g @anthropic-ai/claude-code"
                )
            if returncode != 0:
                err = (_safe_decode(stderr) + "\n" + _safe_decode(stdout)).strip()
                logger.error("Claude CLI error after restore (exit %d): %s", returncode, err)
                raise RuntimeError(f"Claude CLI error: {err}")
        else:
            logger.error("Claude CLI error (exit %d): %s", returncode, err)
            raise RuntimeError(f"Claude CLI error: {err}")

    result = _safe_decode(stdout).strip()

    # Token tracking — estimate from input/output text length since the
    # --output-format=text CLI doesn't emit usage metadata. 4 chars per
    # token is the standard English heuristic. Good enough for rate
    # limits and dashboard display; exact accounting would require
    # switching the CLI to --output-format=json which breaks every
    # caller that treats the return value as plain text.
    input_text = (system_prompt or "") + (prompt or "")
    input_tokens = _estimate_tokens(input_text)
    output_tokens = _estimate_tokens(result)

    # Update usage tracking — tenant-scoped counters
    usage = _load_usage(tenant_id)
    usage["requests"] += 1
    usage["input_tokens"] = (usage.get("input_tokens") or 0) + input_tokens
    usage["output_tokens"] = (usage.get("output_tokens") or 0) + output_tokens
    _usage_cache_set(tenant_id, usage)
    _save_usage(tenant_id, usage)

    if agent_id:
        agent_usage = _get_agent_usage(tenant_id, agent_id)
        agent_usage["requests"] += 1
        agent_usage["input_tokens"] = (agent_usage.get("input_tokens") or 0) + input_tokens
        agent_usage["output_tokens"] = (agent_usage.get("output_tokens") or 0) + output_tokens
        agent_usage["total_tokens"] = agent_usage["input_tokens"] + agent_usage["output_tokens"]

    if tenant_id != "global":
        global_usage = _load_usage("global")
        global_usage["requests"] += 1
        global_usage["input_tokens"] = (global_usage.get("input_tokens") or 0) + input_tokens
        global_usage["output_tokens"] = (global_usage.get("output_tokens") or 0) + output_tokens
        _usage_cache_set("global", global_usage)
        _save_usage("global", global_usage)

    # Store in semantic cache — same exclusion as the lookup above. Don't
    # cache CEO replies; they are contextual dialogue, not reusable artifacts.
    if cache_eligible:
        try:
            from backend.services.semantic_cache import store_cache
            store_cache(system_prompt, prompt, use_model, result, agent_id=agent_id)
        except Exception as e:
            logger.debug("Failed to cache response: %s", e)

    logger.info("CLI call complete: %d chars returned (model=%s, tenant=%s)", len(result), use_model, tenant_id)
    return result
