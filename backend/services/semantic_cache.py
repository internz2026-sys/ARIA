"""Semantic cache using Qdrant — returns cached responses for similar prompts.

Uses sentence-transformers to embed prompts, stores them in Qdrant with the
Claude response. On cache hit (cosine similarity >= threshold), returns the
cached response instead of calling Claude CLI.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue

logger = logging.getLogger("aria.semantic_cache")

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION_NAME = "prompt_cache"
SIMILARITY_THRESHOLD = float(os.getenv("ARIA_CACHE_THRESHOLD", "0.92"))
CACHE_TTL_HOURS = int(os.getenv("ARIA_CACHE_TTL_HOURS", "24"))
EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # 384 dimensions, fast
VECTOR_SIZE = 384

_client: QdrantClient | None = None
_embedder = None


def _get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=QDRANT_URL, timeout=5)
    return _client


def _get_embedder():
    """Lazy-load the SentenceTransformer model. Returns None when the
    `sentence_transformers` package isn't installed (production opted
    out of the heavy torch dep on 2026-05-07 to slim the backend image
    from ~9.6GB to ~800MB). Callers handle None as "skip cache" — every
    call becomes a cache miss + a normal Claude call. Mirrors the
    optional-embedder pattern already used by services/content_index.py.
    """
    global _embedder
    if _embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embedder = SentenceTransformer(EMBEDDING_MODEL)
            logger.info("Loaded embedding model: %s", EMBEDDING_MODEL)
        except Exception as e:
            logger.debug("[semantic_cache] embedder init skipped: %s", e)
            _embedder = None
    return _embedder


def _embed(text: str) -> list[float] | None:
    """Embed a text string into a vector. Returns None when the
    embedder isn't available (sentence_transformers not installed)."""
    model = _get_embedder()
    if not model:
        return None
    try:
        return model.encode(text, normalize_embeddings=True).tolist()
    except Exception as e:
        logger.debug("[semantic_cache] embed failed: %s", e)
        return None


def _prompt_key(system_prompt: str, user_message: str, model: str) -> str:
    """Create a combined text for embedding from the prompt components.

    Includes a hash of the FULL system prompt (not a truncation) so two
    requests with different system prompts never collide. Previously this
    truncated to 200 chars, which made every CEO chat call share the same
    cache key prefix and caused unrelated user messages to return identical
    cached responses.
    """
    system_hash = hashlib.md5(system_prompt.encode("utf-8")).hexdigest()[:12]
    return f"[model:{model}] [system_hash:{system_hash}] {user_message}"


def ensure_collection():
    """Create the Qdrant collection if it doesn't exist."""
    try:
        client = _get_client()
        collections = [c.name for c in client.get_collections().collections]
        if COLLECTION_NAME not in collections:
            client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            )
            logger.info("Created Qdrant collection: %s", COLLECTION_NAME)
    except Exception as e:
        logger.warning("Failed to ensure Qdrant collection: %s", e)


def search_cache(system_prompt: str, user_message: str, model: str, agent_id: str = "") -> str | None:
    """Search for a semantically similar cached prompt. Returns cached response or None."""
    try:
        query_text = _prompt_key(system_prompt, user_message, model)
        vector = _embed(query_text)
        if vector is None:
            # Embedder unavailable → cache lookup impossible. Fall
            # through to a normal Claude call by returning None.
            return None
        client = _get_client()

        # Filter by agent_id if provided
        query_filter = None
        if agent_id:
            query_filter = Filter(must=[
                FieldCondition(key="agent_id", match=MatchValue(value=agent_id))
            ])

        results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=vector,
            query_filter=query_filter,
            limit=1,
        )

        if results.points:
            point = results.points[0]
            score = point.score
            cached_at = point.payload.get("cached_at", 0)
            age_hours = (time.time() - cached_at) / 3600

            if score >= SIMILARITY_THRESHOLD and age_hours < CACHE_TTL_HOURS:
                logger.info(
                    "Cache HIT: score=%.3f, age=%.1fh, agent=%s",
                    score, age_hours, agent_id,
                )
                return point.payload.get("response")
            else:
                logger.debug("Cache MISS: score=%.3f (threshold=%.2f), age=%.1fh", score, SIMILARITY_THRESHOLD, age_hours)

    except Exception as e:
        logger.warning("Semantic cache search failed: %s", e)

    return None


def store_cache(system_prompt: str, user_message: str, model: str, response: str, agent_id: str = ""):
    """Store a prompt-response pair in the semantic cache."""
    try:
        query_text = _prompt_key(system_prompt, user_message, model)
        vector = _embed(query_text)
        if vector is None:
            # Embedder unavailable → can't index this response.
            # Skip the upsert; this becomes a no-op so callers don't
            # crash when sentence_transformers isn't installed.
            return
        client = _get_client()

        point_id = hashlib.md5(query_text.encode()).hexdigest()

        client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "system_prompt_preview": system_prompt[:200],
                        "user_message_preview": user_message[:500],
                        "response": response,
                        "model": model,
                        "agent_id": agent_id,
                        "cached_at": time.time(),
                    },
                )
            ],
        )
        logger.info("Cached response: agent=%s, model=%s", agent_id, model)

    except Exception as e:
        logger.warning("Failed to store in semantic cache: %s", e)
