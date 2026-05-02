"""
POST /webhook/telegram — punto de entrada único para todos los mensajes de Telegram.

Regla crítica (spec §4.1): este endpoint SIEMPRE responde HTTP 200 en < 3s.
Todo procesamiento pesado va a Celery. El usuario recibe el resultado desde el worker.
"""
import logging
from datetime import datetime

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.finance import FamilyMember, Transaction
from app.schemas.finance import TelegramUpdate
from app.services.telegram_client import send_message
from app.workers.finance_tasks import (
    process_audio_message,
    process_text_message,
    save_pending_transaction,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_redis = aioredis.from_url(settings.redis_url, decode_responses=True)


@router.post("/telegram")
async def telegram_webhook(
    update: TelegramUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    # ── Idempotencia: ignorar updates duplicados (Telegram puede reenviarlos) ──
    processed_key = f"processed_update:{update.update_id}"
    if not await _redis.set(processed_key, 1, nx=True, ex=86400):
        return {"ok": True}

    if not update.message:
        return {"ok": True}

    msg = update.message
    if not msg.from_user:
        return {"ok": True}

    # ── Verificar que el usuario está registrado (spec §4.5) ──
    result = await db.execute(
        select(FamilyMember).where(
            FamilyMember.telegram_user_id == msg.from_user.id,
            FamilyMember.is_active.is_(True),
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        return {"ok": True}  # silencioso — no revelar que el bot existe

    chat_id = msg.chat.id
    user_id = str(member.id)

    # ── Comandos ──
    if msg.text and msg.text.startswith("/"):
        await _handle_command(msg.text, member, chat_id, db)
        return {"ok": True}

    # ── Respuesta a confirmación pendiente (confianza baja) ──
    pending = await _redis.get(f"pending_tx:{chat_id}")
    if pending:
        await _handle_confirmation(msg.text, chat_id, user_id)
        return {"ok": True}

    # ── Despachar a Celery (fire & forget) ──
    if msg.voice or msg.audio:
        file_id = (msg.voice or msg.audio).file_id
        process_audio_message.delay(file_id, chat_id, user_id)
    elif msg.text:
        process_text_message.delay(msg.text, chat_id, user_id)

    return {"ok": True}


async def _handle_command(
    text: str,
    member: FamilyMember,
    chat_id: int,
    db: AsyncSession,
) -> None:
    cmd = text.split()[0].lower()

    if cmd == "/undo":
        # Soft delete de la última transacción activa del usuario
        subq = (
            select(Transaction.id)
            .where(
                Transaction.family_member_id == member.id,
                Transaction.deleted_at.is_(None),
            )
            .order_by(Transaction.created_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        stmt = (
            update(Transaction)
            .where(Transaction.id == subq)
            .values(deleted_at=func.now())
            .returning(Transaction.amount, Transaction.currency, Transaction.category)
        )
        result = await db.execute(stmt)
        row = result.first()
        await db.commit()

        if row:
            amount_fmt = f"{float(row.amount):.2f}".rstrip("0").rstrip(".")
            await send_message(
                chat_id,
                f"🗑️ Eliminado: {amount_fmt} {row.currency} en {row.category.value}",
            )
        else:
            await send_message(chat_id, "No hay gastos para deshacer.")

    elif cmd == "/gastos":
        await _send_monthly_summary(member, chat_id, db)

    else:
        await send_message(chat_id, f"Comando no reconocido: {cmd}")


async def _send_monthly_summary(
    member: FamilyMember,
    chat_id: int,
    db: AsyncSession,
) -> None:
    now = datetime.now()
    result = await db.execute(
        select(Transaction.category, func.sum(Transaction.amount).label("total"))
        .where(
            Transaction.family_member_id == member.id,
            Transaction.deleted_at.is_(None),
            func.extract("year", Transaction.transaction_date) == now.year,
            func.extract("month", Transaction.transaction_date) == now.month,
        )
        .group_by(Transaction.category)
        .order_by(func.sum(Transaction.amount).desc())
    )
    rows = result.all()

    if not rows:
        await send_message(chat_id, "No hay gastos registrados este mes.")
        return

    lines = [f"{cat.value}: {float(total):.2f} EUR" for cat, total in rows]
    grand_total = sum(float(total) for _, total in rows)
    lines.append(f"\nTotal: {grand_total:.2f} EUR")
    await send_message(chat_id, "\n".join(lines))


async def _handle_confirmation(text: str | None, chat_id: int, user_id: str) -> None:
    text_lower = (text or "").lower().strip()
    confirmed = text_lower in ("sí", "si", "s", "yes", "y", "1")
    save_pending_transaction.delay(chat_id, user_id, confirmed)
