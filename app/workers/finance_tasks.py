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


def _save_transaction(llm_output: LLMTransactionOutput, user_id: str, chat_id: int) -> uuid.UUID:
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

    return tx_id


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
