"""Social publishing routes — Twitter/X tweet+thread, LinkedIn org/post,
WhatsApp send/connect/disconnect, and the inbox-row "approve & publish"
helper. Plus the WhatsApp inbound webhook (GET verify + POST receive)
which is public (_PUBLIC_PATHS).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.auth import get_verified_tenant
from backend.config.loader import get_tenant_config, save_tenant_config
from backend.services.supabase import get_db as _get_supabase

logger = logging.getLogger("aria.server")

router = APIRouter()


# ── LinkedIn ─────────────────────────────────────────────────────────────
@router.get("/api/linkedin/{tenant_id}/organizations")
async def linkedin_organizations(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """List company pages the user is admin of."""
    from backend.tools import linkedin_tool

    config = get_tenant_config(tenant_id)
    access_token = config.integrations.linkedin_access_token
    if not access_token:
        raise HTTPException(status_code=400, detail="LinkedIn not connected.")

    orgs = await linkedin_tool.get_admin_organizations(access_token)
    return {
        "organizations": orgs,
        "current_org_urn": config.integrations.linkedin_org_urn or "",
    }


class LinkedInPostTargetRequest(BaseModel):
    org_urn: str = ""   # Empty string = post to personal profile
    org_name: str = ""


@router.post("/api/linkedin/{tenant_id}/set-target")
async def linkedin_set_target(
    tenant_id: str,
    body: LinkedInPostTargetRequest,
    _verified: dict = Depends(get_verified_tenant),
):
    """Set whether LinkedIn posts go to personal profile or a company page."""
    config = get_tenant_config(tenant_id)
    config.integrations.linkedin_org_urn = body.org_urn or None
    config.integrations.linkedin_org_name = body.org_name or None
    save_tenant_config(config)

    target = "company" if body.org_urn else "personal"
    logger.info("LinkedIn post target set to %s for tenant %s", target, tenant_id)
    return {"status": "updated", "posting_to": target, "org_name": body.org_name}


@router.post("/api/linkedin/{tenant_id}/post")
async def publish_linkedin_post(
    tenant_id: str,
    body: dict,
    _verified: dict = Depends(get_verified_tenant),
):
    """Publish a post to LinkedIn from the tenant's connected account.

    Requires confirmed=true — human must explicitly approve before publishing.
    """
    # _require_confirmation + _sanitize_social_post_text live in server.py
    # (shared with inbox.py and other routers). Inline-import to avoid
    # the circular import at module-load time.
    from backend.server import _require_confirmation, _sanitize_social_post_text

    gate = _require_confirmation("publish_linkedin", body.get("confirmed", False),
                                "Are you sure you want to publish this post to LinkedIn? This action is public and cannot be undone.")
    if gate:
        return gate

    from backend.tools import linkedin_tool

    config = get_tenant_config(tenant_id)
    access_token = config.integrations.linkedin_access_token

    if not access_token:
        raise HTTPException(status_code=400, detail="LinkedIn not connected. Go to Settings > Integrations.")

    # Use company page URN if set, otherwise personal profile
    author_urn = config.integrations.linkedin_org_urn or config.integrations.linkedin_member_urn
    if not author_urn:
        raise HTTPException(status_code=400, detail="LinkedIn not connected. Go to Settings > Integrations.")

    text = body.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="Post text is required")
    # Sanitize agent meta-commentary so lines like "LinkedIn post for X
    # created and saved to ARIA inbox (item <uuid>). Status: needs_review"
    # never get published to the actual feed. Refuse if nothing
    # substantive remains.
    text = _sanitize_social_post_text(text)
    if not text or len(text) < 20:
        raise HTTPException(
            status_code=400,
            detail=(
                "Post text looks like metadata or agent confirmation, not a "
                "real post. Ask the CEO to regenerate the post, then publish."
            ),
        )

    # Optional image attachment — when the post row has a resolved
    # image_url (from Media Designer pipeline), upload it through
    # LinkedIn's 3-step asset flow so the post renders as an image card
    # instead of text-only. linkedin_tool.create_post falls back to
    # text-only if any step of the upload fails.
    image_url = (body.get("image_url") or "").strip() or None
    result = await linkedin_tool.create_post(
        access_token, author_urn, text, image_url=image_url,
    )

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return result


# ── Twitter ──────────────────────────────────────────────────────────────
class TweetRequest(BaseModel):
    text: str
    reply_to: Optional[str] = None


class ThreadRequest(BaseModel):
    tweets: list[str]


@router.post("/api/twitter/{tenant_id}/tweet")
async def publish_tweet(
    tenant_id: str,
    body: TweetRequest,
    confirmed: bool = False,
    _verified: dict = Depends(get_verified_tenant),
):
    """Post a single tweet from the tenant's connected X account.

    Requires confirmed=true — human must explicitly approve before posting.
    """
    from backend.server import _require_confirmation

    gate = _require_confirmation("publish_twitter", confirmed,
                                f"Publish this tweet to X? This will be visible publicly.\n\n\"{body.text[:100]}{'...' if len(body.text) > 100 else ''}\"")
    if gate:
        return gate

    from backend.tools import twitter_tool
    config = get_tenant_config(tenant_id)
    access_token = config.integrations.twitter_access_token
    refresh_token = config.integrations.twitter_refresh_token

    if not access_token and not refresh_token:
        raise HTTPException(status_code=400, detail="Twitter not connected. Go to Settings > Integrations.")

    # Refresh if needed
    if not access_token and refresh_token:
        try:
            tokens = await twitter_tool.refresh_access_token(refresh_token)
            access_token = tokens["access_token"]
            config.integrations.twitter_access_token = access_token
            config.integrations.twitter_refresh_token = tokens.get("refresh_token", refresh_token)
            save_tenant_config(config)
        except Exception:
            raise HTTPException(status_code=400, detail="Twitter token expired. Reconnect in Settings.")

    result = await twitter_tool.post_tweet(access_token, body.text, reply_to=body.reply_to)

    if result.get("error") == "token_expired" and refresh_token:
        # Try refresh once
        try:
            tokens = await twitter_tool.refresh_access_token(refresh_token)
            access_token = tokens["access_token"]
            config.integrations.twitter_access_token = access_token
            config.integrations.twitter_refresh_token = tokens.get("refresh_token", refresh_token)
            save_tenant_config(config)
            result = await twitter_tool.post_tweet(access_token, body.text, reply_to=body.reply_to)
        except Exception:
            raise HTTPException(status_code=400, detail="Twitter token expired. Reconnect in Settings.")

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.post("/api/twitter/{tenant_id}/thread")
async def publish_thread(
    tenant_id: str,
    body: ThreadRequest,
    confirmed: bool = False,
    _verified: dict = Depends(get_verified_tenant),
):
    """Post a thread (multiple tweets) from the tenant's connected X account.

    Requires confirmed=true — human must explicitly approve before posting.
    """
    from backend.server import _require_confirmation

    gate = _require_confirmation("publish_twitter", confirmed,
                                f"Publish a {len(body.tweets)}-tweet thread to X? This will be visible publicly.")
    if gate:
        return gate

    from backend.tools import twitter_tool
    config = get_tenant_config(tenant_id)
    access_token = config.integrations.twitter_access_token

    if not access_token:
        raise HTTPException(status_code=400, detail="Twitter not connected.")

    results = await twitter_tool.post_thread(access_token, body.tweets)
    return {"tweets": results}


# ── Social Post Approval & Publish ───────────────────────────────────────
class SocialApproveRequest(BaseModel):
    inbox_item_id: str


@router.post("/api/social/{tenant_id}/approve-publish")
async def approve_and_publish_social(
    tenant_id: str,
    body: SocialApproveRequest,
    _verified: dict = Depends(get_verified_tenant),
):
    """Approve a social post from inbox and publish to connected platforms (Twitter/X)."""
    from backend.server import _sanitize_social_post_text
    from backend.tools import twitter_tool

    sb = _get_supabase()

    # Fetch inbox item
    item_result = sb.table("inbox_items").select("*").eq("id", body.inbox_item_id).single().execute()
    item = item_result.data
    if not item:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    if item.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    if item.get("type") != "social_post":
        raise HTTPException(status_code=400, detail="Item is not a social post")

    content = item.get("content", "")

    # Try to parse structured posts from content
    posts = []
    try:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            import json as _json
            data = _json.loads(content[start:end])
            posts = data.get("posts", [])
    except Exception:
        pass
    if not posts:
        try:
            start = content.find("[")
            end = content.rfind("]") + 1
            if start >= 0 and end > start:
                import json as _json
                posts = _json.loads(content[start:end])
        except Exception:
            pass

    # Fallback: treat entire content as a single tweet. Sanitize
    # agent meta-commentary first so things like "Tweet for SMAPS-SIS
    # created and saved to ARIA inbox (item <uuid>). **Post summary:**
    # ..." never get published as the actual post.
    if not posts:
        clean = _sanitize_social_post_text(content)
        if not clean or len(clean) < 20:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No publishable post text found in this inbox row — only "
                    "an agent summary / confirmation message. Ask the CEO to "
                    "regenerate the post so the actual tweet lands here."
                ),
            )
        posts = [{"platform": "twitter", "text": clean[:280]}]

    # Get Twitter credentials
    config = get_tenant_config(tenant_id)
    access_token = config.integrations.twitter_access_token
    refresh_token = config.integrations.twitter_refresh_token

    if not access_token and not refresh_token:
        raise HTTPException(status_code=400, detail="Twitter not connected. Go to Settings > Integrations.")

    # Refresh if needed
    if not access_token and refresh_token:
        try:
            tokens = await twitter_tool.refresh_access_token(refresh_token)
            access_token = tokens["access_token"]
            config.integrations.twitter_access_token = access_token
            config.integrations.twitter_refresh_token = tokens.get("refresh_token", refresh_token)
            save_tenant_config(config)
        except Exception:
            raise HTTPException(status_code=400, detail="Twitter token expired. Reconnect in Settings.")

    # Mark as sending
    sb.table("inbox_items").update({
        "status": "sending",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).execute()

    results = []
    for post in posts:
        platform = post.get("platform", "twitter").lower()
        text = post.get("text", "")
        hashtags = post.get("hashtags", [])
        if not text:
            continue

        if hashtags:
            tag_str = " ".join(f"#{t.strip('#')}" for t in hashtags)
            if tag_str not in text:
                text = f"{text}\n\n{tag_str}"

        if platform == "twitter":
            tweet_text = text[:280]
            result = await twitter_tool.post_tweet(access_token, tweet_text)
            # Retry with refresh if expired
            if result.get("error") == "token_expired" and refresh_token:
                try:
                    tokens = await twitter_tool.refresh_access_token(refresh_token)
                    access_token = tokens["access_token"]
                    config.integrations.twitter_access_token = access_token
                    config.integrations.twitter_refresh_token = tokens.get("refresh_token", refresh_token)
                    save_tenant_config(config)
                    result = await twitter_tool.post_tweet(access_token, tweet_text)
                except Exception:
                    result = {"error": "token_refresh_failed"}
            results.append({"platform": "twitter", **result})
        elif platform == "linkedin":
            from backend.tools import linkedin_tool
            li_token = config.integrations.linkedin_access_token
            li_urn = config.integrations.linkedin_member_urn
            if not li_token or not li_urn:
                results.append({"platform": "linkedin", "status": "skipped", "reason": "not_connected"})
            else:
                result = await linkedin_tool.create_post(li_token, li_urn, text[:3000])
                results.append({"platform": "linkedin", **result})
        else:
            results.append({"platform": platform, "status": "skipped", "reason": "not_integrated_yet"})

    # Update inbox item status
    any_success = any(r.get("tweet_id") or r.get("post_id") for r in results)
    new_status = "sent" if any_success else "failed"
    sb.table("inbox_items").update({
        "status": new_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).execute()

    # If all failed, return error detail so the user sees why
    if not any_success and results:
        errors = [r.get("error", "unknown") for r in results if r.get("error")]
        error_msg = "; ".join(errors) if errors else "No posts were published"
        logger.error("Social publish failed for tenant %s: %s", tenant_id, error_msg)
        from backend.services.safe_errors import safe_detail
        raise HTTPException(status_code=400, detail=safe_detail(error_msg, "Publish failed"))

    return {"status": new_status, "results": results}


# ── WhatsApp Cloud API ───────────────────────────────────────────────────
@router.get("/api/whatsapp/webhook")
async def whatsapp_webhook_verify(request: Request):
    """Meta webhook verification (GET) — responds to the hub.challenge."""
    from backend.tools.whatsapp_tool import WHATSAPP_VERIFY_TOKEN
    params = request.query_params
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        logger.info("WhatsApp webhook verified")
        return int(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/api/whatsapp/webhook")
async def whatsapp_webhook_receive(request: Request):
    """Receive incoming WhatsApp messages and status updates."""
    body = await request.json()

    # Process each entry
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            statuses = value.get("statuses", [])

            # Handle incoming messages
            for msg in messages:
                from_number = msg.get("from", "")
                msg_type = msg.get("type", "")
                msg_id = msg.get("id", "")
                timestamp = msg.get("timestamp", "")

                text_body = ""
                if msg_type == "text":
                    text_body = msg.get("text", {}).get("body", "")

                logger.info("WhatsApp message from %s: %s", from_number, text_body[:100])

                # Store in inbox for tenant review
                try:
                    sb = _get_supabase()

                    # Find tenant by WhatsApp phone number ID
                    phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
                    tenant_id = await _resolve_whatsapp_tenant(phone_number_id)

                    if tenant_id:
                        sb.table("inbox_items").insert({
                            "id": str(uuid.uuid4()),
                            "tenant_id": tenant_id,
                            "type": "whatsapp_message",
                            "agent": "whatsapp",
                            "title": f"WhatsApp from {from_number}",
                            "content": text_body,
                            "status": "needs_review",
                            "metadata": {
                                "from_number": from_number,
                                "message_id": msg_id,
                                "message_type": msg_type,
                                "timestamp": timestamp,
                                "phone_number_id": phone_number_id,
                            },
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }).execute()
                        logger.info("Stored WhatsApp message in inbox for tenant %s", tenant_id)
                except Exception as e:
                    logger.warning("Failed to store WhatsApp message: %s", e)

            # Handle status updates (sent, delivered, read)
            for status in statuses:
                logger.info("WhatsApp status update: %s → %s",
                            status.get("id", ""), status.get("status", ""))

    return {"status": "ok"}


async def _resolve_whatsapp_tenant(phone_number_id: str) -> str | None:
    """Find the tenant that owns a given WhatsApp phone number ID."""
    if not phone_number_id:
        return None
    try:
        sb = _get_supabase()
        # Search tenant_configs for matching WhatsApp phone number ID
        result = sb.table("tenant_configs").select("tenant_id,config").execute()
        for row in (result.data or []):
            config = row.get("config", {})
            integrations = config.get("integrations", {})
            if integrations.get("whatsapp_phone_number_id") == phone_number_id:
                return row.get("tenant_id")
    except Exception as e:
        logger.warning("Failed to resolve WhatsApp tenant: %s", e)

    # Fallback: if only one tenant exists, use that
    try:
        result = sb.table("tenant_configs").select("tenant_id").limit(1).execute()
        if result.data:
            return result.data[0].get("tenant_id")
    except Exception:
        pass
    return None


class WhatsAppSendRequest(BaseModel):
    to: str
    message: str


@router.post("/api/whatsapp/{tenant_id}/send")
async def whatsapp_send_message(
    tenant_id: str,
    body: WhatsAppSendRequest,
    confirmed: bool = False,
    _verified: dict = Depends(get_verified_tenant),
):
    """Send a WhatsApp message from a tenant's connected number.

    Requires confirmed=true — human must explicitly approve before sending.
    """
    from backend.server import _require_confirmation

    gate = _require_confirmation("send_whatsapp", confirmed,
                                f"Send WhatsApp message to {body.to}? This cannot be undone.")
    if gate:
        return gate

    from backend.tools import whatsapp_tool

    config = get_tenant_config(tenant_id)
    token = config.integrations.whatsapp_access_token
    pid = config.integrations.whatsapp_phone_number_id

    if not token or not pid:
        raise HTTPException(status_code=400, detail="WhatsApp not connected. Add your credentials in Settings.")

    result = await whatsapp_tool.send_message(
        to=body.to,
        text=body.message,
        access_token=token,
        phone_number_id=pid,
    )

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return result


class WhatsAppConnectRequest(BaseModel):
    access_token: str
    phone_number_id: str
    business_account_id: str = ""


@router.post("/api/whatsapp/{tenant_id}/connect")
async def whatsapp_connect(
    tenant_id: str,
    body: WhatsAppConnectRequest,
    _verified: dict = Depends(get_verified_tenant),
):
    """Save WhatsApp credentials for a tenant and verify connectivity."""
    from backend.tools import whatsapp_tool

    # Test the connection by fetching business profile
    profile = await whatsapp_tool.get_business_profile(
        access_token=body.access_token,
        phone_number_id=body.phone_number_id,
    )
    if profile.get("error"):
        from backend.services.safe_errors import safe_detail
        raise HTTPException(status_code=400, detail=safe_detail(profile["error"], "Connection test failed"))

    # Save credentials to tenant config
    config = get_tenant_config(tenant_id)
    config.integrations.whatsapp_access_token = body.access_token
    config.integrations.whatsapp_phone_number_id = body.phone_number_id
    config.integrations.whatsapp_business_account_id = body.business_account_id
    save_tenant_config(config)

    return {"status": "connected", "profile": profile}


@router.post("/api/whatsapp/{tenant_id}/disconnect")
async def whatsapp_disconnect(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Remove WhatsApp credentials for a tenant."""
    config = get_tenant_config(tenant_id)
    config.integrations.whatsapp_access_token = None
    config.integrations.whatsapp_phone_number_id = None
    config.integrations.whatsapp_business_account_id = None
    save_tenant_config(config)
    return {"status": "disconnected"}
