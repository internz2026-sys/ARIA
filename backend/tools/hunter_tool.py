"""Hunter.io API wrapper — email finder and verification."""

import os
import httpx

BASE_URL = "https://api.hunter.io/v2"


def _params() -> dict:
    return {"api_key": os.environ.get("HUNTER_API_KEY", "")}


async def find_email(first_name: str, last_name: str, domain: str) -> dict:
    """Find a verified email address for a person at a company."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/email-finder",
            params={**_params(), "domain": domain, "first_name": first_name, "last_name": last_name},
        )
        resp.raise_for_status()
        return resp.json().get("data", {})


async def verify_email(email: str) -> dict:
    """Verify an email address deliverability."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/email-verifier",
            params={**_params(), "email": email},
        )
        resp.raise_for_status()
        return resp.json().get("data", {})
