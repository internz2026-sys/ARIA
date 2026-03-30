"""Tenant config loader — reads/writes tenant configurations from Supabase."""
from __future__ import annotations

import logging
import os
import re
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


def get_tenant_config(tenant_id: str | UUID) -> TenantConfig:
    sb = _get_supabase()
    result = sb.table("tenant_configs").select("*").eq("tenant_id", str(tenant_id)).single().execute()
    return TenantConfig(**result.data)


def save_tenant_config(config: TenantConfig) -> TenantConfig:
    sb = _get_supabase()
    data = config.model_dump(mode="json")
    data["tenant_id"] = str(config.tenant_id)

    # Retry loop: if Supabase rejects a column that doesn't exist in the
    # table yet, strip it and try again (up to 5 times).
    for _ in range(5):
        try:
            sb.table("tenant_configs").upsert(data).execute()
            return config
        except Exception as e:
            msg = str(e)
            # PostgREST PGRST204: column not found in schema cache
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
    sb.table("tenant_configs").update(updates).eq("tenant_id", str(tenant_id)).execute()
    return get_tenant_config(tenant_id)


def get_active_tenants() -> list[TenantConfig]:
    sb = _get_supabase()
    result = sb.table("tenant_configs").select("*").execute()
    return [TenantConfig(**row) for row in result.data]
