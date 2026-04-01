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


async def create_post(access_token: str, author_urn: str, text: str) -> dict:
    """Create a LinkedIn post.

    Args:
        access_token: OAuth access token
        author_urn: LinkedIn member URN (e.g. "urn:li:person:abc123")
        text: Post text content (up to 3000 chars)
    """
    async with httpx.AsyncClient() as client:
        # Use the v2 UGC Post API which is more reliable
        resp = await client.post(
            "https://api.linkedin.com/v2/ugcPosts",
            json={
                "author": author_urn,
                "lifecycleState": "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {
                            "text": text[:3000],
                        },
                        "shareMediaCategory": "NONE",
                    }
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
        logger.info("LinkedIn post created: %s", post_id)
        return {"post_id": post_id, "status": "published"}
