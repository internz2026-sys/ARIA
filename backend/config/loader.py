"""Tenant config loader — reads/writes tenant configurations from Supabase."""
from __future__ import annotations

import logging
import os
import re
import time
from uuid import UUID

from supabase import create_client, Client

from .tenant_schema import TenantConfig

logger = logging.getLogger("aria.config")

_client: Client | None = None


def _get_supabase() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        )
    return _client


# ── TTL Cache for tenant configs ──────────────────────────────────────────────
# Avoids hitting Supabase on every get_tenant_config call (30+ per request chain)
_config_cache: dict[str, tuple[TenantConfig, float]] = {}
_CACHE_TTL = 10  # seconds — short enough to pick up changes quickly
_CACHE_MAX_ENTRIES = 500  # cap to prevent unbounded growth across many tenants


def _cache_get(tenant_id: str) -> TenantConfig | None:
    entry = _config_cache.get(tenant_id)
    if entry and (time.time() - entry[1]) < _CACHE_TTL:
        return entry[0]
    return None


def _cache_set(tenant_id: str, config: TenantConfig):
    # Bounded cache: when we hit the cap, evict the oldest entry by insertion
    # order (Python dicts preserve insertion order). Cheap O(1) eviction
    # without dragging in functools.lru_cache, which doesn't fit our manual
    # invalidation pattern.
    if len(_config_cache) >= _CACHE_MAX_ENTRIES and tenant_id not in _config_cache:
        oldest = next(iter(_config_cache), None)
        if oldest is not None:
            _config_cache.pop(oldest, None)
    _config_cache[tenant_id] = (config, time.time())


def _cache_invalidate(tenant_id: str):
    _config_cache.pop(tenant_id, None)


def get_tenant_config(tenant_id: str | UUID) -> TenantConfig:
    tid = str(tenant_id)
    cached = _cache_get(tid)
    if cached:
        return cached

    sb = _get_supabase()
    result = sb.table("tenant_configs").select("*").eq("tenant_id", tid).single().execute()
    config = TenantConfig(**result.data)
    _cache_set(tid, config)
    return config


def save_tenant_config(config: TenantConfig) -> TenantConfig:
    sb = _get_supabase()
    data = config.model_dump(mode="json")
    data["tenant_id"] = str(config.tenant_id)

    # Retry loop: if Supabase rejects a column that doesn't exist in the
    # table yet, strip it and try again (up to 5 times).
    for _ in range(5):
        try:
            sb.table("tenant_configs").upsert(data).execute()
            _cache_set(str(config.tenant_id), config)
            return config
        except Exception as e:
            msg = str(e)
            m = re.search(r"Could not find the '(\w+)' column", msg)
            if m:
                col = m.group(1)
                logger.warning("Column '%s' missing in tenant_configs — stripping from save", col)
                data.pop(col, None)
                continue
            raise
    return config


def update_tenant_config(tenant_id: str | UUID, updates: dict) -> TenantConfig:
    sb = _get_supabase()
    tid = str(tenant_id)
    sb.table("tenant_configs").update(updates).eq("tenant_id", tid).execute()
    _cache_invalidate(tid)
    return get_tenant_config(tid)


def update_tenant_integrations(config: TenantConfig) -> None:
    """Persist ONLY the integrations column for a tenant.

    Hot-path callers (Gmail token refresh, OAuth callbacks) only mutate
    config.integrations but were going through save_tenant_config which
    upserts the entire row — that's a 30+ column write that also has to
    survive the column-strip retry loop. Targeted UPDATE of the integrations
    JSONB column is dramatically cheaper, especially when the loop is
    refreshing tokens for many tenants in series.

    Note: this also primes the in-memory cache with the supplied config so
    the very next get_tenant_config() in the same request doesn't refetch.
    """
    sb = _get_supabase()
    tid = str(config.tenant_id)
    integrations_json = config.integrations.model_dump(mode="json")
    sb.table("tenant_configs").update(
        {"integrations": integrations_json}
    ).eq("tenant_id", tid).execute()
    # Prime cache instead of invalidating — we already have the new state.
    _cache_set(tid, config)


def get_active_tenants() -> list[TenantConfig]:
    sb = _get_supabase()
    result = sb.table("tenant_configs").select("*").execute()
    configs = [TenantConfig(**row) for row in result.data]
    for c in configs:
        _cache_set(str(c.tenant_id), c)
    return configs
