"""Persist parsed transactions to the DB.

MVP scope: pick the first family_member and the first account. Routing
by sender wa_id and by account_hint is a follow-up — those need either
a wa_id column on family_member or a fuzzy match on account.name.
"""

import logging
from datetime import date as date_cls
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import (
    Account,
    FamilyMember,
    Transaction,
    TransactionKind,
    TransactionSource,
)

log = logging.getLogger("transactions")

# Maps the LLM's coarse confidence labels to the numeric column. Schema
# constrains llm_confidence to [0, 1]; these midpoints are good enough
# for filtering ("show me low-confidence rows") without false precision.
CONFIDENCE_MAP = {"high": Decimal("0.9"), "medium": Decimal("0.6"), "low": Decimal("0.3")}

# Placeholder until the parser extracts a category. The schema requires
# category NOT NULL, so we land everything here and fix it in a follow-up.
DEFAULT_CATEGORY = "sin_categoria"


def _parse_amount(raw) -> Optional[Decimal]:
    if raw is None:
        return None
    try:
        amt = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return amt if amt > 0 else None


def _parse_date(raw) -> Optional[date_cls]:
    if not raw:
        return None
    try:
        return date_cls.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


async def _pick_default_ids(session: AsyncSession) -> Optional[tuple]:
    member_id = await session.scalar(select(FamilyMember.family_member_id).limit(1))
    account_id = await session.scalar(select(Account.account_id).limit(1))
    if member_id is None or account_id is None:
        log.error("cannot save transaction: missing seed (family_member=%s account=%s)",
                  member_id, account_id)
        return None
    return member_id, account_id


async def save_parsed(parsed: dict, sender_wa_id: str) -> Optional[str]:
    """Insert one row in `transaction` from a Gemini-parsed payload.

    Returns the new transaction_id (str) on success, None if skipped or failed.
    Skips when kind is missing or amount is non-positive — those rows are
    typically the 'this isn't a transaction' or low-confidence misfires.
    """
    kind_raw = parsed.get("kind")
    if kind_raw not in ("expense", "income"):
        log.info("skip save: kind=%r not actionable", kind_raw)
        return None

    amount = _parse_amount(parsed.get("amount"))
    if amount is None:
        log.info("skip save: amount missing/invalid raw=%r", parsed.get("amount"))
        return None

    confidence_label = parsed.get("confidence")
    if confidence_label == "low":
        log.info("skip save: confidence=low — would prefer to ask user, MVP just drops")
        return None

    async with AsyncSessionLocal() as session:
        defaults = await _pick_default_ids(session)
        if defaults is None:
            return None
        member_id, account_id = defaults

        tx_date = _parse_date(parsed.get("transaction_date")) or date_cls.today()

        tx = Transaction(
            account_id=account_id,
            family_member_id=member_id,
            kind=TransactionKind(kind_raw),
            amount=amount,
            currency="ARS",
            category=DEFAULT_CATEGORY,
            description=parsed.get("description"),
            transaction_date=tx_date,
            value_date=tx_date,
            source=TransactionSource("whatsapp"),
            llm_confidence=CONFIDENCE_MAP.get(confidence_label),
            llm_raw_output={**parsed, "_sender_wa_id": sender_wa_id},
        )
        session.add(tx)
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            log.exception("transaction insert failed: %s", exc)
            return None

        log.info("saved transaction id=%s kind=%s amount=%s desc=%r",
                 tx.transaction_id, kind_raw, amount, parsed.get("description"))
        return str(tx.transaction_id)
