"""
POST /webhook/telegram — punto de entrada único para todos los mensajes de Telegram.

Regla crítica (spec §4.1): este endpoint SIEMPRE responde HTTP 200 en < 3s.
Todo procesamiento pesado va a Celery. El usuario recibe el resultado desde el worker.
"""
import calendar
import logging
import uuid as _uuid
from datetime import datetime

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.finance import FamilyMember, Transaction
from app.schemas.finance import TelegramCallbackQuery, TelegramUpdate
from app.services.telegram_client import answer_callback_query, send_message
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

    if update.callback_query:
        await _handle_callback_query(update.callback_query, db)
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

    parts = text.split(maxsplit=1)
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "/undo":
        await _cmd_undo(member, chat_id, db)
    elif cmd == "/gastos":
        await _cmd_gastos(chat_id, db)
    elif cmd == "/resumen":
        await _cmd_resumen(chat_id, db)
    elif cmd == "/ingresos":
        await _cmd_ingresos(chat_id, db)
    elif cmd == "/presupuesto":
        await _cmd_presupuesto(args, chat_id, db)
    elif cmd == "/proyeccion":
        await _cmd_proyeccion(chat_id, db)
    elif cmd == "/ayuda":
        await send_message(
            chat_id,
            "Comandos disponibles:\n"
            "/resumen      — balance del mes (ingresos, gastos, ahorro)\n"
            "/gastos       — detalle de gastos variables del mes\n"
            "/ingresos     — detalle de ingresos del mes por persona\n"
            "/presupuesto  — ver límites mensuales y % usado\n"
            "/presupuesto Supermercado 400  — setear límite\n"
            "/proyeccion   — proyección de gasto a fin de mes\n"
            "/undo         — eliminar el último movimiento registrado\n"
            "/ayuda        — esta ayuda",
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


async def _cmd_ingresos(chat_id: int, db: AsyncSession) -> None:
    now = datetime.now()
    result = await db.execute(
        text("""
            SELECT persona, fuente, total
            FROM v_ingresos
            WHERE anio = :anio AND mes = :mes
            ORDER BY total DESC
        """),
        {"anio": now.year, "mes": now.month},
    )
    rows = result.all()

    if not rows:
        await send_message(chat_id, "No hay ingresos registrados este mes.")
        return

    lines = []
    prev_persona = None
    subtotal = 0
    for r in rows:
        if r.persona != prev_persona:
            if prev_persona:
                lines.append(f"  Subtotal: {subtotal:.2f} €")
            lines.append(f"\n*{r.persona}*")
            prev_persona = r.persona
            subtotal = 0
        fuente = f"  {r.fuente}" if r.fuente else "  (sin fuente)"
        lines.append(f"{fuente}: {r.total:.2f} €")
        subtotal += float(r.total)
    if prev_persona:
        lines.append(f"  Subtotal: {subtotal:.2f} €")

    total_result = await db.execute(
        text("SELECT ROUND(SUM(total)::numeric,2) FROM v_ingresos WHERE anio=:anio AND mes=:mes"),
        {"anio": now.year, "mes": now.month},
    )
    grand_total = total_result.scalar() or 0
    lines.append(f"\n─────────────────")
    lines.append(f"Total: {grand_total:.2f} €")
    await send_message(chat_id, f"💰 Ingresos {now.strftime('%B %Y')}" + "\n".join(lines))


async def _handle_confirmation(text: str | None, chat_id: int, user_id: str) -> None:
    text_lower = (text or "").lower().strip()
    confirmed = text_lower in ("sí", "si", "s", "yes", "y", "1")
    save_pending_transaction.delay(chat_id, user_id, confirmed)


async def _handle_callback_query(cq: TelegramCallbackQuery, db: AsyncSession) -> None:
    try:
        await answer_callback_query(cq.id)
    except Exception as e:
        logger.warning("answer_callback_query falló (no crítico): %s", e)

    result = await db.execute(
        select(FamilyMember).where(
            FamilyMember.telegram_user_id == cq.from_user.id,
            FamilyMember.is_active.is_(True),
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        return

    chat_id = cq.message.chat.id if cq.message else cq.from_user.id

    if cq.data and cq.data.startswith("undo:"):
        try:
            tx_id = _uuid.UUID(cq.data[5:])
        except ValueError:
            return
        await _undo_transaction(tx_id, chat_id, db)
    elif cq.data in ("confirm", "cancel"):
        confirmed = cq.data == "confirm"
        save_pending_transaction.delay(chat_id, str(member.id), confirmed)


async def _undo_transaction(tx_id: _uuid.UUID, chat_id: int, db: AsyncSession) -> None:
    stmt = (
        update(Transaction)
        .where(Transaction.id == tx_id, Transaction.deleted_at.is_(None))
        .values(deleted_at=func.now())
        .returning(Transaction.amount, Transaction.currency, Transaction.subcategoria1)
    )
    result = await db.execute(stmt)
    await db.commit()
    row = result.first()
    if row:
        amount_fmt = f"{float(row.amount):.2f}".rstrip("0").rstrip(".")
        await send_message(chat_id, f"↩️ Deshecho: {amount_fmt} {row.currency} — {row.subcategoria1}")
    else:
        await send_message(chat_id, "No se pudo deshacer (ya eliminado o no existe).")


def _budget_bar(pct: float, width: int = 8) -> str:
    filled = min(int(pct / 100 * width), width)
    return "█" * filled + "░" * (width - filled)


async def _cmd_presupuesto(args: str, chat_id: int, db: AsyncSession) -> None:
    now = datetime.now()

    if args:
        parts = args.strip().rsplit(" ", 1)
        if len(parts) != 2:
            await send_message(chat_id, "Uso: /presupuesto Supermercado 400")
            return
        sub1, amount_str = parts
        # Normalizar: primera letra mayúscula para coincidir con la jerarquía ("Supermercado", "Salud"…)
        sub1 = sub1[0].upper() + sub1[1:] if sub1 else sub1
        try:
            amount = float(amount_str.replace(",", "."))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await send_message(chat_id, "El monto debe ser un número positivo. Ej: /presupuesto Supermercado 400")
            return

        await db.execute(
            text("""
                INSERT INTO monthly_budgets (subcategoria1, limit_amount, updated_at)
                VALUES (:sub1, :amount, NOW())
                ON CONFLICT (subcategoria1)
                DO UPDATE SET limit_amount = :amount, updated_at = NOW()
            """),
            {"sub1": sub1, "amount": amount},
        )
        await db.commit()
        await send_message(chat_id, f"✅ Presupuesto de {amount:.2f}€/mes para {sub1} guardado.")
        return

    result = await db.execute(
        text("""
            SELECT b.subcategoria1, b.limit_amount, COALESCE(g.total, 0) AS gastado
            FROM monthly_budgets b
            LEFT JOIN (
                SELECT subcategoria1, SUM(total) AS total
                FROM v_gastos_variables
                WHERE anio = :anio AND mes = :mes
                GROUP BY subcategoria1
            ) g ON LOWER(g.subcategoria1) = LOWER(b.subcategoria1)
            ORDER BY (COALESCE(g.total, 0) / b.limit_amount) DESC
        """),
        {"anio": now.year, "mes": now.month},
    )
    rows = result.all()

    if not rows:
        await send_message(
            chat_id,
            "No hay presupuestos configurados.\n"
            "Usá /presupuesto Supermercado 400 para setear uno.",
        )
        return

    lines = [f"📋 Presupuestos {now.strftime('%B %Y')}", "─────────────────"]
    for r in rows:
        pct = float(r.gastado) / float(r.limit_amount) * 100
        icon = "🔴" if pct >= 100 else "🟡" if pct >= 80 else "🟢"
        lines.append(
            f"{icon} {r.subcategoria1}\n"
            f"   {float(r.gastado):.0f} / {float(r.limit_amount):.0f}€  "
            f"{_budget_bar(pct)}  {pct:.0f}%"
        )

    await send_message(chat_id, "\n".join(lines))


async def _cmd_proyeccion(chat_id: int, db: AsyncSession) -> None:
    now = datetime.now()
    days_elapsed = now.day
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_remaining = days_in_month - days_elapsed

    result = await db.execute(
        text("""
            SELECT
                SUM(CASE WHEN tipo = 'gasto'   THEN total ELSE 0 END) AS gastos,
                SUM(CASE WHEN tipo = 'ingreso' THEN total ELSE 0 END) AS ingresos
            FROM v_resumen_mensual
            WHERE anio = :anio AND mes = :mes
        """),
        {"anio": now.year, "mes": now.month},
    )
    row = result.first()

    gastos = float(row.gastos or 0)
    ingresos = float(row.ingresos or 0)

    if gastos == 0:
        await send_message(chat_id, "No hay gastos registrados este mes para proyectar.")
        return

    daily_rate = gastos / days_elapsed
    projected = daily_rate * days_in_month
    pct_mes = gastos / projected * 100 if projected > 0 else 0

    lines = [
        f"📈 Proyección {now.strftime('%B %Y')}",
        f"─────────────────",
        f"📅 Día {days_elapsed} de {days_in_month}  ({days_remaining} restantes)",
        f"💸 Gastado hasta hoy:  {gastos:>8.2f} €",
        f"📊 Ritmo diario:       {daily_rate:>8.2f} €/día",
        f"─────────────────",
        f"🔮 Proyección a fin de mes: {projected:.2f} €",
        f"   (si seguís a este ritmo)",
    ]

    if ingresos > 0:
        balance_proyectado = ingresos - projected
        emoji = "✅" if balance_proyectado >= 0 else "⚠️"
        lines.append(f"{emoji} Balance proyectado:  {balance_proyectado:>8.2f} €")
        lines.append(f"   (ingresos {ingresos:.2f}€ − proyección {projected:.2f}€)")

    if days_elapsed < 7:
        lines.append(f"\n⚠️ Solo {days_elapsed} días de datos — la proyección puede ser imprecisa.")

    await send_message(chat_id, "\n".join(lines))
