"""
Expansión de installment_plans en N transactions de tarjeta.

Cada cuota K (1..N) genera una transaction con:
  - account_id = la tarjeta del plan
  - transaction_date = fecha_compra + (K-1) meses (mismo día del mes)
  - fecha_valor      = transaction_date (gasta inmediatamente desde el punto de vista
                       de la tarjeta; la cuenta corriente se debita con el pago del resumen)
  - amount           = monto_cuota
  - categoría heredada del plan
  - installment_plan_id = id del plan
  - origen           = 'automatico'

Idempotente: si el plan ya tiene cuotas generadas, no las duplica (chequea por
(installment_plan_id, fecha_valor) único).
"""
import logging
import uuid
from calendar import monthrange
from datetime import date

from sqlalchemy import text as sa_text

from app.core.database import SyncSessionLocal
from app.models.finance import Transaction

logger = logging.getLogger(__name__)


def _add_months(d: date, n: int) -> date:
    y, m = d.year, d.month + n
    while m > 12:
        y += 1
        m -= 12
    last_day = monthrange(y, m)[1]
    return date(y, m, min(d.day, last_day))


def expand_plan(plan_id: uuid.UUID | str) -> list[uuid.UUID]:
    """Genera todas las cuotas pendientes del plan. Devuelve los IDs creados."""
    created: list[uuid.UUID] = []

    with SyncSessionLocal() as db:
        plan = db.execute(
            sa_text("SELECT * FROM installment_plans WHERE id = :id AND activo"),
            {"id": str(plan_id)},
        ).first()
        if not plan:
            return []

        account = db.execute(
            sa_text("SELECT family_member_id FROM accounts WHERE id = :id"),
            {"id": plan.account_id},
        ).first()
        if not account or account.family_member_id is None:
            logger.warning("Plan %s sobre cuenta sin titular", plan.id)
            return []

        existing_dates = {
            row.fecha_valor
            for row in db.execute(
                sa_text("""
                    SELECT fecha_valor FROM transactions
                    WHERE installment_plan_id = :pid AND deleted_at IS NULL
                """),
                {"pid": str(plan.id)},
            ).all()
        }

        nota_base = plan.descripcion
        for k in range(1, plan.cuotas_total + 1):
            fecha = _add_months(plan.fecha_compra, k - 1)
            if fecha in existing_dates:
                continue
            tx = Transaction(
                family_member_id=account.family_member_id,
                account_id=plan.account_id,
                installment_plan_id=plan.id,
                transaction_date=fecha,
                fecha_valor=fecha,
                tipo="gasto",
                amount=float(plan.monto_cuota),
                currency="EUR",
                categoria=plan.categoria,
                subcategoria1=plan.subcategoria1,
                subcategoria2=plan.subcategoria2,
                nota=f"{nota_base} ({k}/{plan.cuotas_total})",
                origen="automatico",
            )
            db.add(tx)
            db.flush()
            created.append(tx.id)

        db.commit()
        logger.info("Plan %s: %d cuotas generadas", plan.descripcion, len(created))

    return created
