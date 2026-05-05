"""Twitter/X API tool — OAuth 2.0 PKCE flow + tweet publishing.

Each ARIA tenant connects their own X account via OAuth 2.0.
App credentials (TWITTER_CLIENT_ID, TWITTER_CLIENT_SECRET) are in .env.
Per-user tokens are stored in tenant_configs.integrations.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from base64 import urlsafe_b64encode
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("aria.twitter")

CLIENT_ID = os.getenv("TWITTER_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("TWITTER_CLIENT_SECRET", "")

# OAuth 2.0 scopes needed for posting + reading profile
SCOPES = "tweet.read tweet.write users.read offline.access"

# In-memory PKCE store (state → {code_verifier, tenant_id})
_pending_auth: dict[str, dict] = {}


def get_auth_url(tenant_id: str, redirect_uri: str) -> str:
    """Generate Twitter OAuth 2.0 authorization URL with PKCE."""
    if not CLIENT_ID:
        raise RuntimeError("TWITTER_CLIENT_ID not set")

    # PKCE challenge
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    state = secrets.token_urlsafe(32)
    _pending_auth[state] = {"code_verifier": code_verifier, "tenant_id": tenant_id}

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"https://x.com/i/oauth2/authorize?{urlencode(params)}"


async def exchange_code(code: str, state: str, redirect_uri: str) -> dict:
    """Exchange authorization code for access + refresh tokens."""
    pending = _pending_auth.pop(state, None)
    if not pending:
        raise RuntimeError("Invalid or expired OAuth state")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.x.com/2/oauth2/token",
            data={
                "code": code,
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "redirect_uri": redirect_uri,
                "code_verifier": pending["code_verifier"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(CLIENT_ID, CLIENT_SECRET),
        )
        if resp.status_code != 200:
            # Redact: provider error responses can echo the access_token
            # back when the request half-succeeded. Never log resp.text raw.
            from backend.services.log_redaction import redact_oauth_payload
            safe = redact_oauth_payload(resp.text)
            logger.error("Twitter token exchange failed: %s %s", resp.status_code, safe)
            raise RuntimeError(f"Token exchange failed: HTTP {resp.status_code}")

        data = resp.json()
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "tenant_id": pending["tenant_id"],
        }


async def refresh_access_token(refresh_token: str) -> dict:
    """Refresh an expired access token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.x.com/2/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
            auth=(CLIENT_ID, CLIENT_SECRET),
        )
        if resp.status_code != 200:
            logger.error("Twitter token refresh failed: %s", resp.text)
            raise RuntimeError("Twitter token refresh failed")

        data = resp.json()
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
        }


async def get_me(access_token: str) -> dict:
    """Get the authenticated user's profile."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.x.com/2/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code == 401:
            return {"error": "token_expired"}
        if resp.status_code != 200:
            return {"error": f"api_error ({resp.status_code})"}
        return resp.json().get("data", {})


async def post_tweet(access_token: str, text: str, reply_to: str | None = None) -> dict:
    """Post a tweet. Returns tweet ID on success."""
    body: dict = {"text": text}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": reply_to}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.x.com/2/tweets",
            json=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code == 401:
            return {"error": "token_expired"}
        if resp.status_code == 403:
            logger.error("Tweet forbidden (403): %s", resp.text)
            return {"error": "Forbidden (403) — your X app may need 'Read and Write' permissions. Check App Settings in developer.x.com."}
        if resp.status_code not in (200, 201):
            logger.error("Tweet failed: %s %s", resp.status_code, resp.text)
            return {"error": f"tweet_failed ({resp.status_code}): {resp.text[:200]}"}

        data = resp.json().get("data", {})
        return {"tweet_id": data.get("id", ""), "text": data.get("text", "")}


async def post_thread(access_token: str, tweets: list[str]) -> list[dict]:
    """Post a thread (list of tweets). Each replies to the previous."""
    results = []
    reply_to = None
    for text in tweets:
        result = await post_tweet(access_token, text, reply_to=reply_to)
        results.append(result)
        if result.get("error"):
            break
        reply_to = result.get("tweet_id")
    return results
