"""Media Agent — generates marketing images.

Pipeline: CEO drafts a request -> Claude (Haiku) refines it into an image
prompt -> the prompt is sent to an image-generation provider -> resulting
PNG is stored in Supabase Storage and indexed in the content library and
the user's inbox.

Provider order:
1. Pollinations AI — free, no auth required. Primary provider.
2. Gemini — fallback only when Pollinations is unreachable AND
   GEMINI_API_KEY is set. Requires paid billing for image gen.
"""
from __future__ import annotations

import base64
import logging
import os
import urllib.parse
import uuid
from datetime import datetime, timezone

import httpx

from backend.agents.base import BaseAgent, MODEL_HAIKU
from backend.config.loader import get_tenant_config
from backend.services import content_library as content_library_service
from backend.services import inbox as inbox_service
from backend.services.supabase import get_db

logger = logging.getLogger("aria.agents.media")

# ── Provider config ────────────────────────────────────────────────────────
GEMINI_MODEL = "gemini-2.0-flash-exp"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"

POLLINATIONS_URL = "https://image.pollinations.ai/prompt"
POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "flux")

STORAGE_BUCKET = "content"
STORAGE_PATH_TEMPLATE = "media/{tenant_id}/{uuid}.png"

# Image dimensions for Pollinations requests
POLLINATIONS_WIDTH = 1024
POLLINATIONS_HEIGHT = 1024

# Minimum bytes a real image response should have — anything smaller is
# almost certainly an error page mistakenly served as 200 OK.
MIN_VALID_IMAGE_BYTES = 1000


# ──────────────────────────────────────────────────────────────────────────
# Agent class
# ──────────────────────────────────────────────────────────────────────────


class MediaAgent(BaseAgent):
    AGENT_NAME = "media"
    CONTEXT_KEY = "action"
    DEFAULT_CONTEXT = "generate_image"
    MODEL = MODEL_HAIKU
    MAX_TOKENS = 500
    CONTEXT_FIELDS = {"business", "product", "audience", "voice"}

    def build_system_prompt(self, config, action: str) -> str:
        return f"""You are the Media Designer for {config.business_name}.

{self.business_context(config, self.CONTEXT_FIELDS)}

Your job: refine image generation prompts for marketing visuals.
Given a request, output ONLY a detailed image prompt (1-3 sentences).
Focus on: style, composition, colors, mood, brand alignment.
Do NOT include any explanation — just the image prompt."""

    def build_user_message(self, action: str, context: dict | None) -> str:
        prompt = (context or {}).get("prompt", "")
        if prompt:
            return f"Create an image prompt for: {prompt}"
        return f"Create a marketing image prompt. Action: {action}. Context: {context}"

    async def run(self, tenant_id: str, context: dict | None = None) -> dict:
        """Generate, store, and index a marketing image end-to-end."""
        config = get_tenant_config(tenant_id)
        ctx = context or {}
        raw_prompt = ctx.get("prompt") or f"Marketing image for {config.business_name}"

        # Phase 1 — refine the user's request into a detailed image prompt
        refined_prompt = await self._refine_prompt(tenant_id, raw_prompt)

        # Phase 2 — generate the image bytes via available providers
        image_data, provider_used = await self._generate_image(refined_prompt)
        if not image_data:
            return self._fail(
                tenant_id=tenant_id,
                refined_prompt=refined_prompt,
                raw_prompt=raw_prompt,
                provider=None,
                user_error=(
                    "Both Pollinations and Gemini failed (Pollinations upstream "
                    "returned an HTML 502; Gemini either has no API key or also failed)."
                ),
                short_result="Image generation failed — both Pollinations and Gemini were unreachable",
            )

        # Phase 3 — upload to Supabase Storage
        image_url = await _store_image(tenant_id, image_data)
        if not image_url:
            return self._fail(
                tenant_id=tenant_id,
                refined_prompt=refined_prompt,
                raw_prompt=raw_prompt,
                provider=provider_used,
                user_error=(
                    "Image was generated successfully but Supabase storage upload failed. "
                    "Most likely cause: the 'content' storage bucket does not exist. "
                    "Create it in Supabase Dashboard -> Storage -> New bucket "
                    "(name: content, public: ON)."
                ),
                short_result="Image generated but Supabase storage upload failed (bucket missing?)",
            )

        # Phase 4 — index the image in content_library and the user's inbox
        content_library_service.create_entry(
            tenant_id,
            type="image",
            title=refined_prompt[:100],
            body=refined_prompt,
            metadata={
                "image_url": image_url,
                "source": provider_used or "pollinations",
                "context": ctx,
            },
        )
        inbox_service.create_item(
            tenant_id=tenant_id,
            agent=self.AGENT_NAME,
            title=(raw_prompt or refined_prompt)[:100],
            content=_render_image_inbox_body(refined_prompt, image_url, provider_used),
            type="image",
            status="ready",
        )

        return {
            "agent": self.AGENT_NAME,
            "tenant_id": tenant_id,
            "status": "completed",
            "result": {
                "image_url": image_url,
                "prompt_used": refined_prompt,
                "original_request": raw_prompt,
                "provider": provider_used,
            },
            "timestamp": _now_iso(),
        }

    # ── Run-pipeline helpers ────────────────────────────────────────────────

    async def _refine_prompt(self, tenant_id: str, raw_prompt: str) -> str:
        """Use Claude (Haiku) to expand a short request into a detailed image prompt.

        Falls back to the raw prompt verbatim if Claude is unavailable
        (rate-limited, CLI timeout, etc.) -- the image generators can
        still produce something usable from the original request.
        """
        from backend.tools.claude_cli import call_claude

        try:
            config = get_tenant_config(tenant_id)
            system_prompt = self.build_system_prompt(config, "generate_image")
            refined = await call_claude(
                system_prompt,
                f"Create an image prompt for: {raw_prompt}",
                max_tokens=self.MAX_TOKENS,
                tenant_id=tenant_id,
                model=self.MODEL,
                agent_id=self.AGENT_NAME,
            )
            refined = (refined or "").strip()
            if not refined:
                logger.warning("[media] Claude returned empty prompt -- using raw")
                return raw_prompt
            logger.info("[media] Refined prompt: %s", refined[:100])
            return refined
        except Exception as e:
            logger.warning(
                "[media] _refine_prompt failed (%s: %s) -- falling back to raw prompt",
                type(e).__name__, e,
            )
            return raw_prompt

    async def _generate_image(self, refined_prompt: str) -> tuple[bytes | None, str | None]:
        """Try providers in order; return (bytes, provider_name) or (None, None)."""
        data = await _generate_with_pollinations(refined_prompt)
        if data:
            return data, "pollinations"

        if os.getenv("GEMINI_API_KEY"):
            logger.warning("[media] Pollinations failed, falling back to Gemini")
            data = await _generate_with_gemini(refined_prompt)
            if data:
                return data, "gemini"

        return None, None

    def _fail(
        self,
        *,
        tenant_id: str,
        refined_prompt: str,
        raw_prompt: str,
        provider: str | None,
        user_error: str,
        short_result: str,
    ) -> dict:
        """Surface a failure in the user's inbox and return a 'failed' run dict."""
        inbox_service.create_item(
            tenant_id=tenant_id,
            agent=self.AGENT_NAME,
            title=f"Image generation failed: {(raw_prompt or refined_prompt)[:80]}",
            content=_render_failure_inbox_body(refined_prompt, raw_prompt, user_error),
            type="image",
            status="needs_review",
        )
        return {
            "agent": self.AGENT_NAME,
            "tenant_id": tenant_id,
            "status": "failed",
            "result": short_result,
            "prompt_used": refined_prompt,
            "timestamp": _now_iso(),
        }


# ──────────────────────────────────────────────────────────────────────────
# Inbox body renderers
# ──────────────────────────────────────────────────────────────────────────


def _render_image_inbox_body(refined_prompt: str, image_url: str, provider: str | None) -> str:
    return (
        f"![Generated image]({image_url})\n\n"
        f"**Prompt used:** {refined_prompt}\n\n"
        f"**Provider:** {provider or 'pollinations'}"
    )


def _render_failure_inbox_body(refined_prompt: str, raw_prompt: str, error: str) -> str:
    return (
        f"**Image generation failed**\n\n"
        f"{error}\n\n"
        f"**Prompt was:** {refined_prompt}\n\n"
        f"**Original request:** {raw_prompt}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Image provider clients
# ──────────────────────────────────────────────────────────────────────────


async def _generate_with_gemini(prompt: str) -> bytes | None:
    """Call Gemini API to generate an image. Returns raw image bytes or None."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("GEMINI_API_KEY not set")
        return None

    url = f"{GEMINI_URL}/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["image", "text"],
            "responseMimeType": "image/png",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error("Gemini API error %d: %s", resp.status_code, resp.text[:200])
                return None
            return _extract_gemini_image(resp.json())
    except Exception as e:
        logger.error("Gemini API call failed: %s", e)
        return None


def _extract_gemini_image(data: dict) -> bytes | None:
    """Pull the inline image bytes out of Gemini's response payload."""
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or {}
            b64_data = inline.get("data", "")
            if b64_data:
                return base64.b64decode(b64_data)
    logger.warning("Gemini response contained no image data")
    return None


async def _generate_with_pollinations(prompt: str) -> bytes | None:
    """Call Pollinations AI to generate an image. Free, no auth required.

    Endpoint: GET https://image.pollinations.ai/prompt/{url-encoded prompt}
    Returns raw PNG bytes directly in the response body.
    """
    encoded = urllib.parse.quote(prompt, safe="")
    url = (
        f"{POLLINATIONS_URL}/{encoded}"
        f"?width={POLLINATIONS_WIDTH}&height={POLLINATIONS_HEIGHT}"
        f"&model={POLLINATIONS_MODEL}&nologo=true"
    )

    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                _log_pollinations_error(resp.status_code, resp.text[:200])
                return None

            content = resp.content
            if not _looks_like_image(content):
                return None

            logger.info("[media] Pollinations returned %d bytes", len(content))
            return content
    except Exception as e:
        logger.error("Pollinations API call failed: %s", e)
        return None


def _log_pollinations_error(status_code: int, body_preview: str) -> None:
    """Surface a clear message when Pollinations' upstream is down."""
    lower = body_preview.lower()
    if "<html" in lower or "502 bad gateway" in lower:
        logger.error(
            "Pollinations upstream returned HTML error page (status %d) — image service is down",
            status_code,
        )
    else:
        logger.error("Pollinations API error %d: %s", status_code, body_preview)


def _looks_like_image(content: bytes) -> bool:
    """Heuristic check that a response body is actually an image, not an HTML error page."""
    if not content or len(content) < MIN_VALID_IMAGE_BYTES:
        logger.warning("Pollinations returned suspiciously small response (%d bytes)", len(content))
        return False
    head = content[:100].lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html"):
        logger.error("Pollinations returned an HTML page instead of an image — upstream issue")
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────
# Storage
# ──────────────────────────────────────────────────────────────────────────


async def _store_image(tenant_id: str, image_data: bytes) -> str | None:
    """Upload image bytes to Supabase Storage and return the public URL.

    Returns None on failure (logged). The previous fallback returned a
    truncated base64 fragment that looked like a valid data URL but was
    actually broken markdown — we never want callers to render that.
    """
    filename = STORAGE_PATH_TEMPLATE.format(tenant_id=tenant_id, uuid=uuid.uuid4().hex)
    try:
        sb = get_db()
        sb.storage.from_(STORAGE_BUCKET).upload(
            filename,
            image_data,
            {"content-type": "image/png"},
        )
        url = sb.storage.from_(STORAGE_BUCKET).get_public_url(filename)
        logger.info("Stored image: %s", url)
        return url
    except Exception as e:
        _log_storage_error(e)
        return None


def _log_storage_error(exc: Exception) -> None:
    """Detect missing-bucket errors specifically and log a one-line fix instruction."""
    msg = str(exc).lower()
    if "bucket" in msg and ("not found" in msg or "does not exist" in msg):
        logger.error(
            "Supabase storage bucket %r does not exist. "
            "Create it in Supabase Dashboard -> Storage -> New bucket "
            "(name: %s, public: ON).",
            STORAGE_BUCKET,
            STORAGE_BUCKET,
        )
    else:
        logger.error("Failed to store image in Supabase: %s", exc)


# ──────────────────────────────────────────────────────────────────────────
# Module-level helpers + singleton
# ──────────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_agent: MediaAgent | None = None


def _get() -> MediaAgent:
    global _agent
    if _agent is None:
        _agent = MediaAgent()
    return _agent


AGENT_NAME = MediaAgent.AGENT_NAME


async def run(tenant_id: str, context: dict | None = None) -> dict:
    return await _get().run(tenant_id, context)
