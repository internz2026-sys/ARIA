"""Facebook / Instagram Graph API wrapper — DMs, comments, posts."""
from __future__ import annotations

import httpx

GRAPH_URL = "https://graph.facebook.com/v18.0"


async def get_facebook_messages(page_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GRAPH_URL}/me/conversations", params={"access_token": page_token, "fields": "messages{message,from,created_time}"})
        resp.raise_for_status()
        return resp.json()


async def reply_facebook_message(page_token: str, recipient_id: str, text: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{GRAPH_URL}/me/messages", json={"recipient": {"id": recipient_id}, "message": {"text": text}}, params={"access_token": page_token})
        resp.raise_for_status()
        return resp.json()


async def get_instagram_dms(access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GRAPH_URL}/me/conversations", params={"access_token": access_token, "platform": "instagram"})
        resp.raise_for_status()
        return resp.json()


async def reply_instagram_dm(access_token: str, recipient_id: str, text: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{GRAPH_URL}/me/messages", json={"recipient": {"id": recipient_id}, "message": {"text": text}}, params={"access_token": access_token})
        resp.raise_for_status()
        return resp.json()


async def post_to_facebook(page_token: str, text: str, image_url: str | None = None) -> dict:
    async with httpx.AsyncClient() as client:
        data = {"message": text, "access_token": page_token}
        if image_url:
            data["url"] = image_url
            resp = await client.post(f"{GRAPH_URL}/me/photos", data=data)
        else:
            resp = await client.post(f"{GRAPH_URL}/me/feed", data=data)
        resp.raise_for_status()
        return resp.json()


async def post_to_instagram(access_token: str, caption: str, image_url: str) -> dict:
    async with httpx.AsyncClient() as client:
        container = await client.post(f"{GRAPH_URL}/me/media", params={"image_url": image_url, "caption": caption, "access_token": access_token})
        container.raise_for_status()
        creation_id = container.json().get("id")
        resp = await client.post(f"{GRAPH_URL}/me/media_publish", params={"creation_id": creation_id, "access_token": access_token})
        resp.raise_for_status()
        return resp.json()
