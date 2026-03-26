"""HubSpot CRM API wrapper — contacts, deals, pipeline, activity logging."""
from __future__ import annotations

import httpx

BASE_URL = "https://api.hubapi.com"


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


async def create_contact(api_key: str, email: str, properties: dict) -> dict:
    """Create a CRM contact."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/crm/v3/objects/contacts",
            json={"properties": {"email": email, **properties}},
            headers=_headers(api_key),
        )
        resp.raise_for_status()
        return resp.json()


async def update_contact(api_key: str, contact_id: str, properties: dict) -> dict:
    """Update an existing contact."""
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{BASE_URL}/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
            headers=_headers(api_key),
        )
        resp.raise_for_status()
        return resp.json()


async def create_deal(api_key: str, properties: dict) -> dict:
    """Create a deal in the pipeline."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/crm/v3/objects/deals",
            json={"properties": properties},
            headers=_headers(api_key),
        )
        resp.raise_for_status()
        return resp.json()


async def update_deal_stage(api_key: str, deal_id: str, stage: str) -> dict:
    """Update deal pipeline stage."""
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{BASE_URL}/crm/v3/objects/deals/{deal_id}",
            json={"properties": {"dealstage": stage}},
            headers=_headers(api_key),
        )
        resp.raise_for_status()
        return resp.json()


async def get_contacts(api_key: str, filters: dict | None = None) -> dict:
    """Search contacts with filters."""
    async with httpx.AsyncClient() as client:
        payload = {"filterGroups": [{"filters": []}], "limit": 100}
        if filters:
            payload["filterGroups"][0]["filters"] = [
                {"propertyName": k, "operator": "EQ", "value": v}
                for k, v in filters.items()
            ]
        resp = await client.post(f"{BASE_URL}/crm/v3/objects/contacts/search", json=payload, headers=_headers(api_key))
        resp.raise_for_status()
        return resp.json()


async def log_activity(api_key: str, contact_id: str, activity_type: str, details: str) -> dict:
    """Log an interaction on a contact."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/crm/v3/objects/notes",
            json={
                "properties": {"hs_note_body": f"[{activity_type}] {details}", "hs_timestamp": ""},
                "associations": [{"to": {"id": contact_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}],
            },
            headers=_headers(api_key),
        )
        resp.raise_for_status()
        return resp.json()
