"""Apollo.io API wrapper — B2B lead database (210M+ companies)."""
from __future__ import annotations

import os
import httpx

BASE_URL = "https://api.apollo.io/v1"


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": os.environ.get("APOLLO_API_KEY", ""),
    }


async def search_companies(query: str, filters: dict | None = None) -> dict:
    """Search Apollo for companies matching ICP criteria."""
    async with httpx.AsyncClient() as client:
        payload = {
            "q_organization_name": query,
            "page": 1,
            "per_page": 25,
            **(filters or {}),
        }
        resp = await client.post(f"{BASE_URL}/mixed_companies/search", json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def enrich_company(domain: str) -> dict:
    """Get detailed company information by domain."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/organizations/enrich",
            params={"domain": domain},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def search_people(company_id: str, titles: list[str]) -> dict:
    """Find contacts at a company by title."""
    async with httpx.AsyncClient() as client:
        payload = {
            "q_organization_ids": [company_id],
            "person_titles": titles,
            "page": 1,
            "per_page": 10,
        }
        resp = await client.post(f"{BASE_URL}/mixed_people/search", json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()
