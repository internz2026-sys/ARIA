"""Asset Lookup — the cross-agent primitive.

Every agent (email_marketer, social_manager, ad_strategist, content_writer,
media) should call into this module before it starts generating, so the
next output can build on what a teammate just produced instead of being
written in isolation.

This is the "shared whiteboard" pattern: the inbox and content_library
are already the places where agent outputs land. These helpers let any
agent READ them with tight, semantic queries — "latest image from Media
in the last 30 min", "most recent blog post with topic X", "top 3
email subject lines by open rate this quarter".

Keep everything here synchronous (wraps blocking supabase-py calls) and
best-effort: a lookup failure must never crash the calling agent — we
just return None / [] and the agent generates without the cross-reference.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

logger = logging.getLogger("aria.services.asset_lookup")

# Markdown image extractor — media_agent saves `![alt](url)` in the inbox
# row body, content library stores the URL in metadata separately.
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")


def _db():
    """Lazy supabase import so agents can be imported without a DB env."""
    from backend.services.supabase import get_db
    return get_db()


def _cutoff_iso(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _coerce_metadata(meta) -> dict:
    """Normalize a metadata column that may be JSON text or already a dict."""
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str):
        try:
            return json.loads(meta)
        except Exception:
            return {}
    return {}


# ─── Inbox queries ─────────────────────────────────────────────────────────


def get_recent_assets(
    tenant_id: str,
    *,
    types: Iterable[str] | None = None,
    agents: Iterable[str] | None = None,
    statuses: Iterable[str] | None = None,
    within_minutes: int = 60,
    limit: int = 5,
) -> list[dict]:
    """Return recent inbox_items rows for the tenant, newest-first.

    All filters are optional. A common pattern is to scope by `agent` +
    `type` — e.g. `agents=["media"], types=["image"]` to find the
    latest Media Agent image.
    """
    if not tenant_id:
        return []
    try:
        q = (
            _db()
            .table("inbox_items")
            .select("id, agent, type, title, content, metadata, email_draft, status, created_at")
            .eq("tenant_id", tenant_id)
            .gte("created_at", _cutoff_iso(within_minutes))
            .order("created_at", desc=True)
            .limit(limit)
        )
        if types:
            t = list(types)
            q = q.in_("type", t) if len(t) > 1 else q.eq("type", t[0])
        if agents:
            a = list(agents)
            q = q.in_("agent", a) if len(a) > 1 else q.eq("agent", a[0])
        if statuses:
            s = list(statuses)
            q = q.in_("status", s) if len(s) > 1 else q.eq("status", s[0])
        res = q.execute()
        return list(res.data or [])
    except Exception as e:
        logger.warning("[asset_lookup] recent_assets failed tenant=%s: %s", tenant_id, e)
        return []


def get_latest_image_url(tenant_id: str, *, within_minutes: int = 30) -> str | None:
    """Latest Media Agent image URL, or None.

    Extracts from `metadata.image_url` first (canonical), falling back to
    parsing the `![alt](url)` markdown the media agent writes into the
    inbox body. Lookback default is 30 min — recent enough to still be
    the image the user meant, short enough to avoid cross-campaign leaks.
    """
    rows = get_recent_assets(
        tenant_id,
        types=["image"],
        agents=["media"],
        within_minutes=within_minutes,
        limit=1,
    )
    if not rows:
        return None
    row = rows[0]
    url = _coerce_metadata(row.get("metadata")).get("image_url")
    if url:
        return url
    m = _MD_IMG_RE.search(row.get("content") or "")
    return m.group(1) if m else None


def get_recent_blog_post(tenant_id: str, *, within_minutes: int = 180) -> dict | None:
    """Latest long-form Content Writer output the tenant has produced.

    Returns the inbox row (title, content, metadata) so callers can
    pluck the headline, excerpt, or body to repurpose into emails /
    social posts. Content-writer types covered: blog_post, article,
    landing_page (all stored by content_writer_agent).
    """
    rows = get_recent_assets(
        tenant_id,
        types=["blog_post", "article", "landing_page", "general"],
        agents=["content_writer"],
        within_minutes=within_minutes,
        limit=1,
    )
    return rows[0] if rows else None


def get_recent_email_hook(tenant_id: str, *, within_minutes: int = 120) -> dict | None:
    """Latest email draft's subject + preview snippet.

    Used by the Social Manager to build teaser posts off a launch email
    the Email Marketer just produced. The returned dict is shaped
    {subject, preview_snippet, to, inbox_item_id} or None.
    """
    rows = get_recent_assets(
        tenant_id,
        types=["email_sequence"],
        agents=["email_marketer"],
        within_minutes=within_minutes,
        limit=1,
    )
    if not rows:
        return None
    draft = rows[0].get("email_draft") or {}
    if isinstance(draft, str):
        try:
            draft = json.loads(draft)
        except Exception:
            draft = {}
    return {
        "subject": draft.get("subject") or rows[0].get("title", ""),
        "preview_snippet": draft.get("preview_snippet") or rows[0].get("content", "")[:200],
        "to": draft.get("to", ""),
        "inbox_item_id": rows[0].get("id"),
    }


# ─── Content library (cross-session, older history) ────────────────────────


def get_related_content(
    tenant_id: str,
    *,
    types: Iterable[str] | None = None,
    topic_query: str = "",
    limit: int = 5,
) -> list[dict]:
    """Search the content_library for older, reusable assets.

    Unlike `get_recent_assets` (scoped to the last N minutes of inbox),
    this reaches into the archive — every agent output ever persisted.
    Useful for "don't regenerate, adapt": a repurposable blog post from
    three months ago is often better than a fresh one made cold.
    `topic_query` is a simple ILIKE on title for now; swap to vector
    search (Qdrant) when we wire that up.
    """
    if not tenant_id:
        return []
    try:
        q = (
            _db()
            .table("content_library_entries")
            .select("id, type, title, body, metadata, created_at")
            .eq("tenant_id", tenant_id)
            .order("created_at", desc=True)
            .limit(limit)
        )
        if types:
            t = list(types)
            q = q.in_("type", t) if len(t) > 1 else q.eq("type", t[0])
        if topic_query:
            # ILIKE on title. Keeps the lookup cheap even without an index.
            q = q.ilike("title", f"%{topic_query}%")
        res = q.execute()
        return list(res.data or [])
    except Exception as e:
        logger.warning(
            "[asset_lookup] related_content failed tenant=%s query=%s: %s",
            tenant_id, topic_query[:40], e,
        )
        return []


# ─── Analytics: top-performers feedback ────────────────────────────────────


def get_top_performers(
    tenant_id: str,
    *,
    agent: str,
    metric: str = "open_rate",
    limit: int = 3,
    within_days: int = 90,
) -> list[dict]:
    """Top N performing outputs for an agent by a given metric.

    Source of truth: `agent_logs` (if we record metrics there) or
    `performance_log` (user-reported). Returns rows shaped
    {title, metric_value, inbox_item_id, created_at} so callers can
    feed them into the next prompt as "reuse the structure of:".

    Currently a stub — until analytics are populated, we fall back to
    the 3 most recently APPROVED (status=sent / status=approved) rows
    for this agent, on the assumption that approval is a quality
    signal even when no metric is logged yet.
    """
    if not tenant_id or not agent:
        return []
    try:
        # TODO: swap to a true analytics table once metric ingestion is
        # live. For now, treat "approved + sent" as the quality signal.
        cutoff = _cutoff_iso(within_days * 24 * 60)
        res = (
            _db()
            .table("inbox_items")
            .select("id, title, content, email_draft, status, created_at")
            .eq("tenant_id", tenant_id)
            .eq("agent", agent)
            .in_("status", ["sent", "approved", "published"])
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(limit)
        ).execute()
        rows = list(res.data or [])
        for r in rows:
            r["metric_value"] = None  # populated once analytics ingestion lands
            r["metric"] = metric
        return rows
    except Exception as e:
        logger.warning(
            "[asset_lookup] top_performers failed tenant=%s agent=%s: %s",
            tenant_id, agent, e,
        )
        return []


# ─── Thin facades for agent prompts ────────────────────────────────────────


def summarize_top_performers_for_prompt(
    tenant_id: str, *, agent: str, metric: str = "open_rate", limit: int = 3,
) -> str:
    """One-shot helper that returns a short text block an agent's system
    prompt can interpolate. Empty string on no history — callers drop it
    into f-strings without branching.
    """
    rows = get_top_performers(tenant_id, agent=agent, metric=metric, limit=limit)
    if not rows:
        return ""
    lines = [f"## Recent top {agent} outputs (structure to emulate)"]
    for r in rows:
        title = (r.get("title") or "")[:80]
        preview = ""
        draft = r.get("email_draft") or {}
        if isinstance(draft, dict):
            preview = draft.get("preview_snippet") or ""
        if not preview:
            preview = (r.get("content") or "")[:160]
        lines.append(f"- {title} — {preview}")
    return "\n".join(lines)
