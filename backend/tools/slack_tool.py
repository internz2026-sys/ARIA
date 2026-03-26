"""Slack API wrapper — internal team alerts and notifications."""
from __future__ import annotations

import os
import httpx


async def send_notification(message: str, channel: str | None = None) -> dict:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    async with httpx.AsyncClient() as client:
        payload = {"text": message}
        if channel:
            payload["channel"] = channel
        resp = await client.post(webhook_url, json=payload)
        resp.raise_for_status()
        return {"status": "sent"}


async def send_alert(message: str, priority: str = "normal") -> dict:
    prefix = {"high": "🚨 URGENT", "medium": "⚠️ ALERT", "normal": "ℹ️ INFO"}.get(priority, "ℹ️")
    return await send_notification(f"{prefix}: {message}")
