"""Telegram webhook for the Consultor (analyst) bot.

Twin of `telegram.py` but pointing at the separate Consultor bot token and
calling `run_consultant_agent` instead of `run_agent`. Same auth (by
family_member.telegram_user_id), same session-memory pattern, same retry on
Gemini failures. The session_id is prefixed with 'consultant:' so agent_run
rows can be filtered per bot without a schema change.

Required env: CONSULTANT_TELEGRAM_BOT_TOKEN, CONSULTANT_TELEGRAM_WEBHOOK_SECRET.
"""

import json
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import FamilyMember
from app.services.agent import run_consultant_agent
from app.services.telegram import send_message

log = logging.getLogger("telegram_consultant_webhook")

router = APIRouter(prefix="/webhook", tags=["telegram-consultant"])


def _token() -> str:
    return os.environ["CONSULTANT_TELEGRAM_BOT_TOKEN"]


@router.post("/telegram-consultant")
async def receive_webhook(
    request: Request,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Incoming update from the Consultor bot. Webhook returns 200 fast;
    Gemini work happens in a background task. The secret token validation
    mirrors the Registrador router."""
    expected_secret = os.environ.get("CONSULTANT_TELEGRAM_WEBHOOK_SECRET")
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=403, detail="bad secret token")

    body = await request.json()
    log.info("consultant payload: %s", json.dumps(body, ensure_ascii=False))

    message = body.get("message") or body.get("edited_message")
    if not message:
        return {"ok": True}

    background.add_task(_handle_message_safe, message)
    return {"ok": True}


async def _handle_message_safe(msg: dict) -> None:
    try:
        await _handle_message(msg)
    except Exception as exc:
        log.exception("consultant handle_message failed: %s", exc)
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is not None:
            try:
                await send_message(
                    chat_id,
                    "No pude contestar ahora (Gemini está saturado o tuvo un "
                    "error). Probá de nuevo en un minuto.",
                    token=_token(),
                )
            except Exception:
                log.exception("could not notify user of consultant failure")


async def _handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    from_user = msg.get("from") or {}
    tg_user_id = from_user.get("id")
    text = msg.get("text")

    # /start: identify bot + return the user's telegram id (the bind is shared
    # with the Registrador — same column on family_member).
    if text and text.strip().split()[0] == "/start":
        name = from_user.get("first_name", "")
        await send_message(
            chat_id,
            (
                f"Hola {name}, soy el Consultor. Te respondo preguntas sobre "
                f"los gastos e ingresos de la familia, basadas en los datos "
                f"reales. Probá:\n"
                f"• 'cuánto gasté este mes'\n"
                f"• 'balance del mes'\n"
                f"• 'tendencia de transporte últimos 6 meses'\n"
                f"• 'qué recurrentes me faltan pagar'\n"
                f"• 'top 5 categorías de este año'\n\n"
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
            "No estás registrado como miembro de la familia. Mandá /start para "
            "ver tu ID y pedile a Hector que te vincule.",
            token=_token(),
        )
        return

    # V1: solo texto. Foto/voz se ignoran con una respuesta amable.
    if not text:
        await send_message(
            chat_id,
            "Por ahora respondo sólo preguntas de texto. Escribí lo que querés "
            "consultar (ej 'cuánto gasté este mes en super').",
            token=_token(),
        )
        return

    reply = await run_consultant_agent(
        message=text,
        session_id=f"consultant:telegram:{chat_id}",
        sender_name=member.full_name,
    )
    await send_message(chat_id, reply or "(sin respuesta)", token=_token(), parse_mode="Markdown")


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


@router.post("/debug/telegram_consultant_parse")
async def debug_consultant_parse(text: str, telegram_user_id: int):
    """Curl helper: runs the consultor agent with a fake Telegram message.
    POST /webhook/debug/telegram_consultant_parse?text=cu%C3%A1nto%20gast%C3%A9%20este%20mes&telegram_user_id=12345
    """
    member = await _lookup_member(telegram_user_id)
    if member is None:
        return {"error": f"no member with telegram_user_id={telegram_user_id}"}
    reply = await run_consultant_agent(
        message=text,
        session_id=f"consultant:telegram:debug:{telegram_user_id}",
        sender_name=member.full_name,
    )
    return {"reply": reply, "member": member.full_name}
