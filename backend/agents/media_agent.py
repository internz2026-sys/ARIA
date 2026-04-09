"""Media Agent — generates marketing images.

Pipeline: CEO drafts a request → Claude (Haiku) refines it into an image
prompt → the prompt is sent to an image-generation provider → resulting
PNG is stored in Supabase and logged to the content library.

Provider selection (in order):
1. Pollinations AI — free, no auth required. Primary provider.
2. Gemini — only used as a fallback if Pollinations is unreachable
   AND GEMINI_API_KEY is set. Requires paid billing for image gen.
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

logger = logging.getLogger("aria.agents.media")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.0-flash-exp"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"

POLLINATIONS_URL = "https://image.pollinations.ai/prompt"
POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "flux")

_agent = None


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
        """Generate an image: CEO prompt -> refine via Claude -> send to Gemini."""
        config = get_tenant_config(tenant_id)
        ctx = context or {}

        # If CEO already provided a prompt, use it; otherwise build one
        raw_prompt = ctx.get("prompt", "")
        if not raw_prompt:
            raw_prompt = f"Marketing image for {config.business_name}"

        # Step 1: Refine the prompt via Claude (Haiku for speed)
        from backend.tools.claude_cli import call_claude

        system_prompt = self.build_system_prompt(config, "generate_image")
        refined_prompt = await call_claude(
            system_prompt,
            f"Create an image prompt for: {raw_prompt}",
            max_tokens=self.MAX_TOKENS,
            tenant_id=tenant_id,
            model=self.MODEL,
            agent_id=self.AGENT_NAME,
        )
        refined_prompt = refined_prompt.strip()
        logger.info("[media] Refined prompt: %s", refined_prompt[:100])

        # Step 2: Generate image — Pollinations AI is the primary provider
        # (free, no auth). Gemini is only used as a fallback if Pollinations
        # is unreachable AND a Gemini key is available.
        image_data = None
        provider_used = None

        image_data = await _generate_with_pollinations(refined_prompt)
        if image_data:
            provider_used = "pollinations"
        elif os.getenv("GEMINI_API_KEY"):
            logger.warning("[media] Pollinations failed, falling back to Gemini")
            image_data = await _generate_with_gemini(refined_prompt)
            if image_data:
                provider_used = "gemini"

        if not image_data:
            # Make the failure visible in the user's inbox so they know
            # generation was attempted but failed (instead of silent void).
            await _save_to_inbox(
                tenant_id=tenant_id,
                prompt=refined_prompt,
                original_request=raw_prompt,
                image_url=None,
                provider=None,
                error="Both Pollinations and Gemini failed (Pollinations upstream returned an HTML 502; Gemini either has no API key or also failed).",
            )
            return {
                "agent": self.AGENT_NAME,
                "tenant_id": tenant_id,
                "status": "failed",
                "result": "Image generation failed — both Pollinations and Gemini were unreachable",
                "prompt_used": refined_prompt,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # Step 3: Store in Supabase storage
        image_url = await _store_image(tenant_id, image_data)

        if not image_url:
            # Storage upload failed — most likely because the 'content'
            # bucket doesn't exist in Supabase. Surface this as a failure
            # so the user knows to create the bucket.
            await _save_to_inbox(
                tenant_id=tenant_id,
                prompt=refined_prompt,
                original_request=raw_prompt,
                image_url=None,
                provider=provider_used,
                error=(
                    "Image was generated successfully but Supabase storage upload failed. "
                    "Most likely cause: the 'content' storage bucket does not exist. "
                    "Create it in Supabase Dashboard -> Storage -> New bucket (name: content, public: ON)."
                ),
            )
            return {
                "agent": self.AGENT_NAME,
                "tenant_id": tenant_id,
                "status": "failed",
                "result": "Image generated but Supabase storage upload failed (bucket missing?)",
                "prompt_used": refined_prompt,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # Step 4: Log to content library
        await _log_to_content_library(tenant_id, refined_prompt, image_url, ctx, provider_used)

        # Step 5: Create an inbox item so the image actually shows up in
        # the user's Inbox page (the content library is separate).
        await _save_to_inbox(
            tenant_id=tenant_id,
            prompt=refined_prompt,
            original_request=raw_prompt,
            image_url=image_url,
            provider=provider_used,
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
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


async def _generate_with_gemini(prompt: str) -> bytes | None:
    """Call Gemini API to generate an image. Returns raw image bytes or None."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("GEMINI_API_KEY not set")
        return None

    url = f"{GEMINI_URL}/{GEMINI_MODEL}:generateContent?key={api_key}"

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
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

            data = resp.json()

            # Extract image from response
            candidates = data.get("candidates", [])
            for candidate in candidates:
                parts = candidate.get("content", {}).get("parts", [])
                for part in parts:
                    if "inlineData" in part:
                        b64_data = part["inlineData"].get("data", "")
                        if b64_data:
                            return base64.b64decode(b64_data)

            logger.warning("Gemini response contained no image data")
            return None

    except Exception as e:
        logger.error("Gemini API call failed: %s", e)
        return None


async def _generate_with_pollinations(prompt: str) -> bytes | None:
    """Call Pollinations AI to generate an image. Free, no auth required.

    Endpoint: GET https://image.pollinations.ai/prompt/{url-encoded prompt}
    Returns raw PNG bytes directly in the response body.
    """
    encoded = urllib.parse.quote(prompt, safe="")
    url = (
        f"{POLLINATIONS_URL}/{encoded}"
        f"?width=1024&height=1024&model={POLLINATIONS_MODEL}&nologo=true"
    )

    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(url)

            if resp.status_code != 200:
                # Detect Pollinations' upstream HTML error pages so the log
                # shows a clear "upstream is down" instead of a confusing
                # "Failed to parse URL from <html>...".
                body_preview = resp.text[:200]
                if "<html" in body_preview.lower() or "502 bad gateway" in body_preview.lower():
                    logger.error(
                        "Pollinations upstream returned HTML error page (status %d) — image service is down",
                        resp.status_code,
                    )
                else:
                    logger.error(
                        "Pollinations API error %d: %s", resp.status_code, body_preview
                    )
                return None

            content = resp.content
            if not content or len(content) < 1000:
                logger.warning("Pollinations returned suspiciously small response (%d bytes)", len(content))
                return None

            # Reject HTML payloads that came back with a 200 status (sometimes
            # Pollinations returns 200 with an HTML error page).
            if content[:100].lower().startswith(b"<!doctype") or content[:100].lower().startswith(b"<html"):
                logger.error("Pollinations returned an HTML page instead of an image — upstream issue")
                return None

            logger.info("[media] Pollinations returned %d bytes", len(content))
            return content

    except Exception as e:
        logger.error("Pollinations API call failed: %s", e)
        return None


async def _store_image(tenant_id: str, image_data: bytes) -> str | None:
    """Store image in Supabase storage and return public URL.

    Returns None if upload fails — callers must handle the None case.
    The previous fallback returned a truncated base64 fragment, which
    looked like a valid data URL but was actually broken markdown.
    """
    try:
        from backend.services.supabase import get_db
        sb = get_db()

        filename = f"media/{tenant_id}/{uuid.uuid4().hex}.png"

        sb.storage.from_("content").upload(
            filename,
            image_data,
            {"content-type": "image/png"},
        )

        # Get public URL
        url = sb.storage.from_("content").get_public_url(filename)
        logger.info("Stored image: %s", url)
        return url

    except Exception as e:
        msg = str(e).lower()
        if "bucket" in msg and ("not found" in msg or "does not exist" in msg):
            logger.error(
                "Supabase storage bucket 'content' does not exist. "
                "Create it in Supabase Dashboard -> Storage -> New bucket "
                "(name: content, public: ON)."
            )
        else:
            logger.error("Failed to store image in Supabase: %s", e)
        return None


async def _log_to_content_library(
    tenant_id: str,
    prompt: str,
    image_url: str,
    context: dict,
    provider: str | None = None,
):
    """Log the generated image to the content library."""
    try:
        from backend.services.supabase import get_db
        sb = get_db()

        sb.table("content_library").insert({
            "tenant_id": tenant_id,
            "type": "image",
            "title": prompt[:100],
            "body": prompt,
            "metadata": {
                "image_url": image_url,
                "source": provider or "pollinations",
                "context": context,
            },
            "status": "completed",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

    except Exception as e:
        logger.warning("Failed to log to content library: %s", e)


async def _save_to_inbox(
    tenant_id: str,
    prompt: str,
    original_request: str,
    image_url: str | None,
    provider: str | None = None,
    error: str | None = None,
):
    """Create an inbox_items row for the generated image (or failed attempt).

    The Inbox page reads from inbox_items, NOT content_library — so without
    this row the user has no way to see the image in the UI even if storage
    upload succeeded. Failure rows are also written so the user gets clear
    feedback when generation fails (rather than a silent void).
    """
    try:
        from backend.services.supabase import get_db
        sb = get_db()

        if image_url and not error:
            # Success path — render markdown that displays the image inline.
            title = original_request[:100] if original_request else prompt[:100]
            content = (
                f"![Generated image]({image_url})\n\n"
                f"**Prompt used:** {prompt}\n\n"
                f"**Provider:** {provider or 'pollinations'}"
            )
            inbox_status = "ready"
        else:
            # Failure path — make the error visible.
            title = f"Image generation failed: {(original_request or prompt)[:80]}"
            content = (
                f"**Image generation failed**\n\n"
                f"{error or 'Unknown error'}\n\n"
                f"**Prompt was:** {prompt}\n\n"
                f"**Original request:** {original_request}"
            )
            inbox_status = "needs_review"

        sb.table("inbox_items").insert({
            "tenant_id": tenant_id,
            "agent": "media",
            "type": "image",
            "title": title,
            "content": content,
            "status": inbox_status,
            "priority": "medium",
        }).execute()
        logger.info("[media] Saved inbox item: %s", title[:60])

    except Exception as e:
        logger.error("Failed to save image to inbox: %s", e)


def _get():
    global _agent
    if _agent is None:
        _agent = MediaAgent()
    return _agent


AGENT_NAME = MediaAgent.AGENT_NAME


async def run(tenant_id: str, context: dict | None = None) -> dict:
    return await _get().run(tenant_id, context)
