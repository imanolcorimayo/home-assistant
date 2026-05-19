"""WhatsApp Cloud API webhook.

Two endpoints:
- GET  /webhook/whatsapp  — Meta's one-time verify handshake.
- POST /webhook/whatsapp  — incoming messages. We just log them for now.
"""

import json
import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.services.whatsapp import send_text

log = logging.getLogger("whatsapp")

router = APIRouter(prefix="/webhook", tags=["whatsapp"])


@router.get("/whatsapp", response_class=PlainTextResponse)
async def verify_webhook(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
):
    """Meta calls this once when the webhook URL is saved in the dashboard.

    Must echo back hub.challenge as plain text if the token matches.
    """
    expected_token = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    if not expected_token:
        raise HTTPException(status_code=500, detail="WHATSAPP_VERIFY_TOKEN not set")
    if hub_mode != "subscribe" or hub_verify_token != expected_token:
        raise HTTPException(status_code=403, detail="verification failed")
    return hub_challenge


@router.post("/whatsapp")
async def receive_webhook(request: Request):
    """Incoming message payload from Meta.

    For now: log it and return 200. No DB writes, no LLM.
    """
    body = await request.json()
    log.info("whatsapp payload: %s", json.dumps(body, ensure_ascii=False))

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                if msg.get("type") == "text":
                    sender = msg["from"]
                    text_body = msg["text"]["body"]
                    try:
                        await send_text(sender, f"echo: {text_body}")
                    except Exception as exc:
                        log.exception("echo failed: %s", exc)
    return {"status": "received"}


@router.post("/debug/send")
async def debug_send(to: str, body: str):
    """Curl helper: POST /webhook/debug/send?to=549...&body=hi"""
    return await send_text(to, body)
