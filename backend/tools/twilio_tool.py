"""Twilio API wrapper — WhatsApp Business + SMS."""

import os
from twilio.rest import Client


def _client() -> Client:
    return Client(
        os.environ.get("TWILIO_ACCOUNT_SID", ""),
        os.environ.get("TWILIO_AUTH_TOKEN", ""),
    )


async def send_whatsapp(to: str, body: str) -> dict:
    """Send a WhatsApp message."""
    message = _client().messages.create(
        from_=f"whatsapp:{os.environ.get('TWILIO_WHATSAPP_NUMBER', '')}",
        to=f"whatsapp:{to}",
        body=body,
    )
    return {"sid": message.sid, "status": message.status}


async def send_sms(to: str, body: str) -> dict:
    """Send an SMS message."""
    message = _client().messages.create(
        from_=os.environ.get("TWILIO_WHATSAPP_NUMBER", ""),
        to=to,
        body=body,
    )
    return {"sid": message.sid, "status": message.status}
