"""Google Business Profile API wrapper — review monitoring and response."""

import httpx

BASE_URL = "https://mybusiness.googleapis.com/v4"


async def get_reviews(access_token: str, location_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/accounts/-/locations/{location_id}/reviews",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def reply_to_review(access_token: str, review_name: str, text: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{BASE_URL}/{review_name}/reply",
            json={"comment": text},
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()
