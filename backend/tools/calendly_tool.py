"""Calendly API wrapper — appointment booking."""

import os
import httpx

BASE_URL = "https://api.calendly.com"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('CALENDLY_API_KEY', '')}",
        "Content-Type": "application/json",
    }


async def get_available_slots(event_type_uri: str) -> dict:
    """Get available booking slots for an event type."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/event_type_available_times",
            params={"event_type": event_type_uri},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def create_booking_link(event_type_uri: str, invitee_email: str) -> dict:
    """Generate a personalized scheduling link."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/scheduling_links",
            json={"max_event_count": 1, "owner": event_type_uri, "owner_type": "EventType"},
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return {"booking_url": data.get("resource", {}).get("booking_url", ""), "invitee": invitee_email}
