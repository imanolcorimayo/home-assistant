"""Telegram webhook for the Observer (notifications) bot.

This bot is mostly *outbound* — the dispatcher in services/notifications.py
ships proactive alerts here. The webhook itself handles only four kinds of
inbound message:

- /start            : capture chat_id, confirm enabled kinds.
- /preferencias     : list each kind with ✅ / ⛔.
- /silenciar <kind> : opt-out from a kind.
- /activar <kind>   : opt-in to a kind.

Anything else gets a one-line "I only send alerts" reply pointing at the
Registrador and Consultor bots — the Observer never enters a free chat.

Required env: OBSERVER_TELEGRAM_BOT_TOKEN, OBSERVER_TELEGRAM_WEBHOOK_SECRET.
"""

import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models import FamilyMember, NotificationPreference
from app.services.telegram import send_message

log = logging.getLogger("telegram_observer_webhook")

router = APIRouter(prefix="/webhook", tags=["telegram-observer"])

# Canonical list of kinds for /preferencias display. Mirrors migration 006.
_KNOWN_KINDS = [
    "budget_80",
    "budget_100",
    "recurring_due_3d",
    "recurring_due_today",
    "recurring_overdue_1d",
    "recurring_overdue_7d",
    "weekly_summary",
    "inactivity",
    "unusual_tx",
]


def _token() -> str:
    return os.environ["OBSERVER_TELEGRAM_BOT_TOKEN"]


@router.post("/telegram-observer")
async def receive_webhook(
    request: Request,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    expected_secret = os.environ.get("OBSERVER_TELEGRAM_WEBHOOK_SECRET")
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=403, detail="bad secret token")

    body = await request.json()
    log.info("observer payload: %s", json.dumps(body, ensure_ascii=False))

    message = body.get("message") or body.get("edited_message")
    if not message:
        return {"ok": True}

    background.add_task(_handle_message_safe, message)
    return {"ok": True}


async def _handle_message_safe(msg: dict) -> None:
    try:
        await _handle_message(msg)
    except Exception as exc:
        log.exception("observer handle_message failed: %s", exc)


async def _handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    from_user = msg.get("from") or {}
    tg_user_id = from_user.get("id")
    text = (msg.get("text") or "").strip()

    member = await _lookup_member(tg_user_id)

    # /start: persist chat_id (so the dispatcher can reach the user even if
    # the bind was set up via SQL and never via webhook), then echo the
    # enabled kinds. If unbound, fall through to the bind prompt.
    if text.split()[:1] == ["/start"]:
        if member is None:
            await send_message(chat_id, _unbound_msg(tg_user_id), token=_token())
            return
        await _persist_chat_id(member.family_member_id, chat_id)
        kinds = await _enabled_kinds_for(member.family_member_id)
        body = (
            f"Hola {from_user.get('first_name','')}, soy el Observador. "
            f"A partir de ahora te aviso de cosas importantes (presupuesto, "
            f"recurrentes, gastos inusuales, resumen semanal los domingos).\n\n"
            f"Estás recibiendo: {', '.join(kinds) if kinds else '(nada activo)'}.\n\n"
            f"Comandos:\n"
            f"• /preferencias\n"
            f"• /silenciar <kind>\n"
            f"• /activar <kind>"
        )
        await send_message(chat_id, body, token=_token())
        return

    if member is None:
        await send_message(chat_id, _unbound_msg(tg_user_id), token=_token())
        return

    parts = text.split()
    cmd = parts[0] if parts else ""

    if cmd == "/preferencias":
        rows = await _all_preferences(member.family_member_id)
        # Show every known kind, ✅ or ⛔, so the user sees what's available
        # even if a kind hasn't been seeded into preferences yet.
        lines = ["Preferencias de notificaciones:"]
        for k in _KNOWN_KINDS:
            on = rows.get(k, True)  # default TRUE for missing
            lines.append(f"{'✅' if on else '⛔'} {k}")
        lines.append("\nUsá /silenciar <kind> o /activar <kind> para cambiar.")
        await send_message(chat_id, "\n".join(lines), token=_token())
        return

    if cmd in ("/silenciar", "/activar"):
        if len(parts) < 2:
            await send_message(chat_id, f"Uso: {cmd} <kind>. Ej: {cmd} budget_80",
                               token=_token())
            return
        kind = parts[1]
        if kind not in _KNOWN_KINDS:
            await send_message(
                chat_id,
                f"Kind desconocido: {kind}. Mandá /preferencias para ver la lista.",
                token=_token(),
            )
            return
        enabled = cmd == "/activar"
        await _upsert_preference(member.family_member_id, kind, enabled)
        await send_message(
            chat_id,
            f"{'✅' if enabled else '⛔'} {kind} {'activado' if enabled else 'silenciado'}.",
            token=_token(),
        )
        return

    # Anything else: friendly redirect. The Observer never enters free chat.
    await send_message(
        chat_id,
        "Soy el Observador, solo mando avisos. Para preguntar usá "
        "@Analisis_cassa_bot; para registrar usá @Manager_House_Hold_bot.",
        token=_token(),
    )


# ============================================================
# DB helpers
# ============================================================


def _unbound_msg(tg_user_id: int | None) -> str:
    return (
        f"No estás vinculado todavía. Tu ID de Telegram es {tg_user_id}. "
        f"Pedile a Hector que te vincule (UPDATE family_member SET "
        f"telegram_user_id = {tg_user_id} WHERE full_name = '<vos>')."
    )


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


async def _persist_chat_id(family_member_id, chat_id: int) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(FamilyMember)
            .where(FamilyMember.family_member_id == family_member_id)
            .values(telegram_chat_id=chat_id)
        )
        await session.commit()


async def _enabled_kinds_for(family_member_id) -> list[str]:
    async with AsyncSessionLocal() as session:
        rows = await session.scalars(
            select(NotificationPreference.kind).where(
                NotificationPreference.family_member_id == family_member_id,
                NotificationPreference.enabled,
            )
        )
        return list(rows)


async def _all_preferences(family_member_id) -> dict:
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(NotificationPreference.kind, NotificationPreference.enabled).where(
                NotificationPreference.family_member_id == family_member_id,
            )
        )).all()
    return {r.kind: bool(r.enabled) for r in rows}


async def _upsert_preference(family_member_id, kind: str, enabled: bool) -> None:
    """Insert or update one preference row. We use ON CONFLICT on the UNIQUE
    (family_member_id, kind) so users can toggle a kind that was never seeded."""
    async with AsyncSessionLocal() as session:
        stmt = pg_insert(NotificationPreference).values(
            family_member_id=family_member_id, kind=kind, enabled=enabled,
        ).on_conflict_do_update(
            index_elements=["family_member_id", "kind"],
            set_={"enabled": enabled, "updated_ts": datetime.now(timezone.utc)},
        )
        await session.execute(stmt)
        await session.commit()
