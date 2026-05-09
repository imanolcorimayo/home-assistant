"""
Generación automática de cuotas de préstamos.

Idempotente: cada cuota se identifica por (loan_id, año, mes) y solo se inserta
si no existe ya una transaction con esos datos.

Solo procesa el mes en curso — si el job estuvo caído un mes entero,
la cuota faltante NO se genera retroactivamente desde aquí (se debe regenerar
manualmente). Esto evita re-debits accidentales si alguien cambia fecha_inicio.
"""
import logging
import uuid
from calendar import monthrange
from datetime import date

from sqlalchemy import text as sa_text

from app.core.database import SyncSessionLocal
from app.models.finance import Transaction

logger = logging.getLogger(__name__)


def generate_due_installments(today: date | None = None) -> list[uuid.UUID]:
    """Genera las cuotas de préstamos cuyo vencimiento es del mes en curso y ya pasó.

    Devuelve los IDs de las transactions creadas (vacío si no había nada que generar).
    """
    today = today or date.today()
    created: list[uuid.UUID] = []

    with SyncSessionLocal() as db:
        loans = db.execute(sa_text("SELECT * FROM loans WHERE activo")).all()

        for loan in loans:
            year, month = today.year, today.month
            last_day = monthrange(year, month)[1]
            day = min(loan.dia_vencimiento, last_day)
            fecha_valor = date(year, month, day)

            # Aún no llegó el día del mes
            if fecha_valor > today:
                continue
            # Fuera del rango del préstamo
            if fecha_valor < loan.fecha_inicio or fecha_valor > loan.fecha_fin:
                continue

            exists = db.execute(
                sa_text("""
                    SELECT 1 FROM transactions
                    WHERE loan_id = :loan_id AND deleted_at IS NULL
                      AND EXTRACT(YEAR  FROM fecha_valor) = :y
                      AND EXTRACT(MONTH FROM fecha_valor) = :m
                    LIMIT 1
                """),
                {"loan_id": loan.id, "y": year, "m": month},
            ).first()
            if exists:
                continue

            account = db.execute(
                sa_text("SELECT family_member_id FROM accounts WHERE id = :id"),
                {"id": loan.cuenta_pago_id},
            ).first()
            if not account or account.family_member_id is None:
                logger.warning("Préstamo %s apunta a cuenta sin titular — skip", loan.nombre)
                continue

            is_last = (year == loan.fecha_fin.year and month == loan.fecha_fin.month)
            if is_last and loan.monto_ultima_cuota is not None:
                amount = float(loan.monto_ultima_cuota)
            else:
                amount = float(loan.monto_cuota)

            tx = Transaction(
                family_member_id=account.family_member_id,
                account_id=loan.cuenta_pago_id,
                loan_id=loan.id,
                transaction_date=fecha_valor,
                fecha_valor=fecha_valor,
                tipo="gasto",
                amount=amount,
                currency="EUR",
                categoria="Gastos Fijos",
                subcategoria1="Prestamos",
                subcategoria2=loan.nombre,
                nota=f"Cuota automática — {loan.nombre}",
                origen="automatico",
            )
            db.add(tx)
            db.flush()
            created.append(tx.id)
            logger.info("Cuota generada: %s €%.2f fecha=%s", loan.nombre, amount, fecha_valor)

        db.commit()

    return created
