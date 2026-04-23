"""LinkedIn API tool — OAuth 2.0 + posting via Posts API.

Each ARIA tenant connects their own LinkedIn account via OAuth 2.0.
App credentials (LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET) are in .env.
Per-user tokens are stored in tenant_configs.integrations.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("aria.linkedin")

CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET", "")

SCOPES = "openid profile email w_member_social w_organization_social r_organization_social rw_organization_admin"

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
API_BASE = "https://api.linkedin.com/v2"


def get_auth_url(redirect_uri: str, state: str) -> str:
    """Generate LinkedIn OAuth 2.0 authorization URL."""
    if not CLIENT_ID:
        raise RuntimeError("LINKEDIN_CLIENT_ID not set")

    from urllib.parse import urlencode
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": SCOPES,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str, redirect_uri: str) -> dict:
    """Exchange authorization code for access token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            logger.error("LinkedIn token exchange failed: %s %s", resp.status_code, resp.text)
            raise RuntimeError(f"Token exchange failed: {resp.text}")

        data = resp.json()
        return {
            "access_token": data["access_token"],
            "expires_in": data.get("expires_in", 0),
        }


async def get_profile(access_token: str) -> dict:
    """Get the authenticated user's LinkedIn profile (name, sub/ID)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.linkedin.com/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code == 401:
            return {"error": "token_expired"}
        if resp.status_code != 200:
            return {"error": f"api_error ({resp.status_code})"}
        return resp.json()


async def get_admin_organizations(access_token: str) -> list[dict]:
    """Get organizations (company pages) where the user is an admin."""
    async with httpx.AsyncClient() as client:
        # Get organization access control — find orgs where user has ADMINISTRATOR role
        resp = await client.get(
            "https://api.linkedin.com/v2/organizationAcls",
            params={"q": "roleAssignee", "role": "ADMINISTRATOR", "projection": "(elements*(organization~(id,localizedName,vanityName)))"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-Restli-Protocol-Version": "2.0.0",
            },
        )

        logger.info("LinkedIn orgs response: %s %s", resp.status_code, resp.text[:500])

        if resp.status_code != 200:
            logger.warning("Failed to fetch LinkedIn organizations: %s", resp.text[:300])
            return []

        data = resp.json()
        orgs = []
        for el in data.get("elements", []):
            org_data = el.get("organization~", {})
            org_urn = el.get("organization", "")
            if org_data:
                orgs.append({
                    "id": org_data.get("id", ""),
                    "name": org_data.get("localizedName", ""),
                    "vanity_name": org_data.get("vanityName", ""),
                    "urn": org_urn,
                })
            elif org_urn:
                # Extract org ID from URN like "urn:li:organization:12345"
                org_id = org_urn.split(":")[-1] if ":" in org_urn else ""
                orgs.append({
                    "id": org_id,
                    "name": "",
                    "vanity_name": "",
                    "urn": org_urn,
                })
        return orgs


async def _register_image_upload(
    client: httpx.AsyncClient, access_token: str, owner_urn: str,
) -> tuple[str, str] | None:
    """Register a feedshare-image upload and return (upload_url, asset_urn).

    LinkedIn's ugcPosts API doesn't accept external image URLs directly —
    you have to register an asset against the owner, receive a pre-signed
    upload URL, PUT the bytes there, then reference the returned asset
    URN in the post's `media` array. Returns None on any failure so
    callers can fall back to a text-only post instead of 500'ing.
    """
    try:
        resp = await client.post(
            f"{API_BASE}/assets?action=registerUpload",
            json={
                "registerUploadRequest": {
                    "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                    "owner": owner_urn,
                    "serviceRelationships": [
                        {
                            "relationshipType": "OWNER",
                            "identifier": "urn:li:userGeneratedContent",
                        }
                    ],
                }
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
            },
        )
        if resp.status_code not in (200, 201):
            logger.warning(
                "LinkedIn registerUpload failed: %s %s",
                resp.status_code, resp.text[:300],
            )
            return None
        data = resp.json()
        value = data.get("value") or {}
        asset_urn = value.get("asset") or ""
        mech = (
            value.get("uploadMechanism", {})
            .get("com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest", {})
        )
        upload_url = mech.get("uploadUrl") or ""
        if not asset_urn or not upload_url:
            logger.warning("LinkedIn registerUpload missing fields: %s", data)
            return None
        return upload_url, asset_urn
    except Exception as e:
        logger.warning("LinkedIn registerUpload errored: %s", e)
        return None


async def _fetch_image_bytes(
    client: httpx.AsyncClient, image_url: str,
) -> tuple[bytes, str] | None:
    """Download an image URL and return (bytes, mime_type), or None."""
    try:
        resp = await client.get(image_url, timeout=30.0)
        if resp.status_code != 200:
            logger.warning(
                "Image download failed for LinkedIn publish: %s (%d)",
                image_url[:120], resp.status_code,
            )
            return None
        ctype = resp.headers.get("content-type", "image/png")
        return resp.content, ctype
    except Exception as e:
        logger.warning("Image download errored for LinkedIn publish: %s", e)
        return None


async def _upload_image_bytes(
    client: httpx.AsyncClient,
    access_token: str,
    upload_url: str,
    image_bytes: bytes,
    mime_type: str,
) -> bool:
    """PUT raw image bytes to the pre-signed LinkedIn upload URL."""
    try:
        resp = await client.put(
            upload_url,
            content=image_bytes,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": mime_type or "image/png",
            },
            timeout=60.0,
        )
        if resp.status_code not in (200, 201):
            logger.warning(
                "LinkedIn asset upload PUT failed: %s %s",
                resp.status_code, resp.text[:300],
            )
            return False
        return True
    except Exception as e:
        logger.warning("LinkedIn asset upload errored: %s", e)
        return False


async def create_post(
    access_token: str,
    author_urn: str,
    text: str,
    image_url: str | None = None,
) -> dict:
    """Create a LinkedIn post, optionally with an attached image.

    Args:
        access_token: OAuth access token
        author_urn: LinkedIn member or organization URN
        text: Post text content (up to 3000 chars)
        image_url: Optional fully-qualified URL of an image to embed.
            If provided, triggers the 3-step upload flow (register ->
            PUT bytes -> reference asset URN). On any failure step we
            log the warning and fall through to a text-only post so
            the user still sees their copy go live.
    """
    async with httpx.AsyncClient() as client:
        # Optional image handoff. None of these steps raise — they each
        # return a sentinel on failure so we can degrade gracefully.
        asset_urn: str | None = None
        if image_url:
            registered = await _register_image_upload(client, access_token, author_urn)
            if registered:
                upload_url, asset_urn_candidate = registered
                fetched = await _fetch_image_bytes(client, image_url)
                if fetched:
                    image_bytes, mime = fetched
                    ok = await _upload_image_bytes(
                        client, access_token, upload_url, image_bytes, mime,
                    )
                    if ok:
                        asset_urn = asset_urn_candidate
                        logger.info(
                            "LinkedIn image uploaded, asset=%s (src=%s)",
                            asset_urn, image_url[:120],
                        )
                    else:
                        logger.info("LinkedIn image upload failed — posting text-only")
                else:
                    logger.info(
                        "Couldn't fetch image bytes — posting text-only (src=%s)",
                        image_url[:120],
                    )
            else:
                logger.info("Couldn't register LinkedIn upload — posting text-only")

        share_content: dict = {
            "shareCommentary": {"text": text[:3000]},
            "shareMediaCategory": "IMAGE" if asset_urn else "NONE",
        }
        if asset_urn:
            share_content["media"] = [
                {
                    "status": "READY",
                    "description": {"text": ""},
                    "media": asset_urn,
                    "title": {"text": ""},
                }
            ]

        resp = await client.post(
            "https://api.linkedin.com/v2/ugcPosts",
            json={
                "author": author_urn,
                "lifecycleState": "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": share_content,
                },
                "visibility": {
                    "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC",
                },
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
            },
        )

        logger.info("LinkedIn post response: %s %s", resp.status_code, resp.text[:500])

        if resp.status_code == 401:
            return {"error": "token_expired"}
        if resp.status_code == 403:
            logger.error("LinkedIn post forbidden (403): %s", resp.text)
            return {"error": "Forbidden (403) — check your LinkedIn app has 'Share on LinkedIn' product approved."}
        if resp.status_code not in (200, 201):
            logger.error("LinkedIn post failed: %s %s", resp.status_code, resp.text)
            return {"error": f"post_failed ({resp.status_code}): {resp.text[:300]}"}

        data = resp.json()
        post_id = data.get("id", resp.headers.get("x-restli-id", ""))
        logger.info("LinkedIn post created: %s (image=%s)", post_id, bool(asset_urn))
        return {"post_id": post_id, "status": "published", "image_attached": bool(asset_urn)}
