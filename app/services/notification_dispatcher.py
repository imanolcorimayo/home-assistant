"""
Dispatcher de notificaciones encoladas en la tabla `notifications`.

Lógica:
1. Lee las que tengan scheduled_at <= now() AND sent_at IS NULL.
2. Para cada una: chequea user_preferences (kind+telegram_user_id). Si está
   deshabilitada → la marca como "skipped" (sent_at=now, error='skipped:disabled').
3. Si está habilitada: envía por Telegram, marca sent_at=now() en éxito,
   o setea error y deja sent_at NULL para reintento.

Idempotencia: una notification con sent_at != NULL no se reenvía. Reintentos
son responsabilidad del beat (cada 5 min va a volver a intentar las que tengan
error pero sent_at NULL — para evitar loops infinitos, registramos error pero
no reintentamos automáticamente: si quedó marcada con sent_at=NULL después
de 1 fallo, igual se intentará en el próximo tick).
"""
import logging
import uuid

from sqlalchemy import text as sa_text

from app.core.database import SyncSessionLocal
from app.services import telegram_client

logger = logging.getLogger(__name__)


def send_pending(now_ts: str | None = None) -> dict:
    """Despacha notifications pendientes. Devuelve {sent, skipped, failed}."""
    sent = skipped = failed = 0

    with SyncSessionLocal() as db:
        rows = db.execute(sa_text("""
            SELECT id, target_chat_id, kind, title, body
            FROM notifications
            WHERE sent_at IS NULL
              AND scheduled_at <= now()
            ORDER BY scheduled_at
            LIMIT 50
        """)).all()

        for n in rows:
            pref = db.execute(sa_text("""
                SELECT enabled FROM user_preferences
                WHERE telegram_user_id = :uid AND kind = :k
                LIMIT 1
            """), {"uid": n.target_chat_id, "k": n.kind}).first()
            enabled = (pref is None) or pref.enabled  # Default: habilitado si no hay pref

            if not enabled:
                db.execute(sa_text("""
                    UPDATE notifications SET sent_at = now(), error = 'skipped:disabled'
                    WHERE id = :id
                """), {"id": n.id})
                skipped += 1
                continue

            text = f"<b>{n.title}</b>\n{n.body}" if n.title else n.body
            try:
                telegram_client.send_message_sync(n.target_chat_id, text, parse_mode="HTML")
                db.execute(sa_text("UPDATE notifications SET sent_at = now(), error = NULL WHERE id = :id"),
                           {"id": n.id})
                sent += 1
            except Exception as exc:
                logger.warning("Notif %s falló: %s", n.id, exc)
                db.execute(sa_text("UPDATE notifications SET error = :e WHERE id = :id"),
                           {"id": n.id, "e": str(exc)[:500]})
                failed += 1

        db.commit()

    if sent + skipped + failed > 0:
        logger.info("Notificaciones: enviadas=%d skipped=%d fallidas=%d", sent, skipped, failed)
    return {"sent": sent, "skipped": skipped, "failed": failed}


def enqueue(
    target_chat_id: int,
    kind: str,
    title: str,
    body: str,
    *,
    scheduled_at_sql: str = "now()",
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
    dedupe_key: str | None = None,
) -> uuid.UUID | None:
    """Inserta una notification en la cola.

    Si dedupe_key colisiona (UNIQUE), no inserta y devuelve None — útil para
    no spamear la misma alerta varias veces.
    """
    with SyncSessionLocal() as db:
        try:
            result = db.execute(sa_text(f"""
                INSERT INTO notifications
                    (target_chat_id, kind, title, body, scheduled_at,
                     related_entity_type, related_entity_id, dedupe_key)
                VALUES (:cid, :k, :t, :b, {scheduled_at_sql}, :ret, :rei, :dk)
                ON CONFLICT (dedupe_key) DO NOTHING
                RETURNING id
            """), {
                "cid": target_chat_id, "k": kind, "t": title, "b": body,
                "ret": related_entity_type, "rei": related_entity_id, "dk": dedupe_key,
            })
            row = result.first()
            db.commit()
            return row.id if row else None
        except Exception as exc:
            db.rollback()
            logger.warning("enqueue notif falló (%s/%s): %s", kind, dedupe_key, exc)
            return None
