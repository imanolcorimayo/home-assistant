"""
POST /webhook/telegram — punto de entrada único para todos los mensajes de Telegram.

Regla crítica (spec §4.1): este endpoint SIEMPRE responde HTTP 200 en < 3s.
Todo procesamiento pesado va a Celery. El usuario recibe el resultado desde el worker.
"""
import logging
from datetime import datetime

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, text, update
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
    processed_key = f"processed_update:{update.update_id}"
    if not await _redis.set(processed_key, 1, nx=True, ex=86400):
        return {"ok": True}

    if not update.message:
        return {"ok": True}

    msg = update.message
    if not msg.from_user:
        return {"ok": True}

    result = await db.execute(
        select(FamilyMember).where(
            FamilyMember.telegram_user_id == msg.from_user.id,
            FamilyMember.is_active.is_(True),
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        return {"ok": True}

    chat_id = msg.chat.id
    user_id = str(member.id)

    if msg.text and msg.text.startswith("/"):
        await _handle_command(msg.text, member, chat_id, db)
        return {"ok": True}

    pending = await _redis.get(f"pending_tx:{chat_id}")
    if pending:
        await _handle_confirmation(msg.text, chat_id, user_id)
        return {"ok": True}

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
        await _cmd_undo(member, chat_id, db)
    elif cmd == "/gastos":
        await _cmd_gastos(chat_id, db)
    elif cmd == "/resumen":
        await _cmd_resumen(chat_id, db)
    elif cmd == "/ayuda":
        await send_message(
            chat_id,
            "Comandos disponibles:\n"
            "/resumen — balance del mes (ingresos, gastos, ahorro)\n"
            "/gastos  — detalle de gastos variables del mes\n"
            "/undo    — eliminar el último movimiento registrado\n"
            "/ayuda   — esta ayuda",
        )
    else:
        await send_message(chat_id, f"Comando no reconocido: {cmd}\nUsá /ayuda para ver los disponibles.")


async def _cmd_undo(member: FamilyMember, chat_id: int, db: AsyncSession) -> None:
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
        .returning(
            Transaction.amount,
            Transaction.currency,
            Transaction.subcategoria1,
            Transaction.subcategoria2,
        )
    )
    result = await db.execute(stmt)
    row = result.first()
    await db.commit()

    if row:
        amount_fmt = f"{float(row.amount):.2f}".rstrip("0").rstrip(".")
        sub2 = f" / {row.subcategoria2}" if row.subcategoria2 else ""
        await send_message(chat_id, f"🗑️ Eliminado: {amount_fmt} {row.currency} — {row.subcategoria1}{sub2}")
    else:
        await send_message(chat_id, "No hay movimientos para deshacer.")


async def _cmd_resumen(chat_id: int, db: AsyncSession) -> None:
    now = datetime.now()
    result = await db.execute(
        text("""
            SELECT ingresos, gastos, balance, pct_gasto_sobre_ingreso
            FROM v_balance_mensual
            WHERE anio = :anio AND mes = :mes
        """),
        {"anio": now.year, "mes": now.month},
    )
    row = result.first()

    if not row or (row.ingresos == 0 and row.gastos == 0):
        await send_message(chat_id, "No hay movimientos registrados este mes.")
        return

    pct = f"{row.pct_gasto_sobre_ingreso:.0f}%" if row.pct_gasto_sobre_ingreso else "—"
    msg = (
        f"📊 Resumen {now.strftime('%B %Y')}\n"
        f"─────────────────\n"
        f"💰 Ingresos:  {row.ingresos:>10.2f} €\n"
        f"💸 Gastos:    {row.gastos:>10.2f} €\n"
        f"─────────────────\n"
        f"{'✅' if row.balance >= 0 else '⚠️'} Balance:  {row.balance:>10.2f} €\n"
        f"📈 Gasto/ingreso: {pct}"
    )
    await send_message(chat_id, msg)


async def _cmd_gastos(chat_id: int, db: AsyncSession) -> None:
    now = datetime.now()
    result = await db.execute(
        text("""
            SELECT subcategoria1, subcategoria2, ROUND(SUM(total)::numeric, 2) AS total
            FROM v_gastos_variables
            WHERE anio = :anio AND mes = :mes
            GROUP BY subcategoria1, subcategoria2
            ORDER BY SUM(total) DESC
            LIMIT 15
        """),
        {"anio": now.year, "mes": now.month},
    )
    rows = result.all()

    if not rows:
        await send_message(chat_id, "No hay gastos variables registrados este mes.")
        return

    lines = []
    prev_sub1 = None
    for r in rows:
        if r.subcategoria1 != prev_sub1:
            lines.append(f"\n*{r.subcategoria1}*")
            prev_sub1 = r.subcategoria1
        sub2 = f"  {r.subcategoria2}" if r.subcategoria2 else "  (sin categoría)"
        lines.append(f"{sub2}: {r.total:.2f} €")

    total_result = await db.execute(
        text("""
            SELECT ROUND(SUM(total)::numeric, 2) FROM v_gastos_variables
            WHERE anio = :anio AND mes = :mes
        """),
        {"anio": now.year, "mes": now.month},
    )
    grand_total = total_result.scalar() or 0

    lines.append(f"\n─────────────────")
    lines.append(f"Total: {grand_total:.2f} €")
    await send_message(chat_id, f"💸 Gastos {now.strftime('%B %Y')}" + "\n".join(lines))


async def _handle_confirmation(text: str | None, chat_id: int, user_id: str) -> None:
    text_lower = (text or "").lower().strip()
    confirmed = text_lower in ("sí", "si", "s", "yes", "y", "1")
    save_pending_transaction.delay(chat_id, user_id, confirmed)
