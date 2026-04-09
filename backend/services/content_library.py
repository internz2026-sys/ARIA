"""Content Library Service — shared CRUD for the content_library table.

Agents should call create_entry() instead of writing rows directly. This
keeps the schema in one place and makes future changes (new columns,
storage migrations) a single edit instead of a hunt across agent files.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from backend.services.supabase import get_db

logger = logging.getLogger("aria.services.content_library")


def create_entry(
    tenant_id: str,
    *,
    type: str,
    title: str,
    body: str,
    metadata: dict | None = None,
    status: str = "completed",
) -> dict | None:
    """Insert a row into content_library and return the saved record.

    Args:
        tenant_id: tenant the content belongs to
        type: content type slug ("image", "blog_post", "email", etc.)
        title: short display title (truncated to 100 chars by callers if needed)
        body: full content body (or prompt for generated assets)
        metadata: optional JSON-serializable dict (image URLs, source provider,
                  context, anything else worth keeping next to the content)
        status: defaults to "completed" — set to "draft" or "failed" as needed

    Returns:
        The saved row dict, or None on failure (logged).
    """
    try:
        row = {
            "tenant_id": tenant_id,
            "type": type,
            "title": title,
            "body": body,
            "metadata": metadata or {},
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        result = get_db().table("content_library").insert(row).execute()
        saved = result.data[0] if result.data else None
        if saved:
            logger.info("Saved content_library entry: type=%s title=%s", type, title[:60])
        return saved
    except Exception as e:
        logger.error("Failed to save content_library entry (type=%s): %s", type, e)
        return None
