"""
Generación de transactions de suscripciones recurrentes.

Por cada suscripción activa, si el día_mes ya pasó este mes (CURRENT_DATE >= dia_mes
clamped al último día del mes), inserta una transaction.

Idempotente: cada cargo se identifica por (recurring_charge_id, año, mes).
"""
import logging
import uuid
from calendar import monthrange
from datetime import date

from sqlalchemy import text as sa_text

from app.core.database import SyncSessionLocal
from app.models.finance import Transaction

logger = logging.getLogger(__name__)


def generate_due_recurring(today: date | None = None) -> list[uuid.UUID]:
    today = today or date.today()
    created: list[uuid.UUID] = []

    with SyncSessionLocal() as db:
        items = db.execute(sa_text("SELECT * FROM recurring_charges WHERE activo")).all()

        for r in items:
            year, month = today.year, today.month
            last_day = monthrange(year, month)[1]
            day = min(r.dia_mes, last_day)
            fecha_valor = date(year, month, day)

            # Aún no llegó el día
            if fecha_valor > today:
                continue
            # Fuera del rango de la suscripción
            if r.fecha_inicio and fecha_valor < r.fecha_inicio:
                continue
            if r.fecha_fin and fecha_valor > r.fecha_fin:
                continue

            exists = db.execute(
                sa_text("""
                    SELECT 1 FROM transactions
                    WHERE recurring_charge_id = :rid AND deleted_at IS NULL
                      AND EXTRACT(YEAR  FROM fecha_valor) = :y
                      AND EXTRACT(MONTH FROM fecha_valor) = :m
                    LIMIT 1
                """),
                {"rid": r.id, "y": year, "m": month},
            ).first()
            if exists:
                continue

            account = db.execute(
                sa_text("SELECT family_member_id FROM accounts WHERE id = :id"),
                {"id": r.account_id},
            ).first()
            if not account or account.family_member_id is None:
                logger.warning("Suscripción %s sobre cuenta sin titular — skip", r.nombre)
                continue

            tx = Transaction(
                family_member_id=account.family_member_id,
                account_id=r.account_id,
                recurring_charge_id=r.id,
                transaction_date=fecha_valor,
                fecha_valor=fecha_valor,
                tipo="gasto",
                amount=float(r.monto),
                currency="EUR",
                categoria=r.categoria,
                subcategoria1=r.subcategoria1,
                subcategoria2=r.subcategoria2,
                nota=f"Suscripción — {r.nombre}",
                origen="automatico",
            )
            db.add(tx)
            db.flush()
            created.append(tx.id)
            logger.info("Recurring generado: %s €%.2f fecha=%s", r.nombre, float(r.monto), fecha_valor)

        db.commit()

    return created
