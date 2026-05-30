"""Telegram webhook for the Director (orchestrator) bot.

Twin of `telegram_consultant.py` but pointing at the Director bot token and
calling `run_director_agent`, which routes each message to the Registrador
or Consultor via local Gemini tools. The Director keeps its own session
under 'director:telegram:{chat_id}', and passes the subagent session keys
('telegram:{chat_id}' and 'consultant:telegram:{chat_id}') so memory threads
through the same way as if the user had talked to those bots directly.

Required env: DIRECTOR_TELEGRAM_BOT_TOKEN, DIRECTOR_TELEGRAM_WEBHOOK_SECRET.
"""

import json
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import FamilyMember
from app.services.agent import run_director_agent
from app.services.telegram import send_message

log = logging.getLogger("telegram_director_webhook")

router = APIRouter(prefix="/webhook", tags=["telegram-director"])


def _token() -> str:
    return os.environ["DIRECTOR_TELEGRAM_BOT_TOKEN"]


@router.post("/telegram-director")
async def receive_webhook(
    request: Request,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Incoming update from the Director bot. Webhook returns 200 fast;
    Gemini work (including the subagent call it routes to) happens in a
    background task. Secret token validation mirrors the other bots."""
    expected_secret = os.environ.get("DIRECTOR_TELEGRAM_WEBHOOK_SECRET")
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=403, detail="bad secret token")

    body = await request.json()
    log.info("director payload: %s", json.dumps(body, ensure_ascii=False))

    message = body.get("message") or body.get("edited_message")
    if not message:
        return {"ok": True}

    background.add_task(_handle_message_safe, message)
    return {"ok": True}


async def _handle_message_safe(msg: dict) -> None:
    try:
        await _handle_message(msg)
    except Exception as exc:
        log.exception("director handle_message failed: %s", exc)
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is not None:
            try:
                await send_message(
                    chat_id,
                    "No pude procesar tu mensaje ahora (el director o algún "
                    "subagente tuvo un error). Probá de nuevo en un minuto.",
                    token=_token(),
                )
            except Exception:
                log.exception("could not notify user of director failure")


async def _handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    from_user = msg.get("from") or {}
    tg_user_id = from_user.get("id")
    text = msg.get("text")

    # /start: identify bot + return the user's telegram id. Binding is shared
    # with the other 3 bots (same column on family_member).
    if text and text.strip().split()[0] == "/start":
        name = from_user.get("first_name", "")
        await send_message(
            chat_id,
            (
                f"Hola {name}, soy el Director. Mandame cualquier cosa y yo "
                f"se la paso al subagente que corresponde: si registrás "
                f"algo, va al Registrador; si preguntás por números, va al "
                f"Consultor. No necesitás elegir vos.\n\n"
                f"Tu ID de Telegram es: {tg_user_id}. Si todavía no estás "
                f"vinculado a un miembro de la familia, pasale este ID a Hector."
            ),
            token=_token(),
        )
        return

    member = await _lookup_member(tg_user_id)
    if member is None:
        await send_message(
            chat_id,
            "No estás registrado como miembro de la familia. Mandá /start "
            "para ver tu ID y pedile a Hector que te vincule.",
            token=_token(),
        )
        return

    # MVP: solo texto. Foto/voz se ignoran con respuesta amable — el
    # Registrador sí las maneja, pero por ahora el Director no las rutea
    # (follow-up: forward al Registrador con la media tal cual).
    if not text:
        await send_message(
            chat_id,
            "Por ahora respondo sólo mensajes de texto. Si querés registrar "
            "una foto o nota de voz, mandala al Registrador directamente.",
            token=_token(),
        )
        return

    reply = await run_director_agent(
        message=text,
        session_id=f"director:telegram:{chat_id}",
        sender_name=member.full_name,
        registrar_session_id=f"telegram:{chat_id}",
        consultant_session_id=f"consultant:telegram:{chat_id}",
    )
    await send_message(chat_id, reply or "(sin respuesta)", token=_token())


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


@router.post("/debug/telegram_director_parse")
async def debug_director_parse(text: str, telegram_user_id: int):
    """Curl helper: runs the Director with a fake Telegram message.
    POST /webhook/debug/telegram_director_parse?text=gast%C3%A9%2030&telegram_user_id=12345
    """
    member = await _lookup_member(telegram_user_id)
    if member is None:
        return {"error": f"no member with telegram_user_id={telegram_user_id}"}
    chat_id = f"debug:{telegram_user_id}"
    reply = await run_director_agent(
        message=text,
        session_id=f"director:telegram:{chat_id}",
        sender_name=member.full_name,
        registrar_session_id=f"telegram:{chat_id}",
        consultant_session_id=f"consultant:telegram:{chat_id}",
    )
    return {"reply": reply, "member": member.full_name}
