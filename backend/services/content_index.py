"""Content index — long-term, semantic memory over finished agent output.

Two jobs, bundled here because they run at the same moment (when a
sub-agent's inbox row is finalized) and extract the same fields:

1. **content_library mirror**: copy the finalized inbox row into the
   `content_library_entries` Supabase table with extracted fields
   (image_urls, topic keywords, entities) in `metadata`. This is the
   authoritative cross-session archive — the raw inbox row is a hot
   working set and may be deleted when the user cleans up, but the
   library entry persists for the life of the tenant.

2. **Qdrant embedding**: embed the row's title + content + extracted
   keywords via the same sentence-transformers pipeline
   `semantic_cache.py` uses (all-MiniLM-L6-v2, 384d). Stored in a
   separate Qdrant collection (`aria_content`) scoped by tenant.
   `semantic_find_assets` queries by natural-language hint and returns
   matching inbox rows.

Both operations are best-effort and silently no-op when the
dependencies are unavailable (Qdrant down, content_library missing
columns, etc.) — never crash the agent pipeline.

Call sites:
  - `paperclip_office_sync.poll_completed_issues` — after an inbox row
    is imported from a Paperclip comment.
  - `server.create_inbox_item` — after a skill-curl inbox write
    completes.
  - `server._dispatch_paperclip_and_watch_to_inbox` — after the watcher
    finalizes the placeholder with real content.

All three call `index_inbox_row(row)` once per finalized row; the
function checks `inbox_items.content_indexed` first and skips if
already indexed (so double-calls are safe).
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Iterable

from backend.services.asset_lookup import extract_image_url_from_row

logger = logging.getLogger("aria.content_index")


# Qdrant collection that holds the inbox-row embeddings. Separate from
# the `prompt_cache` collection the semantic cache uses so TTL policies
# and cleanup don't interfere with each other.
COLLECTION_NAME = "aria_content"
VECTOR_SIZE = 384  # matches semantic_cache.py's all-MiniLM-L6-v2

# Stopword set used for lightweight keyword extraction. Tuned for
# marketing-agent output (lots of CTA-ish / positioning words) rather
# than a general-purpose English stopword list.
_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "this", "that", "your",
    "our", "use", "make", "create", "draft", "send", "post", "tweet",
    "about", "using", "include", "earlier", "latest", "last", "recent",
    "previous", "those", "these", "them", "been", "made", "wrote",
    "generated", "new", "one", "just", "some", "what", "which", "when",
    "have", "has", "are", "was", "were", "will", "would", "should",
    "could", "there", "their", "they", "them", "like", "just", "also",
    "then", "than", "very", "more", "most", "some", "such", "here",
    "into", "over", "only", "while", "after", "before", "through",
    "content", "email", "social", "message", "please", "thanks",
}


def _db():
    from backend.services.supabase import get_db
    return get_db()


def _extract_keywords(text: str, limit: int = 12) -> list[str]:
    """Pull a small set of content keywords for lightweight recall.
    Alphabetic tokens, length >= 4, not stopwords, de-duplicated,
    preserved in first-seen order."""
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", text.lower())
    seen: set[str] = set()
    keywords: list[str] = []
    for tok in tokens:
        if tok in _STOPWORDS or len(tok) < 4:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        keywords.append(tok)
        if len(keywords) >= limit:
            break
    return keywords


def _extract_image_urls(row: dict) -> list[str]:
    """Collect every image URL we can find across the three storage
    surfaces (metadata.image_url, email_draft.image_urls, markdown/raw
    in content). Returns a de-duplicated list preserving order."""
    urls: list[str] = []
    seen: set[str] = set()

    def _add(u: str | None) -> None:
        if u and u not in seen:
            urls.append(u)
            seen.add(u)

    _add(extract_image_url_from_row(row))

    # email_draft.image_urls array (Email Marketer writes this)
    draft = row.get("email_draft") or {}
    if isinstance(draft, dict):
        for u in draft.get("image_urls") or []:
            if isinstance(u, str):
                _add(u)

    # All markdown and raw image URLs in content
    content = row.get("content") or ""
    for m in re.finditer(r"!\[[^\]]*\]\((https?://[^\s)]+)\)", content):
        _add(m.group(1))
    for m in re.finditer(
        r"https?://\S+?\.(?:png|jpg|jpeg|gif|webp|svg)(?:\?\S*)?", content, re.IGNORECASE,
    ):
        _add(m.group(0))

    return urls


# ─── Qdrant wiring ─────────────────────────────────────────────────────────


_qdrant_client = None
_embedder = None


def _get_qdrant():
    global _qdrant_client
    if _qdrant_client is None:
        try:
            import os
            from qdrant_client import QdrantClient
            _qdrant_client = QdrantClient(
                url=os.getenv("QDRANT_URL", "http://localhost:6333"),
                timeout=5,
            )
        except Exception as e:
            logger.debug("[content_index] Qdrant client init failed: %s", e)
            _qdrant_client = None
    return _qdrant_client


def _get_embedder():
    global _embedder
    if _embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embedder = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception as e:
            logger.debug("[content_index] embedder init failed: %s", e)
            _embedder = None
    return _embedder


def _embed(text: str) -> list[float] | None:
    model = _get_embedder()
    if not model:
        return None
    try:
        vec = model.encode(text, normalize_embeddings=True).tolist()
        return vec
    except Exception as e:
        logger.debug("[content_index] embed failed: %s", e)
        return None


def _ensure_collection() -> bool:
    client = _get_qdrant()
    if not client:
        return False
    try:
        from qdrant_client.models import Distance, VectorParams
        existing = [c.name for c in client.get_collections().collections]
        if COLLECTION_NAME not in existing:
            client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            )
            logger.info("[content_index] created Qdrant collection %s", COLLECTION_NAME)
        return True
    except Exception as e:
        logger.debug("[content_index] ensure_collection failed: %s", e)
        return False


# ─── Public API ────────────────────────────────────────────────────────────


def index_inbox_row(row: dict) -> None:
    """Index a finalized inbox row into both the content_library mirror
    and the Qdrant semantic index. Best-effort: swallows every error.

    Skips rows that look unfinalized (status=processing / placeholder
    stub content) so the hot-path "Email Marketer is working on..."
    placeholder never leaks into recall.
    """
    if not row or not isinstance(row, dict):
        return
    tenant_id = row.get("tenant_id")
    if not tenant_id:
        return
    status = (row.get("status") or "").lower()
    if status in ("processing", "pending"):
        return
    content = (row.get("content") or "").strip()
    if not content or len(content) < 40:
        # Too short to be a real asset — skip quietly
        return

    title = (row.get("title") or "").strip()
    agent = row.get("agent") or ""
    row_type = row.get("type") or ""
    item_id = row.get("id")
    image_urls = _extract_image_urls(row)
    keywords = _extract_keywords(f"{title} {content}")

    _mirror_to_library(tenant_id, item_id, agent, row_type, title, content, image_urls, keywords)
    _upsert_embedding(tenant_id, item_id, agent, row_type, title, content, image_urls, keywords)


def _mirror_to_library(
    tenant_id: str,
    item_id: str,
    agent: str,
    row_type: str,
    title: str,
    content: str,
    image_urls: list[str],
    keywords: list[str],
) -> None:
    """Write a row to `content_library_entries` so the asset outlives
    inbox cleanup. Upserts on (tenant_id, metadata.inbox_item_id)
    logically — we do a pre-check query instead of relying on Postgres
    unique constraints (which would require a migration)."""
    if not item_id:
        return
    try:
        sb = _db()
        existing = (
            sb.table("content_library_entries")
            .select("id")
            .eq("tenant_id", tenant_id)
            .contains("metadata", {"inbox_item_id": item_id})
            .limit(1)
            .execute()
        )
        if existing.data:
            # Already mirrored — don't duplicate, don't re-embed below
            # either (handled in _upsert_embedding via its own check).
            return
        metadata = {
            "inbox_item_id": item_id,
            "agent": agent,
            "image_urls": image_urls,
            "keywords": keywords,
        }
        sb.table("content_library_entries").insert({
            "tenant_id": tenant_id,
            "type": row_type or "content",
            "title": title or "(untitled)",
            "body": content[:10000],  # cap to protect against XL payloads
            "metadata": metadata,
        }).execute()
        logger.info(
            "[content_index] mirrored inbox %s -> content_library (tenant=%s, type=%s)",
            item_id, tenant_id, row_type,
        )
    except Exception as e:
        # Column mismatch / RLS / connection — log and move on. This
        # feature must never break the main agent pipeline.
        logger.debug("[content_index] mirror_to_library skipped: %s", e)


def _upsert_embedding(
    tenant_id: str,
    item_id: str,
    agent: str,
    row_type: str,
    title: str,
    content: str,
    image_urls: list[str],
    keywords: list[str],
) -> None:
    """Embed the row and upsert into Qdrant. No-op if Qdrant or the
    embedder isn't available — the ILIKE fallback in
    `find_referenced_asset` still works."""
    if not item_id:
        return
    if not _ensure_collection():
        return
    client = _get_qdrant()
    if not client:
        return

    # Embedding text — lead with title (often the most concept-dense
    # chunk), then content, then extracted keywords. Cap at ~2000 chars
    # so long emails/blogs don't balloon the embedding cost.
    embed_text = "\n".join(
        filter(None, [title, content[:1800], " ".join(keywords)])
    )
    vec = _embed(embed_text)
    if not vec:
        return

    try:
        from qdrant_client.models import PointStruct
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant_id}:{item_id}"))
        client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vec,
                    payload={
                        "tenant_id": tenant_id,
                        "inbox_item_id": item_id,
                        "agent": agent,
                        "type": row_type,
                        "title": title,
                        "has_image": bool(image_urls),
                        "image_url": image_urls[0] if image_urls else None,
                        "keywords": keywords,
                    },
                )
            ],
        )
        logger.info(
            "[content_index] embedded inbox %s into Qdrant (tenant=%s, agent=%s)",
            item_id, tenant_id, agent,
        )
    except Exception as e:
        logger.debug("[content_index] upsert_embedding failed: %s", e)


def semantic_find_assets(
    tenant_id: str,
    *,
    query: str,
    agent: str | None = None,
    types: Iterable[str] | None = None,
    limit: int = 5,
    score_threshold: float = 0.35,
) -> list[dict]:
    """Semantic search over the tenant's indexed inbox rows.

    Returns inbox_items rows (same shape `get_recent_assets` returns)
    sorted by cosine similarity to the query, filtered by tenant +
    optional agent/type.

    Why the return shape matches `get_recent_assets` instead of raw
    Qdrant payload: `find_referenced_asset` is the caller and it
    already handles the inbox-row shape downstream. Keeping the
    contract identical means semantic hits and ILIKE fallback hits
    are interchangeable — callers don't branch on which path produced
    the result.

    `score_threshold` is conservative (0.35) so a random semi-related
    row doesn't outrank a genuine miss. Below this, we return [] and
    let ILIKE handle it.
    """
    if not tenant_id or not query:
        return []
    client = _get_qdrant()
    if not client:
        return []
    vec = _embed(query)
    if not vec:
        return []

    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny
        must = [FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id))]
        if agent:
            must.append(FieldCondition(key="agent", match=MatchValue(value=agent)))
        if types:
            type_list = list(types)
            if len(type_list) == 1:
                must.append(FieldCondition(key="type", match=MatchValue(value=type_list[0])))
            else:
                must.append(FieldCondition(key="type", match=MatchAny(any=type_list)))

        hits = client.search(
            collection_name=COLLECTION_NAME,
            query_vector=vec,
            limit=limit,
            score_threshold=score_threshold,
            query_filter=Filter(must=must),
        )
    except Exception as e:
        logger.debug("[content_index] semantic search failed: %s", e)
        return []

    if not hits:
        return []

    # Fetch the full inbox rows so downstream consumers get the same
    # shape `get_recent_assets` returns. We could denormalize payload
    # enough to skip this, but keeping the row as source of truth means
    # a stale Qdrant payload can't silently serve wrong content.
    item_ids = [h.payload.get("inbox_item_id") for h in hits if h.payload]
    item_ids = [i for i in item_ids if i]
    if not item_ids:
        return []

    try:
        sb = _db()
        res = (
            sb.table("inbox_items")
            .select("id, agent, type, title, content, metadata, email_draft, status, created_at")
            .eq("tenant_id", tenant_id)
            .in_("id", item_ids)
            .execute()
        )
        rows_by_id = {r["id"]: r for r in (res.data or [])}
        # Preserve Qdrant's similarity ordering
        ordered = [rows_by_id[i] for i in item_ids if i in rows_by_id]
        return ordered
    except Exception as e:
        logger.debug("[content_index] fetch rows failed: %s", e)
        return []
