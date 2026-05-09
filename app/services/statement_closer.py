"""
Cierre y pago automático del resumen de tarjetas de crédito.

Lógica:
1. Para cada tarjeta activa, si HOY es su día de vencimiento (clamped al último
   día del mes) y aún no existe un card_statement con esa fecha_vencimiento:
2. Calcular el ciclo recién cerrado: rango (previous_cierre, last_cierre] de
   fechas de operación.
3. Sumar todos los gastos en la tarjeta dentro de ese rango que no estén ya
   asociados a otro card_statement_id.
4. Crear el card_statement (UNIQUE en account_id+fecha_cierre evita duplicar).
5. Crear el par de transactions:
   - Cuenta corriente: gasto = monto, categoría 'Gastos Fijos' / 'Tarjeta'
   - Tarjeta:          ingreso = monto, categoría 'Buroc'        / 'Pago resumen'
   Ambas tienen card_statement_id apuntando al mismo statement.
"""
import logging
import uuid
from calendar import monthrange
from datetime import date

from sqlalchemy import text as sa_text

from app.core.database import SyncSessionLocal
from app.models.finance import Transaction

logger = logging.getLogger(__name__)


def _clamp_day(year: int, month: int, day: int) -> date:
    last = monthrange(year, month)[1]
    return date(year, month, min(day, last))


def _last_cierre_before(today: date, cierre_dia: int) -> date:
    """Última fecha de cierre estricto anterior a hoy."""
    candidate = _clamp_day(today.year, today.month, cierre_dia)
    if candidate < today:
        return candidate
    # mes anterior
    py, pm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return _clamp_day(py, pm, cierre_dia)


def _previous_cierre(cierre: date) -> date:
    """El cierre del mes anterior al dado."""
    py, pm = (cierre.year - 1, 12) if cierre.month == 1 else (cierre.year, cierre.month - 1)
    return _clamp_day(py, pm, cierre.day)


def close_due_statements(today: date | None = None) -> list[uuid.UUID]:
    """Si hoy es día de vencimiento de alguna tarjeta, cierra y paga el resumen.

    Devuelve los IDs de los card_statements creados.
    """
    today = today or date.today()
    created: list[uuid.UUID] = []

    with SyncSessionLocal() as db:
        cards = db.execute(sa_text("""
            SELECT id, nombre, cierre_dia, vencimiento_dia, cuenta_pago_id, family_member_id
            FROM accounts
            WHERE activa AND tipo = 'tarjeta_credito'
              AND cierre_dia IS NOT NULL AND vencimiento_dia IS NOT NULL
              AND cuenta_pago_id IS NOT NULL
        """)).all()

        for c in cards:
            vto_today = _clamp_day(today.year, today.month, c.vencimiento_dia)
            if vto_today != today:
                continue

            cierre_curr = _last_cierre_before(today, c.cierre_dia)
            cierre_prev = _previous_cierre(cierre_curr)

            existing = db.execute(
                sa_text("SELECT id FROM card_statements WHERE account_id = :a AND fecha_cierre = :fc"),
                {"a": c.id, "fc": cierre_curr},
            ).first()
            if existing:
                continue

            row = db.execute(
                sa_text("""
                    SELECT COALESCE(SUM(
                        CASE WHEN tipo='gasto'   THEN amount
                             WHEN tipo='ingreso' THEN -amount
                             ELSE 0 END
                    ), 0) AS monto
                    FROM transactions
                    WHERE account_id = :aid
                      AND deleted_at IS NULL
                      AND card_statement_id IS NULL
                      AND transaction_date >  :prev
                      AND transaction_date <= :curr
                """),
                {"aid": c.id, "prev": cierre_prev, "curr": cierre_curr},
            ).first()
            monto = float(row.monto or 0)

            if monto <= 0:
                logger.info("Statement %s: nada que cobrar (%.2f)", c.nombre, monto)
                continue

            stmt_result = db.execute(
                sa_text("""
                    INSERT INTO card_statements
                        (account_id, fecha_cierre, fecha_vencimiento, monto, cuenta_pago_id, pagado, pagado_at)
                    VALUES (:a, :fc, :fv, :m, :cp, true, now())
                    RETURNING id
                """),
                {"a": c.id, "fc": cierre_curr, "fv": today, "m": monto, "cp": c.cuenta_pago_id},
            )
            stmt_id = stmt_result.scalar()

            cuenta_pago = db.execute(
                sa_text("SELECT family_member_id FROM accounts WHERE id = :id"),
                {"id": c.cuenta_pago_id},
            ).first()
            if not cuenta_pago or cuenta_pago.family_member_id is None:
                logger.error("Statement %s: cuenta_pago sin titular", c.nombre)
                db.rollback()
                continue

            # Gasto en la cuenta corriente (sale plata real)
            tx_pago = Transaction(
                family_member_id=cuenta_pago.family_member_id,
                account_id=c.cuenta_pago_id,
                card_statement_id=stmt_id,
                transaction_date=today,
                fecha_valor=today,
                tipo="gasto",
                amount=monto,
                currency="EUR",
                categoria="Gastos Fijos",
                subcategoria1="Tarjeta",
                subcategoria2=c.nombre,
                nota=f"Pago resumen {c.nombre}",
                origen="automatico",
            )
            db.add(tx_pago)

            # Ingreso en la tarjeta (cancela deuda)
            tx_cancel = Transaction(
                family_member_id=c.family_member_id,
                account_id=c.id,
                card_statement_id=stmt_id,
                transaction_date=today,
                fecha_valor=today,
                tipo="ingreso",
                amount=monto,
                currency="EUR",
                categoria="Buroc",
                subcategoria1="Pago resumen",
                subcategoria2=c.nombre,
                nota=f"Pago resumen {c.nombre}",
                origen="automatico",
            )
            db.add(tx_cancel)

            # Marcar las transactions del ciclo como ya cobradas (statement_id)
            db.execute(
                sa_text("""
                    UPDATE transactions
                    SET card_statement_id = :sid
                    WHERE account_id = :aid
                      AND deleted_at IS NULL
                      AND card_statement_id IS NULL
                      AND transaction_date >  :prev
                      AND transaction_date <= :curr
                """),
                {"sid": stmt_id, "aid": c.id, "prev": cierre_prev, "curr": cierre_curr},
            )

            db.commit()
            created.append(stmt_id)
            logger.info(
                "Statement cerrado: %s ciclo (%s, %s] monto=%.2f vto=%s",
                c.nombre, cierre_prev, cierre_curr, monto, today,
            )

    return created
