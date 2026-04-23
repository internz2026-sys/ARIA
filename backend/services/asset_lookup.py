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
    session_id: str | None = None,
) -> list[dict]:
    """Return recent inbox_items rows for the tenant, newest-first.

    All filters are optional. A common pattern is to scope by `agent` +
    `type` — e.g. `agents=["media"], types=["image"]` to find the
    latest Media Agent image.

    When `session_id` is provided, the query is first tried scoped to
    that chat session. If no rows match, falls back to the tenant-wide
    time-windowed query so cross-session recall still works. This keeps
    two parallel chats on the same tenant from cross-contaminating
    ("use yesterday's banner from the other conversation") while still
    matching the old behavior when session scoping yields nothing.
    """
    if not tenant_id:
        return []

    def _base_query():
        q = (
            _db()
            .table("inbox_items")
            .select("id, agent, type, title, content, metadata, email_draft, status, created_at, chat_session_id")
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
        return q

    try:
        if session_id:
            scoped = _base_query().eq("chat_session_id", session_id).execute()
            scoped_rows = list(scoped.data or [])
            if scoped_rows:
                return scoped_rows
        res = _base_query().execute()
        return list(res.data or [])
    except Exception as e:
        logger.warning("[asset_lookup] recent_assets failed tenant=%s: %s", tenant_id, e)
        return []


def get_latest_image_url(
    tenant_id: str,
    *,
    within_minutes: int = 30,
    session_id: str | None = None,
) -> str | None:
    """Latest Media Agent image URL, or None.

    Extracts from `metadata.image_url` first (canonical), falling back to
    parsing the `![alt](url)` markdown the media agent writes into the
    inbox body. Lookback default is 30 min — recent enough to still be
    the image the user meant, short enough to avoid cross-campaign leaks.

    When `session_id` is provided, same-session matches win; if none are
    found in-session, falls back to tenant+time scope so old-school
    one-shot delegations keep working.
    """
    rows = get_recent_assets(
        tenant_id,
        types=["image"],
        agents=["media"],
        within_minutes=within_minutes,
        limit=1,
        session_id=session_id,
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


# ─── Reference resolution (agent-to-agent & cross-turn recall) ─────────────


# Anaphora + reference phrases the CEO or user typically uses to point at
# prior agent output. Keep them tight: matching too loosely makes every
# "the email" in a task pull a random inbox row.
_REFERENCE_PATTERNS = [
    r"\bthe\s+(banner|image|picture|photo|graphic|visual|logo|"
    r"thumbnail|illustration|post|tweet|thread|email|newsletter|"
    r"blog(?:\s+post)?|article|ad|campaign|draft)\b",
    r"\bmy\s+(latest|last|recent|newest|previous)\b",
    r"\b(that|this)\s+(one|banner|image|post|email|blog|ad)\b",
    r"\b(the\s+one\s+(i|we|you)\s+(made|created|generated|wrote|drafted))\b",
    r"\b(earlier|from\s+(this\s+morning|yesterday|last\s+week|last\s+night|before))\b",
]
_REFERENCE_RE = re.compile("|".join(_REFERENCE_PATTERNS), re.IGNORECASE)


def task_has_reference(text: str) -> bool:
    """True if the task text contains phrases indicating the user is
    pointing at a prior agent asset (rather than asking for cold
    generation). Used by agents to decide when to invoke the fuzzy
    referenced-asset search as a fallback to time-windowed lookups."""
    if not text:
        return False
    return bool(_REFERENCE_RE.search(text))


def find_referenced_asset(
    tenant_id: str,
    *,
    text_hint: str = "",
    agent: str | None = None,
    types: Iterable[str] | None = None,
    limit: int = 5,
    within_days: int = 30,
) -> list[dict]:
    """Search the inbox for assets matching a natural-language reference.

    This is the "agents talk to each other" primitive: when a downstream
    agent sees "use the banner from earlier" but the tight 30-min / 6-h
    time-windowed helpers turn up nothing, it calls this with the task
    text as `text_hint` and gets back the most plausible rows.

    Strategy:
      1. If H (semantic search) is available, try Qdrant first for a
         concept match ("the professional-looking one", "that red
         banner"). Falls through to ILIKE on failure.
      2. ILIKE fallback on title + content against up to 4 keywords
         extracted from `text_hint` (stopwords filtered, len>=4).
      3. Newest-first within the lookback window (default 30 days to
         cover cross-session recall).
      4. Optional `agent` / `types` filters when the caller knows the
         shape of what they're looking for.

    Returns list of rows sorted by recency. Callers typically take [0]
    for "the most plausible match" or the full list when showing
    candidates to the user.
    """
    if not tenant_id:
        return []

    # Try semantic search first — handles concept-match queries that
    # ILIKE can't answer ("the professional one", "that red banner").
    # Silent no-op if Qdrant/embedder isn't ready.
    if text_hint:
        try:
            from backend.services.content_index import semantic_find_assets  # lazy
            semantic_hits = semantic_find_assets(
                tenant_id, query=text_hint, agent=agent, types=types, limit=limit,
            )
            if semantic_hits:
                return semantic_hits
        except Exception as e:
            logger.debug("[asset_lookup] semantic search skipped: %s", e)

    # ILIKE fallback
    stopwords = {
        "the", "and", "for", "with", "from", "into", "this", "that", "your",
        "our", "use", "make", "create", "draft", "send", "post", "tweet",
        "about", "using", "include", "earlier", "latest", "last", "recent",
        "previous", "those", "these", "them", "been", "made", "wrote",
        "generated", "new", "one", "just", "some", "what", "which", "when",
    }
    words = [w.strip(".,!?:;\"'").lower() for w in (text_hint or "").split()]
    keywords = [w for w in words if len(w) >= 4 and w not in stopwords][:4]

    try:
        cutoff = _cutoff_iso(within_days * 24 * 60)
        q = (
            _db()
            .table("inbox_items")
            .select("id, agent, type, title, content, metadata, email_draft, status, created_at")
            .eq("tenant_id", tenant_id)
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(limit * 3)  # over-fetch so ILIKE scoring has headroom
        )
        if agent:
            q = q.eq("agent", agent)
        if types:
            t = list(types)
            q = q.in_("type", t) if len(t) > 1 else q.eq("type", t[0])
        # Supabase/postgrest: chain .or_() for multi-column ILIKE match on
        # ANY keyword. Building the string by hand since the client doesn't
        # have a nicer helper.
        if keywords:
            or_parts = []
            for kw in keywords:
                esc = kw.replace(",", " ").replace("(", "").replace(")", "")
                or_parts.append(f"title.ilike.%{esc}%")
                or_parts.append(f"content.ilike.%{esc}%")
            q = q.or_(",".join(or_parts))
        rows = list((q.execute()).data or [])
        return rows[:limit]
    except Exception as e:
        logger.warning(
            "[asset_lookup] find_referenced_asset failed tenant=%s hint=%r: %s",
            tenant_id, (text_hint or "")[:80], e,
        )
        return []


def extract_image_url_from_row(row: dict | None) -> str | None:
    """Pull the best image URL out of an arbitrary inbox row.

    Checks metadata.image_url, email_draft.image_urls[0], and the
    markdown / raw URL in content — same three sources the frontend's
    getInboxThumbnail walks. Used by the delegation resolver when
    turning a `source_inbox_item_id` into a concrete URL the
    downstream agent can embed.
    """
    if not row:
        return None
    url = _coerce_metadata(row.get("metadata")).get("image_url")
    if url:
        return url
    draft = row.get("email_draft")
    if isinstance(draft, str):
        try:
            draft = json.loads(draft)
        except Exception:
            draft = None
    if isinstance(draft, dict):
        imgs = draft.get("image_urls")
        if isinstance(imgs, list) and imgs:
            return imgs[0]
    content = row.get("content") or ""
    m = _MD_IMG_RE.search(content)
    if m:
        return m.group(1)
    m2 = re.search(
        r"https?://\S+?\.(?:png|jpg|jpeg|gif|webp)(?:\?\S*)?",
        content, re.IGNORECASE,
    )
    return m2.group(0) if m2 else None


def get_inbox_row_by_id(tenant_id: str, item_id: str) -> dict | None:
    """Fetch a single inbox row scoped to the tenant. Returns None on
    miss / error so callers can cheaply `if row:` check and move on."""
    if not tenant_id or not item_id:
        return None
    try:
        res = (
            _db()
            .table("inbox_items")
            .select("id, agent, type, title, content, metadata, email_draft, status, created_at")
            .eq("tenant_id", tenant_id)
            .eq("id", item_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as e:
        logger.warning(
            "[asset_lookup] get_inbox_row_by_id failed tenant=%s id=%s: %s",
            tenant_id, item_id, e,
        )
        return None


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


def summarize_style_memory_for_prompt(
    tenant_id: str, *, agent: str, limit: int = 3,
) -> str:
    """Return a compact "user edits to emulate" block for BaseAgent.

    Pulls recent rows from `style_adjustments` (written by
    routers/inbox.py every time the user meaningfully edited a draft
    this agent produced). The block is truncated aggressively — we
    only need enough signal for the model to notice the direction of
    the edits, not verbatim replay.
    """
    if not tenant_id or not agent:
        return ""
    try:
        rows = (
            _db()
            .table("style_adjustments")
            .select("original_content, edited_content, diff_chars, created_at")
            .eq("tenant_id", tenant_id)
            .eq("agent", agent)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        ).data or []
    except Exception:
        return ""
    if not rows:
        return ""
    lines = [
        "## User style preferences (the tenant edited past drafts this way — emulate)",
    ]
    for r in rows:
        before = (r.get("original_content") or "")[:280].replace("\n", " ")
        after = (r.get("edited_content") or "")[:280].replace("\n", " ")
        lines.append(f"- BEFORE: {before}")
        lines.append(f"  AFTER:  {after}")
    return "\n".join(lines)


def summarize_cancel_reasons_for_prompt(
    tenant_id: str, *, agent: str, limit: int = 3,
) -> str:
    """Return a short block of recent cancellation reasons for the agent.

    Read from inbox_items where cancel_reason is set. Helps the model
    avoid the specific failure modes the user has flagged ("too salesy",
    "wrong recipient", "off brand voice"). Empty string when the column
    isn't present yet or the tenant has no cancelled rows.
    """
    if not tenant_id or not agent:
        return ""
    try:
        rows = (
            _db()
            .table("inbox_items")
            .select("title, cancel_reason, created_at")
            .eq("tenant_id", tenant_id)
            .eq("agent", agent)
            .not_.is_("cancel_reason", "null")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        ).data or []
    except Exception:
        return ""
    if not rows:
        return ""
    lines = ["## Recent user cancellation reasons (avoid these failure modes)"]
    for r in rows:
        title = (r.get("title") or "")[:80]
        reason = (r.get("cancel_reason") or "").strip()[:200]
        if reason:
            lines.append(f"- {title}: {reason}")
    return "\n".join(lines)
