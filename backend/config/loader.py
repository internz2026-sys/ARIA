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


def _cache_get(tenant_id: str) -> TenantConfig | None:
    entry = _config_cache.get(tenant_id)
    if entry and (time.time() - entry[1]) < _CACHE_TTL:
        return entry[0]
    return None


def _cache_set(tenant_id: str, config: TenantConfig):
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


def get_active_tenants() -> list[TenantConfig]:
    sb = _get_supabase()
    result = sb.table("tenant_configs").select("*").execute()
    configs = [TenantConfig(**row) for row in result.data]
    for c in configs:
        _cache_set(str(c.tenant_id), c)
    return configs
