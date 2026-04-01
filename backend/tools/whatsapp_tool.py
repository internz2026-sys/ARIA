"""WhatsApp Cloud API tool — send and receive messages via Meta's API.

Each ARIA tenant can connect their own WhatsApp Business account.
Global app credentials are in .env; per-tenant tokens are in tenant_configs.integrations.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("aria.whatsapp")

# Global app-level credentials (used if tenant doesn't have their own)
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "aria-whatsapp-verify")

API_BASE = "https://graph.facebook.com/v21.0"


async def send_message(to: str, text: str,
                       access_token: str | None = None,
                       phone_number_id: str | None = None) -> dict:
    """Send a WhatsApp text message to a phone number.

    Args:
        to: Recipient phone number in international format (e.g. +639453324472)
        text: Message body
        access_token: Per-tenant token (falls back to global env)
        phone_number_id: Per-tenant phone number ID (falls back to global env)
    """
    token = access_token or WHATSAPP_ACCESS_TOKEN
    pid = phone_number_id or WHATSAPP_PHONE_NUMBER_ID

    if not token or not pid:
        return {"error": "WhatsApp not configured — missing access token or phone number ID"}

    # Strip non-numeric chars except leading +
    clean_to = to.strip().lstrip("+")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{API_BASE}/{pid}/messages",
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": clean_to,
                "type": "text",
                "text": {"body": text},
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

        if resp.status_code not in (200, 201):
            logger.error("WhatsApp send failed: %s %s", resp.status_code, resp.text)
            return {"error": f"send_failed ({resp.status_code}): {resp.text[:300]}"}

        data = resp.json()
        msg_id = ""
        messages = data.get("messages", [])
        if messages:
            msg_id = messages[0].get("id", "")

        logger.info("WhatsApp message sent to %s: %s", to, msg_id)
        return {"message_id": msg_id, "status": "sent"}


async def send_template(to: str, template_name: str, language: str = "en_US",
                        components: list | None = None,
                        access_token: str | None = None,
                        phone_number_id: str | None = None) -> dict:
    """Send a WhatsApp template message (required for initiating conversations)."""
    token = access_token or WHATSAPP_ACCESS_TOKEN
    pid = phone_number_id or WHATSAPP_PHONE_NUMBER_ID

    if not token or not pid:
        return {"error": "WhatsApp not configured"}

    clean_to = to.strip().lstrip("+")

    template_obj: dict = {
        "name": template_name,
        "language": {"code": language},
    }
    if components:
        template_obj["components"] = components

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{API_BASE}/{pid}/messages",
            json={
                "messaging_product": "whatsapp",
                "to": clean_to,
                "type": "template",
                "template": template_obj,
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

        if resp.status_code not in (200, 201):
            logger.error("WhatsApp template send failed: %s %s", resp.status_code, resp.text)
            return {"error": f"template_failed ({resp.status_code}): {resp.text[:300]}"}

        data = resp.json()
        msg_id = ""
        messages = data.get("messages", [])
        if messages:
            msg_id = messages[0].get("id", "")

        return {"message_id": msg_id, "status": "sent"}


async def get_business_profile(access_token: str | None = None,
                                phone_number_id: str | None = None) -> dict:
    """Get the WhatsApp Business profile info."""
    token = access_token or WHATSAPP_ACCESS_TOKEN
    pid = phone_number_id or WHATSAPP_PHONE_NUMBER_ID

    if not token or not pid:
        return {"error": "WhatsApp not configured"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{API_BASE}/{pid}/whatsapp_business_profile",
            params={"fields": "about,address,description,vertical,websites,profile_picture_url"},
            headers={"Authorization": f"Bearer {token}"},
        )

        if resp.status_code != 200:
            return {"error": f"profile_fetch_failed ({resp.status_code})"}

        return resp.json().get("data", [{}])[0]
