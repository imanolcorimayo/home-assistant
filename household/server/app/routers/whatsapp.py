"""WhatsApp Cloud API webhook.

Two endpoints:
- GET  /webhook/whatsapp  — Meta's one-time verify handshake.
- POST /webhook/whatsapp  — incoming messages. We just log them for now.
"""

import json
import logging
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.services.transaction_parser import parse_transaction
from app.services.transactions import save_parsed
from app.services.whatsapp import download_media, send_text

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
async def receive_webhook(request: Request, background: BackgroundTasks):
    """Incoming message payload from Meta.

    Webhook returns 200 fast; heavy work (Gemini, media download) happens
    in a background task so the user gets the 'procesando' ack quickly
    and Meta doesn't retry on slow acks.
    """
    body = await request.json()
    log.info("whatsapp payload: %s", json.dumps(body, ensure_ascii=False))

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                background.add_task(_handle_message_safe, msg)
    return {"status": "received"}


async def _handle_message_safe(msg: dict) -> None:
    try:
        await _handle_message(msg)
    except Exception as exc:
        log.exception("handle_message failed: %s", exc)


async def _handle_message(msg: dict) -> None:
    sender = msg["from"]
    msg_type = msg.get("type")

    if msg_type == "text":
        parsed = await parse_transaction(text=msg["text"]["body"])
    elif msg_type in ("audio", "image"):
        # Ack right away — media path takes ~5s; silence feels broken.
        await send_text(sender, "Procesando...")
        media_id = msg[msg_type]["id"]
        bytes_, mime = await download_media(media_id)
        # Strip codecs suffix Gemini doesn't like: "audio/ogg; codecs=opus" -> "audio/ogg"
        mime = mime.split(";")[0].strip()
        caption = msg.get(msg_type, {}).get("caption")
        parsed = await parse_transaction(text=caption, media_bytes=bytes_, media_mime=mime)
    else:
        log.info("ignoring unsupported message type: %s", msg_type)
        return

    tx_id = None
    if parsed:
        tx_id = await save_parsed(parsed, sender_wa_id=sender)
    await send_text(sender, _format_parsed(parsed, tx_id))


def _format_parsed(parsed: dict | None, tx_id: str | None = None) -> str:
    if not parsed:
        return "no pude procesar el mensaje (gemini no respondió)"
    if not parsed.get("kind"):
        return f"no parece una transacción (confidence={parsed.get('confidence')})"
    saved_line = f"guardado #{tx_id[:8]}" if tx_id else "no guardado (confianza baja)"
    return (
        f"{parsed['kind']} - {parsed.get('amount')}\n"
        f"{parsed.get('description') or '(sin descripcion)'}\n"
        f"categoría: {parsed.get('category') or '(sin categoría)'}\n"
        f"fecha: {parsed.get('transaction_date') or '(hoy)'}\n"
        f"cuenta: {parsed.get('account_hint') or '(?)'}\n"
        f"confianza: {parsed.get('confidence')}\n"
        f"{saved_line}"
    )


@router.post("/debug/send")
async def debug_send(to: str, body: str):
    """Curl helper: POST /webhook/debug/send?to=549...&body=hi"""
    return await send_text(to, body)


@router.post("/debug/parse")
async def debug_parse(text: str):
    """Curl helper: POST /webhook/debug/parse?text=gasté%201500%20en%20supermercado"""
    parsed = await parse_transaction(text=text)
    return {"parsed": parsed}


@router.post("/debug/parse_and_save")
async def debug_parse_and_save(text: str):
    """Curl helper: parses AND inserts. Lets us test DB writes without WhatsApp.

    POST /webhook/debug/parse_and_save?text=gasté%201500%20en%20supermercado
    """
    parsed = await parse_transaction(text=text)
    tx_id = await save_parsed(parsed, sender_wa_id="debug") if parsed else None
    return {"parsed": parsed, "transaction_id": tx_id}
