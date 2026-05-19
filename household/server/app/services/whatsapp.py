"""Outbound WhatsApp send. One function, one POST."""

import logging
import os

import httpx

log = logging.getLogger("whatsapp")

GRAPH_URL = "https://graph.facebook.com/v18.0"


async def send_text(to: str, body: str) -> dict:
    phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{GRAPH_URL}/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
    r.raise_for_status()
    data = r.json()
    message_id = (data.get("messages") or [{}])[0].get("id")
    log.info("sent to=%s id=%s", to, message_id)
    return data
