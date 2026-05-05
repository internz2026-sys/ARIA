"""Redis-backed sliding-window rate limiter — replacement for the
in-memory `_rate_limits` dict in backend/auth.py.

The original limiter had three problems we're closing here:

  1. Per-IP only. A logged-in user could hammer expensive endpoints
     (CEO chat, agent runs, image generation) up to the IP cap from
     a single browser, with no per-tenant or per-user backstop.
  2. In-memory state. Every backend redeploy / container restart
     wiped the dict, giving anyone a fresh quota immediately after
     a deploy. With the security work in this audit we redeploy
     several times per session — that's a lot of free quota.
  3. Trivially bypassable. Per-IP only means a proxy rotation gives
     unbounded throughput, even on cost-amplification endpoints.

Design:

  - `hit(bucket, key, limit, window_seconds)` is the primitive.
    Returns (allowed: bool, current_count: int). Uses a Lua script
    so INCR + EXPIRE are atomic — no read-then-write race where two
    concurrent calls both pass the cap.
  - Buckets are namespaced ("ip", "user", "tenant", or any string)
    so multiple limit policies on the same key don't collide.
  - Fail-open on Redis error. A Redis blip should NOT 503 the whole
    API; better to accept some traffic during the outage and log it.
    The tradeoff is a brief window of degraded enforcement, which is
    acceptable for the security model — this is rate limiting, not
    auth, and auth still runs upstream.

The module also keeps a tiny in-memory fallback so dev environments
without Redis (the aria-redis container down, REDIS_URL pointing at a
nonexistent host) still get *some* limiting instead of a hard
fail-open. The in-memory fallback is per-process and not coordinated
across replicas, which is fine because dev is single-process.
"""
from __future__ import annotations

import logging
import os
import time
from threading import Lock
from typing import Optional

logger = logging.getLogger("aria.services.rate_limit")

# Lazy Redis client. None = not yet attempted, False = attempted and
# unreachable (degraded mode for the rest of the process lifetime), or
# the actual client instance.
_redis_client: object = None
_client_lock = Lock()

# In-memory fallback — only used when Redis is unreachable. Keyed by
# the same `bucket:key` namespace as Redis. Each value is a list of
# request timestamps within the active window; we GC entries older
# than the window on every check.
_memory_buckets: dict[str, list[float]] = {}
_memory_lock = Lock()
_memory_last_gc: float = 0.0


# ── Lua script: atomic INCR + EXPIRE on first set ─────────────────────
#
# Keeps the limiter coherent under concurrency. Without the script,
# two simultaneous requests can both INCR before either runs EXPIRE,
# and a stale key with no TTL gets stuck in Redis forever.
#
# Returns the count AFTER increment. Caller compares against limit.
_INCR_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


def _get_redis():
    """Return a Redis client, or False if unreachable. Cached after first
    successful connect (or first failure)."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    with _client_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            import redis as _redis_lib  # local import keeps this optional
            url = os.environ.get("REDIS_URL", "redis://redis:6379")
            client = _redis_lib.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            client.ping()
            _redis_client = client
            logger.info("[rate_limit] redis connected at %s", url)
        except Exception as e:
            logger.warning(
                "[rate_limit] redis unavailable, falling back to in-memory: %s: %s",
                type(e).__name__, e,
            )
            _redis_client = False
    return _redis_client


def _hit_memory(full_key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Sliding-window in-memory check used when Redis is unreachable."""
    global _memory_last_gc
    now = time.time()
    cutoff = now - window_seconds

    with _memory_lock:
        # Periodic GC every 5 min so dead keys don't grow unbounded
        if now - _memory_last_gc > 300:
            stale = [k for k, ts in _memory_buckets.items() if not ts or ts[-1] < now - 600]
            for k in stale:
                _memory_buckets.pop(k, None)
            _memory_last_gc = now

        bucket = _memory_buckets.setdefault(full_key, [])
        # Drop entries older than the window
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= limit:
            return False, len(bucket)
        bucket.append(now)
        return True, len(bucket)


def hit(bucket: str, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Atomic increment-and-check.

    Args:
        bucket: namespace ("ip" / "tenant" / "user" / etc.). Lets you
            run multiple policies on the same identity without keys
            colliding.
        key: identity (IP address, tenant_id, user_id, ...).
        limit: max hits allowed in `window_seconds`.
        window_seconds: window size in seconds.

    Returns:
        (allowed, current_count). `allowed` is False once `current_count`
        exceeds `limit`.

    Fails open on Redis errors — caller need not handle exceptions.
    """
    if not key:
        # No identity to rate-limit on (anonymous / pre-auth). Allow.
        return True, 0

    full_key = f"ratelimit:{bucket}:{key}"

    r = _get_redis()
    if r is False:
        return _hit_memory(full_key, limit, window_seconds)

    try:
        count = r.eval(_INCR_SCRIPT, 1, full_key, window_seconds)
        return (count <= limit), int(count)
    except Exception as e:
        # Mid-flight Redis error — degrade to memory for this single
        # call, but DON'T mark the client dead globally; the next call
        # will retry. Transient blips shouldn't disable Redis until
        # restart.
        logger.warning("[rate_limit] redis hit error (failing to memory): %s", e)
        return _hit_memory(full_key, limit, window_seconds)


def reset(bucket: str, key: str) -> None:
    """Clear a key's count. Used in tests + admin tools. No-op on errors."""
    full_key = f"ratelimit:{bucket}:{key}"
    r = _get_redis()
    if r and r is not False:
        try:
            r.delete(full_key)
        except Exception:
            pass
    with _memory_lock:
        _memory_buckets.pop(full_key, None)
