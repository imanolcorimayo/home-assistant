"""Outbound notification dispatcher — sends pending rows via the Observer bot.

Generators write to `notification` (with a dedupe_key so they're idempotent);
this module reads pending rows and ships them. Split intentionally:
- The dispatcher is dumb. No deciding, just sending.
- If Telegram is down, the row stays pending and the next dispatcher tick
  retries — no in-process retry loops, no lost work.
- Sender token is fixed (OBSERVER_TELEGRAM_BOT_TOKEN); the Observer is the
  only bot that sends unsolicited messages.
"""

import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.database import AsyncSessionLocal
from app.models import Notification
from app.services.telegram import send_message

log = logging.getLogger("notifications")

# Conservative batch size — avoids long-running DB sessions and limits the
# blast radius if a transient Telegram error happens on row N of N+M.
_DISPATCH_BATCH = 50


def _observer_token() -> str | None:
    """Return the Observer bot token or None if not configured. Returning
    None lets the dispatcher no-op cleanly during local setup where the
    third bot hasn't been provisioned yet (instead of raising on every tick)."""
    return os.environ.get("OBSERVER_TELEGRAM_BOT_TOKEN") or None


async def dispatch_pending_notifications() -> dict:
    """Send up to `_DISPATCH_BATCH` notifications whose `scheduled_ts` has
    arrived. Each send marks `sent_ts` on success or `error` on failure;
    the row stays for the next tick to retry in either case (error rows are
    retried — best-effort. If you need 'stop retrying after N attempts',
    extend with a counter column).

    Returns {sent, failed, skipped_no_token} for observability."""
    token = _observer_token()
    if not token:
        log.warning("dispatch_pending_notifications: OBSERVER_TELEGRAM_BOT_TOKEN not set; skipping")
        return {"sent": 0, "failed": 0, "skipped_no_token": True}

    sent = 0
    failed = 0
    async with AsyncSessionLocal() as session:
        rows = (
            await session.scalars(
                select(Notification)
                .where(
                    Notification.sent_ts.is_(None),
                    Notification.scheduled_ts <= datetime.now(timezone.utc),
                )
                .order_by(Notification.scheduled_ts)
                .limit(_DISPATCH_BATCH)
            )
        ).all()

        for n in rows:
            text = n.title if not n.body else f"{n.title}\n\n{n.body}"
            try:
                resp = await send_message(n.target_chat_id, text, token=token)
                if not resp:
                    # send_message returns {} on 4xx/5xx and only logs — treat
                    # as a soft failure so we retry next tick.
                    raise RuntimeError("send_message returned empty response (see telegram log)")
                await session.execute(
                    update(Notification)
                    .where(Notification.notification_id == n.notification_id)
                    .values(sent_ts=datetime.now(timezone.utc), error=None)
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001 - log + persist, never raise from dispatch
                log.exception("notification %s send failed", n.notification_id)
                await session.execute(
                    update(Notification)
                    .where(Notification.notification_id == n.notification_id)
                    .values(error=str(exc)[:500])
                )
                failed += 1
        await session.commit()

    if sent or failed:
        log.info("dispatch: sent=%d failed=%d", sent, failed)
    return {"sent": sent, "failed": failed, "skipped_no_token": False}
