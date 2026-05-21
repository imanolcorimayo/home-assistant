"""Telegram Bot API webhook.

Routes incoming messages (text / voice / audio / photo) through the registrar
agent — same pipeline as the chat web — so Telegram gets the conversational
UX (session memory, dedup judgment, follow-ups). The agent's MCP `add_expense`
tool writes the transaction; the sender's identity is resolved here from
`family_member.telegram_user_id` and injected into the agent's prompt.

Bind a new family member with:
  UPDATE family_member SET telegram_user_id=<id> WHERE full_name='<name>';

The user can discover their id by sending /start — the bot replies with it.
"""

import json
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import FamilyMember
from app.services.agent import run_agent
from app.services.telegram import download_media, send_message

log = logging.getLogger("telegram_webhook")

router = APIRouter(prefix="/webhook", tags=["telegram"])


@router.post("/telegram")
async def receive_webhook(
    request: Request,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Incoming update payload from Telegram.

    Webhook returns 200 fast; heavy work (Gemini, media download) happens in
    a background task — Telegram retries on slow acks and we don't want
    duplicates.

    If TELEGRAM_WEBHOOK_SECRET is set, every request must echo it back in the
    X-Telegram-Bot-Api-Secret-Token header (Telegram sends what you registered
    via setWebhook). Mismatch → 403.
    """
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=403, detail="bad secret token")

    body = await request.json()
    log.info("telegram payload: %s", json.dumps(body, ensure_ascii=False))

    message = body.get("message") or body.get("edited_message")
    if not message:
        # ignore non-message updates (channel posts, callback_query, etc) for V1
        return {"ok": True}

    background.add_task(_handle_message_safe, message)
    return {"ok": True}


async def _handle_message_safe(msg: dict) -> None:
    try:
        await _handle_message(msg)
    except Exception as exc:
        log.exception("handle_message failed: %s", exc)
        # The user is waiting for *some* reply; silence after a failed Gemini
        # call (e.g. all models 503) feels broken. Best-effort — if even this
        # send fails, the outer log line is the only trace left.
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is not None:
            try:
                await send_message(
                    chat_id,
                    "No pude procesar el mensaje ahora (Gemini está saturado o "
                    "tuvo un error). Probá de nuevo en un minuto.",
                )
            except Exception:
                log.exception("could not notify user of handle_message failure")


async def _handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    from_user = msg.get("from") or {}
    tg_user_id = from_user.get("id")
    text = msg.get("text")

    # /start: reply with the user's telegram id so Hector can bind it.
    if text and text.strip().split()[0] == "/start":
        name = from_user.get("first_name", "")
        await send_message(
            chat_id,
            f"Hola {name}. Tu ID de Telegram es: {tg_user_id}\n\n"
            f"Pasale este ID a Hector para vincularte como miembro de la familia.",
        )
        return

    member = await _lookup_member(tg_user_id)
    if member is None:
        await send_message(
            chat_id,
            "No estás registrado todavía. Mandá /start para ver tu ID y "
            "pedile a Hector que te vincule.",
        )
        return

    # Resolve content into (text, media_bytes, media_mime) for run_agent.
    media_bytes: bytes | None = None
    media_mime: str | None = None
    agent_text: str = text or ""

    if msg.get("voice") or msg.get("audio"):
        media = msg.get("voice") or msg.get("audio")
        await send_message(chat_id, "Procesando audio...")
        media_bytes, media_mime = await download_media(media["file_id"])
        media_mime = media_mime or "audio/ogg"
    elif msg.get("photo"):
        # Telegram sends an array of sizes ascending; the last is the largest.
        photo = msg["photo"][-1]
        await send_message(chat_id, "Procesando imagen...")
        agent_text = msg.get("caption") or ""
        media_bytes, media_mime = await download_media(photo["file_id"])
        media_mime = media_mime or "image/jpeg"
    elif not text:
        log.info("ignoring unsupported message type: keys=%s", list(msg.keys()))
        return

    # session_id keyed by chat so a follow-up ("sí, guardalo igual") works.
    reply = await run_agent(
        message=agent_text,
        session_id=f"telegram:{chat_id}",
        media=media_bytes,
        media_mime=media_mime,
        sender_name=member.full_name,
    )
    await send_message(chat_id, reply or "(sin respuesta)")


async def _lookup_member(tg_user_id: int | None) -> FamilyMember | None:
    if tg_user_id is None:
        return None
    async with AsyncSessionLocal() as session:
        return await session.scalar(
            select(FamilyMember).where(
                FamilyMember.telegram_user_id == tg_user_id,
                FamilyMember.is_active,
            )
        )


@router.post("/debug/telegram_parse")
async def debug_telegram_parse(text: str, telegram_user_id: int):
    """Curl helper: runs the same agent path a real Telegram text message uses,
    without going through Meta. Returns the agent's reply.
    POST /webhook/debug/telegram_parse?text=gast%C3%A9%2030&telegram_user_id=12345
    """
    member = await _lookup_member(telegram_user_id)
    if member is None:
        return {"error": f"no member with telegram_user_id={telegram_user_id}"}
    reply = await run_agent(
        message=text,
        session_id=f"telegram:debug:{telegram_user_id}",
        sender_name=member.full_name,
    )
    return {"reply": reply, "member": member.full_name}
