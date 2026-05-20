"""Minimal chat API for testing the household flow without WhatsApp/Telegram.

One POST endpoint that takes a text message and runs it through the same
parse → save pipeline the WhatsApp webhook uses. The HTML/JS that talks
to it lives in /code/chat (mounted from the repo's household/chat/ dir).
"""

import logging

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel

from app.services.transaction_parser import parse_transaction
from app.services.transactions import save_parsed

log = logging.getLogger("chat")

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    text: str


class ChatResponse(BaseModel):
    reply: str
    transaction_id: str | None = None


def _format_reply(parsed: dict | None, tx_id: str | None) -> str:
    if not parsed:
        return "no pude procesar el mensaje (gemini no respondió)"
    if not parsed.get("kind"):
        return f"no parece una transacción (confianza={parsed.get('confidence')})"
    saved = f"guardado #{tx_id[:8]}" if tx_id else "no guardado (confianza baja)"
    lines = [
        f"{parsed['kind']} - ${parsed.get('amount')}",
        parsed.get("description") or "(sin descripcion)",
        f"fecha: {parsed.get('transaction_date') or '(hoy)'}",
        f"cuenta: {parsed.get('account_hint') or '(?)'}",
        f"confianza: {parsed.get('confidence')}",
        saved,
    ]
    return "\n".join(lines)


@router.post("/message", response_model=ChatResponse)
async def chat_message(req: ChatRequest) -> ChatResponse:
    text = (req.text or "").strip()
    if not text:
        return ChatResponse(reply="mensaje vacío")
    parsed = await parse_transaction(text=text)
    tx_id = await save_parsed(parsed, sender_wa_id="chat-ui") if parsed else None
    return ChatResponse(reply=_format_reply(parsed, tx_id), transaction_id=tx_id)


@router.post("/media", response_model=ChatResponse)
async def chat_media(
    file: UploadFile = File(...),
    caption: str | None = Form(None),
) -> ChatResponse:
    data = await file.read()
    if not data:
        return ChatResponse(reply="archivo vacío")
    # Strip codecs suffix Gemini doesn't like ("audio/webm;codecs=opus").
    mime = (file.content_type or "application/octet-stream").split(";")[0].strip()
    log.info("chat media: mime=%s bytes=%d caption=%r", mime, len(data), caption)
    parsed = await parse_transaction(text=caption, media_bytes=data, media_mime=mime)
    tx_id = await save_parsed(parsed, sender_wa_id="chat-ui") if parsed else None
    return ChatResponse(reply=_format_reply(parsed, tx_id), transaction_id=tx_id)
