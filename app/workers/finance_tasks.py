"""
Tareas Celery del módulo financiero.
"""
import uuid
import logging
from datetime import date

import redis
from pydantic import ValidationError
from sqlalchemy import text as sa_text

from app.core.config import settings
from app.core.database import SyncSessionLocal
from app.models.finance import Transaction
from app.schemas.finance import LLMTransactionListOutput, LLMTransactionOutput
from app.services import (
    loan_generator,
    notification_dispatcher,
    ollama_client,
    recurring_generator,
    statement_closer,
    telegram_client,
    whisper_client,
)
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_redis = redis.from_url(settings.redis_url, decode_responses=True)
_PENDING_TX_TTL = 300


@celery_app.task(bind=True, max_retries=2, default_retry_delay=5, soft_time_limit=120, time_limit=180)
def process_shopping_message(self, text_msg: str, chat_id: int, user_id: str) -> None:
    """Extrae items de compra del texto vía LLM y los inserta en shopping_list_items."""
    from sqlalchemy import text as sa_t

    items = ollama_client.extract_shopping_items(text_msg)
    if not items:
        telegram_client.send_message_sync(chat_id, "🤔 No encontré items para agregar.")
        return

    inserted = []
    with SyncSessionLocal() as db:
        for it in items:
            r = db.execute(sa_t("""
                INSERT INTO shopping_list_items (texto, cantidad, unidad, created_by)
                VALUES (:t, :c, :u, :cb)
                RETURNING id, texto, cantidad, unidad
            """), {"t": it["texto"], "c": it.get("cantidad"), "u": it.get("unidad"),
                   "cb": uuid.UUID(user_id)}).first()
            inserted.append(r)
        db.commit()

    if not inserted:
        telegram_client.send_message_sync(chat_id, "🤔 No encontré items para agregar.")
        return

    lines = [f"🛒 Agregado{'s' if len(inserted)!=1 else ''} ({len(inserted)}):"]
    for r in inserted:
        cant = f" — {float(r.cantidad):g}{r.unidad or ''}" if r.cantidad else ""
        lines.append(f"  • {r.texto}{cant}")
    telegram_client.send_message_sync(chat_id, "\n".join(lines))


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10, soft_time_limit=120, time_limit=180)
def process_photo_message(
    self,
    file_id: str,
    chat_id: int,
    user_id: str,
    caption: str | None = None,
    file_name: str = "archivo",
    mime_type: str = "application/octet-stream",
) -> None:
    """Descarga la foto/document de Telegram, la persiste como attachment.

    Si hay caption con texto, le pide al LLM que extraiga una transaction
    pendiente (estado_pago='pendiente') y vincula el attachment como 'boleta'.
    Sin caption: queda huérfano para asociación posterior desde la web.
    """
    from sqlalchemy import text as sa_t
    from app.services import attachment_storage

    try:
        content = telegram_client.download_file_sync(file_id)
    except Exception as exc:
        logger.error("No se pudo descargar foto/document chat_id=%s: %s", chat_id, exc)
        telegram_client.send_message_sync(chat_id, "❌ No pude descargar el archivo.")
        return

    # Guardar el attachment (usa session sync directa, sin async)
    from app.core.database import SyncSessionLocal
    saved_id: str | None = None
    with SyncSessionLocal() as db:
        try:
            res = db.execute(sa_t("""
                INSERT INTO attachments
                    (file_path, original_name, mime_type, size_bytes,
                     uploaded_by, uploaded_via, role, notas)
                VALUES (:fp, :on, :mt, :sb, :ub, 'telegram', 'boleta', :n)
                RETURNING id
            """), {"fp": "__pending__", "on": file_name, "mt": mime_type,
                   "sb": len(content), "ub": uuid.UUID(user_id), "n": caption})
            saved_id = str(res.scalar())
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error("INSERT attachment falló: %s", exc)
            telegram_client.send_message_sync(chat_id, "❌ No pude guardar el archivo.")
            return

    # Escribir a disco
    try:
        from datetime import datetime as _dt
        from pathlib import Path
        now = _dt.now()
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "bin"
        ext = "".join(c for c in ext if c.isalnum())[:8] or "bin"
        rel_path = f"{now.year}/{now.month:02d}/{saved_id}.{ext}"
        abs_path = Path("/app/data/files") / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(content)
    except Exception as exc:
        logger.error("Escribir archivo falló: %s", exc)
        telegram_client.send_message_sync(chat_id, "❌ Error escribiendo archivo.")
        return

    # Update file_path
    with SyncSessionLocal() as db:
        db.execute(sa_t("UPDATE attachments SET file_path = :fp WHERE id = :id"),
                   {"fp": rel_path, "id": saved_id})
        db.commit()

    # Si hay caption, intentar extraer transaction pendiente
    if caption and caption.strip():
        try:
            from app.schemas.finance import LLMTransactionListOutput
            wrapper = LLMTransactionListOutput(
                transactions=ollama_client.extract_transactions(caption)
            )
            if wrapper.transactions:
                tx = wrapper.transactions[0]  # tomamos la primera
                tx_id = _save_transaction(tx, user_id, chat_id, estado_pago="pendiente")
                # Asociar el attachment al movimiento creado
                with SyncSessionLocal() as db:
                    db.execute(sa_t("""
                        UPDATE attachments
                        SET entity_type = 'transaction', entity_id = :ei, role = 'boleta'
                        WHERE id = :id
                    """), {"ei": tx_id, "id": saved_id})
                    db.commit()
                telegram_client.send_message_sync(
                    chat_id,
                    f"📎 Boleta guardada y vinculada al movimiento pendiente:\n"
                    f"• {tx.subcategoria1}{(' / ' + tx.subcategoria2) if tx.subcategoria2 else ''} — €{float(tx.amount):,.2f}\n"
                    f"Marcalo como pagado desde la web cuando lo abones."
                )
                return
        except Exception as exc:
            logger.warning("Caption no produjo transaction: %s", exc)

    # Sin caption útil: archivo guardado huérfano
    telegram_client.send_message_sync(
        chat_id,
        f"📎 Archivo guardado ({len(content):,} bytes). Vinculalo a un movimiento desde la web."
    )


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10, soft_time_limit=300, time_limit=360)
def process_audio_message(self, file_id: str, chat_id: int, user_id: str) -> None:
    try:
        audio_bytes = telegram_client.download_file_sync(file_id)
        text = whisper_client.transcribe(audio_bytes)
        logger.info("Transcripción OK chat_id=%s: %r", chat_id, text[:80])
        process_text_message.delay(text, chat_id, user_id)
    except Exception as exc:
        logger.exception("Error en process_audio_message: %s", exc)
        telegram_client.send_message_sync(
            chat_id, "❌ No pude procesar el audio. ¿Podés enviarlo como mensaje de texto?"
        )
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10, soft_time_limit=90, time_limit=120)
def process_text_message(self, text: str, chat_id: int, user_id: str) -> None:
    try:
        transactions = ollama_client.extract_transactions(text)
    except Exception as exc:
        logger.warning("Fallo extracción LLM chat_id=%s: %s", chat_id, exc)
        telegram_client.send_message_sync(
            chat_id, "❌ No pude entender ese movimiento. Intentá con: \"Gasté 40€ en farmacia\"."
        )
        return

    if not transactions:
        telegram_client.send_message_sync(
            chat_id, "❌ No encontré ningún movimiento de dinero en ese mensaje."
        )
        return

    confident = [t for t in transactions if t.confidence >= 0.75]
    uncertain = [t for t in transactions if t.confidence < 0.75]

    if confident:
        saved = [_save_transaction(tx, user_id, chat_id) for tx in confident]
        _send_bulk_confirmation(chat_id, confident, saved)

    if uncertain:
        wrapper = LLMTransactionListOutput(transactions=uncertain)
        _redis.setex(f"pending_tx:{chat_id}", _PENDING_TX_TTL, wrapper.model_dump_json())
        lines = [_format_tx_line(t) for t in uncertain]
        telegram_client.send_message_with_keyboard_sync(
            chat_id,
            "❓ No estoy seguro\n\n" + "\n".join(lines),
            buttons=[[
                {"text": "✓ Guardar", "callback_data": "confirm"},
                {"text": "✗ Cancelar", "callback_data": "cancel"},
            ]],
        )


@celery_app.task(name="app.workers.finance_tasks.generate_loan_installments")
def generate_loan_installments() -> int:
    """Beat-scheduled: genera cuotas de préstamos vencidas hoy. Idempotente."""
    created = loan_generator.generate_due_installments()
    if created:
        logger.info("Cuotas de préstamos generadas: %d", len(created))
    return len(created)


@celery_app.task(name="app.workers.finance_tasks.generate_recurring_charges")
def generate_recurring_charges() -> int:
    """Beat-scheduled: genera transactions de suscripciones recurrentes. Idempotente."""
    created = recurring_generator.generate_due_recurring()
    if created:
        logger.info("Cargos recurrentes generados: %d", len(created))
    return len(created)


@celery_app.task(name="app.workers.finance_tasks.close_card_statements")
def close_card_statements() -> int:
    """Beat-scheduled: cierra y paga resúmenes de tarjeta cuyo vto sea hoy. Idempotente."""
    created = statement_closer.close_due_statements()
    if created:
        logger.info("Resúmenes de tarjeta cerrados: %d", len(created))
    return len(created)


@celery_app.task(name="app.workers.finance_tasks.dispatch_notifications")
def dispatch_notifications() -> dict:
    """Beat-scheduled: cada 5 min lee la cola de notifications y las envía."""
    return notification_dispatcher.send_pending()


@celery_app.task(name="app.workers.finance_tasks.schedule_due_reminders")
def schedule_due_reminders() -> int:
    """Beat-scheduled diario 09:00: encola recordatorios de cosas que vencen
    en ≤2 días (cuotas de préstamo, vencimiento de tarjeta).
    """
    from sqlalchemy import text as sa_t
    from datetime import date, timedelta

    encolados = 0
    with SyncSessionLocal() as db:
        # Préstamos: día de vto en 0/1/2 días
        targets = db.execute(sa_t("""
            SELECT l.id, l.nombre, l.monto_cuota, l.dia_vencimiento, fm.telegram_user_id
            FROM loans l
            JOIN accounts a    ON a.id  = l.cuenta_pago_id
            JOIN family_members fm ON fm.id = a.family_member_id
            WHERE l.activo AND fm.telegram_user_id IS NOT NULL
              AND l.fecha_fin >= CURRENT_DATE
        """)).all()
        today = date.today()
        for t in targets:
            from calendar import monthrange
            day = min(t.dia_vencimiento, monthrange(today.year, today.month)[1])
            target_d = date(today.year, today.month, day)
            if target_d < today:
                # ya pasó este mes → siguiente
                ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
                target_d = date(ny, nm, min(t.dia_vencimiento, monthrange(ny, nm)[1]))
            if (target_d - today).days not in (0, 1, 2):
                continue
            dk = f"loan-{t.id}-{target_d.isoformat()}"
            res = notification_dispatcher.enqueue(
                target_chat_id=t.telegram_user_id,
                kind="reminder",
                title=f"🏦 Cuota de {t.nombre}",
                body=f"€{float(t.monto_cuota):,.2f} se debita el {target_d.strftime('%d/%m')}",
                related_entity_type="loan", related_entity_id=str(t.id),
                dedupe_key=dk,
            )
            if res: encolados += 1

        # Tarjetas: vencimiento próximo
        cards = db.execute(sa_t("""
            SELECT a.id, a.nombre, a.vencimiento_dia, a.cierre_dia, fm.telegram_user_id
            FROM accounts a
            JOIN family_members fm ON fm.id = a.family_member_id
            WHERE a.activa AND a.tipo = 'tarjeta_credito'
              AND a.vencimiento_dia IS NOT NULL
              AND fm.telegram_user_id IS NOT NULL
        """)).all()
        for c in cards:
            from calendar import monthrange
            day = min(c.vencimiento_dia, monthrange(today.year, today.month)[1])
            target_d = date(today.year, today.month, day)
            if target_d < today:
                ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
                target_d = date(ny, nm, min(c.vencimiento_dia, monthrange(ny, nm)[1]))
            if (target_d - today).days not in (0, 1, 2):
                continue
            # Estimación monto del ciclo en curso (gastos no asociados a un statement)
            from datetime import timedelta as td
            cierre_estimado = target_d - td(days=15)  # heurística, refinable
            row = db.execute(sa_t("""
                SELECT COALESCE(SUM(CASE WHEN tipo='gasto' THEN amount ELSE -amount END), 0) AS m
                FROM transactions
                WHERE account_id = :a AND deleted_at IS NULL AND card_statement_id IS NULL
                  AND transaction_date <= CURRENT_DATE
            """), {"a": c.id}).first()
            monto = float(row.m or 0)
            dk = f"card-{c.id}-{target_d.isoformat()}"
            res = notification_dispatcher.enqueue(
                target_chat_id=c.telegram_user_id,
                kind="reminder",
                title=f"💳 Vto {c.nombre}",
                body=f"~€{monto:,.0f} se debitan el {target_d.strftime('%d/%m')}",
                related_entity_type="account", related_entity_id=str(c.id),
                dedupe_key=dk,
            )
            if res: encolados += 1

    if encolados:
        logger.info("Recordatorios encolados: %d", encolados)
    return encolados


@celery_app.task(name="app.workers.finance_tasks.schedule_monthly_summary")
def schedule_monthly_summary() -> int:
    """Beat-scheduled día 1 a las 09:00: encola resumen del mes anterior."""
    from sqlalchemy import text as sa_t
    from datetime import date

    today = date.today()
    if today.month == 1:
        py, pm = today.year - 1, 12
    else:
        py, pm = today.year, today.month - 1

    encolados = 0
    with SyncSessionLocal() as db:
        # Balance del mes anterior
        bal = db.execute(sa_t("""
            SELECT ingresos, gastos, balance, pct_gasto_sobre_ingreso
            FROM v_balance_mensual WHERE anio = :a AND mes = :m
        """), {"a": py, "m": pm}).first()

        # Top 3 gastos del mes anterior
        top = db.execute(sa_t("""
            SELECT subcategoria1, SUM(total) AS total
            FROM v_gastos_variables WHERE anio = :a AND mes = :m
            GROUP BY subcategoria1 ORDER BY total DESC LIMIT 3
        """), {"a": py, "m": pm}).all()

        targets = db.execute(sa_t(
            "SELECT telegram_user_id FROM family_members WHERE telegram_user_id IS NOT NULL"
        )).all()

        nombres_mes = ['Enero','Febrero','Marzo','Abril','Mayo','Junio',
                       'Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre']
        title = f"📊 Resumen — {nombres_mes[pm-1]} {py}"
        if bal:
            ing = float(bal.ingresos or 0)
            gas = float(bal.gastos or 0)
            ahorro = ing - gas
            tasa = (ahorro/ing*100) if ing > 0 else None
            tasa_s = f"{tasa:.1f}%" if tasa is not None else "—"
            body = (
                f"Ingresos: €{ing:,.0f}\n"
                f"Gastos:   €{gas:,.0f}\n"
                f"Balance:  €{ahorro:,.0f}  (tasa ahorro {tasa_s})"
            )
            if top:
                body += "\n\nTop 3 gastos:\n" + "\n".join(
                    f"  • {t.subcategoria1}: €{float(t.total):,.0f}" for t in top
                )
        else:
            body = "Sin datos del mes anterior."

        for t in targets:
            dk = f"summary-{py}-{pm}-{t.telegram_user_id}"
            res = notification_dispatcher.enqueue(
                target_chat_id=t.telegram_user_id,
                kind="monthly_summary",
                title=title, body=body, dedupe_key=dk,
            )
            if res: encolados += 1

    if encolados:
        logger.info("Resúmenes mensuales encolados: %d", encolados)
    return encolados


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5)
def save_pending_transaction(self, chat_id: int, user_id: str, confirmed: bool) -> None:
    pending_json = _redis.get(f"pending_tx:{chat_id}")
    _redis.delete(f"pending_tx:{chat_id}")

    if not confirmed or not pending_json:
        telegram_client.send_message_sync(chat_id, "❌ Cancelado.")
        return

    try:
        wrapper = LLMTransactionListOutput.model_validate_json(pending_json)
    except ValidationError as exc:
        logger.error("JSON inválido pending_tx:%s — %s", chat_id, exc)
        telegram_client.send_message_sync(chat_id, "❌ Ocurrió un error. Intentá de nuevo.")
        return

    saved = [_save_transaction(tx, user_id, chat_id) for tx in wrapper.transactions]
    _send_bulk_confirmation(chat_id, wrapper.transactions, saved)


def _resolve_account(db, user_id: str, medio_pago: str | None, cuenta_hint: str | None) -> tuple[uuid.UUID, str, dict]:
    """Devuelve (account_id, tipo_cuenta, info) según las pistas del LLM.

    Estrategia: pista explícita > medio_pago > default por miembro.
    Devuelve la fila de la cuenta como dict (id, tipo, cierre_dia, vencimiento_dia).
    """
    def _fetch(where: str, params: dict) -> dict | None:
        row = db.execute(sa_text(
            f"SELECT id, tipo, cierre_dia, vencimiento_dia FROM accounts WHERE activa AND {where} LIMIT 1"
        ), params).first()
        return dict(row._mapping) if row else None

    cuenta = None

    if cuenta_hint == "casa":
        cuenta = _fetch("tipo = 'efectivo'", {})
    elif cuenta_hint in ("hector", "luisiana"):
        nombre = "Hector Marioni" if cuenta_hint == "hector" else "Luisiana"
        cuenta = _fetch(
            "tipo = 'corriente' AND family_member_id = (SELECT id FROM family_members WHERE full_name = :n)",
            {"n": nombre},
        )

    if cuenta is None and medio_pago == "tarjeta_credito":
        cuenta = _fetch(
            "tipo = 'tarjeta_credito' AND family_member_id = :uid",
            {"uid": user_id},
        )

    if cuenta is None and medio_pago == "efectivo":
        cuenta = _fetch("tipo = 'efectivo'", {})

    if cuenta is None:
        cuenta = _fetch(
            "tipo = 'corriente' AND family_member_id = :uid",
            {"uid": user_id},
        )

    if cuenta is None:
        raise RuntimeError(f"No se encontró cuenta default para user_id={user_id}")

    return cuenta["id"], cuenta["tipo"], cuenta


def _compute_fecha_valor(transaction_date: date, cuenta: dict) -> date:
    """fecha_valor refleja el momento contable en que la transacción afecta la cuenta.

    Para todas las cuentas (incluida tarjeta de crédito), es la misma fecha de operación.
    En tarjetas, los gastos suben deuda inmediatamente; el descuento de la cuenta corriente
    al pagar el resumen se modela como un evento separado (card_statement) con sus propias
    transactions vinculadas.
    """
    return transaction_date


def _save_transaction(
    llm_output: LLMTransactionOutput,
    user_id: str,
    chat_id: int,
    estado_pago: str | None = None,
) -> uuid.UUID:
    tipo = "ingreso" if llm_output.categoria == "Entradas" else "gasto"

    with SyncSessionLocal() as db:
        account_id, _, cuenta = _resolve_account(
            db, user_id, llm_output.medio_pago, llm_output.cuenta_hint
        )
        fecha_valor = _compute_fecha_valor(llm_output.transaction_date, cuenta)

        tx = Transaction(
            family_member_id=uuid.UUID(user_id),
            account_id=account_id,
            transaction_date=llm_output.transaction_date,
            fecha_valor=fecha_valor,
            tipo=tipo,
            amount=float(llm_output.amount),
            currency=llm_output.currency,
            categoria=llm_output.categoria,
            subcategoria1=llm_output.subcategoria1,
            subcategoria2=llm_output.subcategoria2,
            subcategoria3=llm_output.subcategoria3,
            nota=llm_output.nota,
            estado_pago=estado_pago,
            origen="telegram",
            llm_confidence=llm_output.confidence,
            llm_raw_output=llm_output.model_dump(mode="json"),
        )
        db.add(tx)
        db.commit()
        tx_id = tx.id
        logger.info(
            "Guardado: %s %.2f %s/%s account=%s fecha_valor=%s",
            tipo, tx.amount, tx.subcategoria1, tx.subcategoria2, account_id, fecha_valor,
        )

    # Hooks de notificación (post-commit, no bloquean el guardado si fallan)
    try:
        if tipo == "gasto":
            _maybe_notify_budget(tx_id, chat_id)
            _maybe_notify_anomaly(tx_id, chat_id)
    except Exception as exc:
        logger.warning("Hooks de notificación fallaron tx=%s: %s", tx_id, exc)

    return tx_id


def _maybe_notify_budget(tx_id: uuid.UUID, chat_id: int) -> None:
    """Si la transaction recién creada hace cruzar el 80% o el 100% del presupuesto
    de su subcategoría, encola una notificación.
    """
    with SyncSessionLocal() as db:
        info = db.execute(sa_text("""
            WITH tx AS (
                SELECT subcategoria1, transaction_date FROM transactions WHERE id = :id
            ),
            mes_actual AS (
                SELECT
                    EXTRACT(YEAR  FROM transaction_date)::INT AS y,
                    EXTRACT(MONTH FROM transaction_date)::INT AS m,
                    subcategoria1
                FROM tx
            ),
            gastado AS (
                SELECT SUM(amount) AS total
                FROM transactions, mes_actual
                WHERE transactions.deleted_at IS NULL
                  AND transactions.tipo = 'gasto'
                  AND transactions.subcategoria1 = mes_actual.subcategoria1
                  AND EXTRACT(YEAR  FROM transactions.transaction_date) = mes_actual.y
                  AND EXTRACT(MONTH FROM transactions.transaction_date) = mes_actual.m
            ),
            limite AS (
                SELECT b.limit_amount FROM monthly_budgets b, mes_actual
                WHERE LOWER(b.subcategoria1) = LOWER(mes_actual.subcategoria1)
                LIMIT 1
            )
            SELECT (SELECT total FROM gastado) AS gastado,
                   (SELECT limit_amount FROM limite) AS limite,
                   (SELECT subcategoria1 FROM mes_actual) AS sub1,
                   (SELECT y FROM mes_actual) AS y,
                   (SELECT m FROM mes_actual) AS m
        """), {"id": tx_id}).first()

    if not info or not info.limite or not info.gastado:
        return

    limite = float(info.limite)
    gastado = float(info.gastado)
    pct = gastado / limite * 100
    sub1 = info.sub1

    # Calcular el threshold cruzado (sin la última transaction)
    # Para simplificar: si pct >= 100 o pct >= 80, encolamos con dedupe por mes/threshold
    if pct >= 100:
        threshold = 100
        title = f"⚠️ Presupuesto excedido — {sub1}"
        body = f"Gastaste €{gastado:,.0f} de €{limite:,.0f} ({pct:.0f}%)"
    elif pct >= 80:
        threshold = 80
        title = f"📊 Presupuesto al {pct:.0f}% — {sub1}"
        body = f"Llevás €{gastado:,.0f} de €{limite:,.0f} este mes"
    else:
        return

    notification_dispatcher.enqueue(
        target_chat_id=chat_id,
        kind="budget",
        title=title, body=body,
        related_entity_type="transaction", related_entity_id=str(tx_id),
        dedupe_key=f"budget-{sub1}-{info.y}-{info.m}-{threshold}",
    )


def _maybe_notify_anomaly(tx_id: uuid.UUID, chat_id: int) -> None:
    """Si el monto excede avg+2σ del histórico de su subcategoría (con n>=3),
    encola una alerta de anomalía.
    """
    with SyncSessionLocal() as db:
        info = db.execute(sa_text("""
            WITH tx AS (
                SELECT subcategoria1, amount AS tx_amount FROM transactions WHERE id = :id
            ),
            stats AS (
                SELECT AVG(t2.amount) AS avg_a, STDDEV_POP(t2.amount) AS sd_a
                FROM transactions t2, tx
                WHERE t2.deleted_at IS NULL
                  AND t2.tipo = 'gasto'
                  AND t2.subcategoria1 = tx.subcategoria1
                  AND t2.id <> :id
                  AND t2.transaction_date >= CURRENT_DATE - INTERVAL '6 months'
                HAVING COUNT(*) >= 3
            )
            SELECT (SELECT tx_amount      FROM tx)    AS amount,
                   (SELECT subcategoria1  FROM tx)    AS sub1,
                   (SELECT avg_a          FROM stats) AS avg_a,
                   (SELECT sd_a           FROM stats) AS sd_a
        """), {"id": tx_id}).first()

    if not info or info.avg_a is None or info.sd_a is None:
        return
    amount = float(info.amount)
    avg_a  = float(info.avg_a)
    sd_a   = float(info.sd_a or 0)
    if not (amount > avg_a + 2 * sd_a and amount > avg_a * 1.5):
        return
    excess = (amount - avg_a) / avg_a * 100
    notification_dispatcher.enqueue(
        target_chat_id=chat_id,
        kind="anomaly",
        title=f"🔍 Gasto inusual — {info.sub1}",
        body=f"€{amount:,.0f} ({excess:+.0f}% vs habitual €{avg_a:,.0f})",
        related_entity_type="transaction", related_entity_id=str(tx_id),
        dedupe_key=f"anomaly-{tx_id}",
    )


def _send_bulk_confirmation(
    chat_id: int, transactions: list[LLMTransactionOutput], tx_ids: list[uuid.UUID]
) -> None:
    lines = [_format_confirmation(t) for t in transactions]
    text = "\n".join(lines)
    try:
        if len(transactions) == 1:
            telegram_client.send_message_with_keyboard_sync(
                chat_id,
                text,
                buttons=[[{"text": "↩️ Deshacer", "callback_data": f"undo:{tx_ids[0]}"}]],
            )
        else:
            telegram_client.send_message_sync(chat_id, text)
    except Exception as e:
        logger.warning("No se pudo enviar confirmación a chat_id=%s: %s", chat_id, e)


def _format_tx_line(t: LLMTransactionOutput) -> str:
    parts = [t.subcategoria1]
    if t.subcategoria2:
        parts.append(t.subcategoria2)
    if t.subcategoria3:
        parts.append(t.subcategoria3)
    return f"• {t.amount:.2f} {t.currency} — {' / '.join(parts)}"


def _format_confirmation(t: LLMTransactionOutput) -> str:
    amount_fmt = f"{t.amount:.2f}".rstrip("0").rstrip(".")
    parts = [t.subcategoria1]
    if t.subcategoria2:
        parts.append(t.subcategoria2)
    if t.subcategoria3:
        parts.append(t.subcategoria3)
    return f"✅ {amount_fmt} {t.currency} — {' / '.join(parts)}"
