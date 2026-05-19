"""Outbound WhatsApp send + media download."""

import logging
import os

import httpx

log = logging.getLogger("whatsapp")

GRAPH_URL = "https://graph.facebook.com/v18.0"


async def download_media(media_id: str) -> tuple[bytes, str]:
    """Fetch WhatsApp media by ID. Returns (bytes, mime_type).

    Two-step: GET /{media_id} returns a short-lived URL; then GET that
    URL with the Bearer token to get the actual bytes.
    """
    token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        meta = await client.get(f"{GRAPH_URL}/{media_id}", headers=headers)
        meta.raise_for_status()
        info = meta.json()
        url = info["url"]
        mime = info.get("mime_type", "application/octet-stream")

        blob = await client.get(url, headers=headers)
        blob.raise_for_status()
        log.info("downloaded media id=%s mime=%s bytes=%d", media_id, mime, len(blob.content))
        return blob.content, mime


def _normalize_to(to: str) -> str:
    # AR mobile quirk: inbound wa_id comes as "549<area><number>" but Meta's
    # send API expects "54<area><number>" (without the mobile-prefix 9).
    if to.startswith("549"):
        return "54" + to[3:]
    return to


async def send_text(to: str, body: str) -> dict:
    phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    payload = {
        "messaging_product": "whatsapp",
        "to": _normalize_to(to),
        "type": "text",
        "text": {"body": body},
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{GRAPH_URL}/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
    if r.status_code >= 400:
        log.error("send_text %d to=%s body=%r meta_response=%s",
                  r.status_code, to, body, r.text)
        r.raise_for_status()
    data = r.json()
    message_id = (data.get("messages") or [{}])[0].get("id")
    log.info("sent to=%s id=%s", to, message_id)
    return data
