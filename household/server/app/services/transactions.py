"""Persist parsed transactions to the DB.

MVP scope: pick the first family_member and the first account. Routing
by sender wa_id and by account_hint is a follow-up — those need either
a wa_id column on family_member or a fuzzy match on account.name.
"""

import logging
import uuid
from datetime import date as date_cls
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import (
    Account,
    Category,
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

# Where uncategorized rows land. Must match a seeded row in the category table.
DEFAULT_CATEGORY = "Sin categoría"


async def active_category_names() -> list[str]:
    """Names the parser offers Gemini to choose from. Excludes the
    'Sin categoría' fallback — that's reserved for 'none of these apply'."""
    async with AsyncSessionLocal() as session:
        rows = await session.scalars(
            select(Category.name)
            .where(Category.is_active, Category.name != DEFAULT_CATEGORY)
            .order_by(Category.name)
        )
        return list(rows)


async def recent_transactions(
    limit: int = 20, days: int = 90, term: Optional[str] = None, kind: Optional[str] = None
) -> list[dict]:
    """Recent (non-deleted) transactions, newest first.

    kind: 'expense', 'income', or None for both. Backs the agent's pre-fed list
    (expenses only) and the look_up_transactions tool (any kind).

    Each row carries `kind` and `recurring_charge_id` (set when the row is a
    recurring payment), so income and recurring charges are visible too.
    """
    since = date_cls.today() - timedelta(days=max(days, 1))
    async with AsyncSessionLocal() as session:
        q = select(Transaction).where(
            Transaction.deleted_ts.is_(None),
            Transaction.transaction_date >= since,
        )
        if kind in ("expense", "income"):
            q = q.where(Transaction.kind == TransactionKind(kind))
        if term:
            like = f"%{term.strip()}%"
            q = q.where(or_(Transaction.description.ilike(like), Transaction.category.ilike(like)))
        q = q.order_by(Transaction.transaction_date.desc()).limit(min(max(limit, 1), 50))
        rows = (await session.scalars(q)).all()
        return [
            {
                # id lets the agent target a row with edit_expense; the pre-fed
                # prompt list drops it (see _format_recent), look_up keeps it.
                "id": str(t.transaction_id),
                "kind": t.kind.value,
                "date": str(t.transaction_date),
                "amount": float(t.amount),
                "currency": t.currency,
                "category": t.category,
                "description": t.description,
                "recurring_charge_id": (
                    str(t.recurring_charge_id) if t.recurring_charge_id else None
                ),
            }
            for t in rows
        ]


async def recent_expenses(
    limit: int = 20, days: int = 90, term: Optional[str] = None
) -> list[dict]:
    """Recent expenses only — what the agent pre-feeds into its prompt to judge
    duplicates/categories. Thin wrapper over recent_transactions(kind='expense')."""
    return await recent_transactions(limit=limit, days=days, term=term, kind="expense")


async def _resolve_category(session: AsyncSession, name: Optional[str]) -> tuple[str, Optional[object]]:
    """Map a (possibly fuzzy) category name to (canonical_name, category_id).
    Falls back to DEFAULT_CATEGORY when the name is missing/unknown."""
    if name:
        cat = await session.scalar(
            select(Category).where(
                func.lower(Category.name) == name.strip().lower(), Category.is_active
            )
        )
        if cat:
            return cat.name, cat.category_id
    cat = await session.scalar(select(Category).where(Category.name == DEFAULT_CATEGORY))
    return (cat.name, cat.category_id) if cat else (DEFAULT_CATEGORY, None)


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


async def _pick_defaults(session: AsyncSession) -> Optional[tuple]:
    # MVP: no per-sender routing yet, so fall back to the oldest active
    # account/member. Deterministic (LIMIT 1 with no ORDER BY is arbitrary).
    member_id = await session.scalar(
        select(FamilyMember.family_member_id)
        .where(FamilyMember.is_active)
        .order_by(FamilyMember.created_ts)
        .limit(1)
    )
    account = await session.scalar(
        select(Account).where(Account.is_active).order_by(Account.created_ts).limit(1)
    )
    if member_id is None or account is None:
        log.error("cannot save transaction: missing seed (family_member=%s account=%s)",
                  member_id, account)
        return None
    return member_id, account


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
        defaults = await _pick_defaults(session)
        if defaults is None:
            return None
        member_id, account = defaults

        tx_date = _parse_date(parsed.get("transaction_date")) or date_cls.today()
        category_name, category_id = await _resolve_category(session, parsed.get("category"))

        tx = Transaction(
            account_id=account.account_id,
            family_member_id=member_id,
            kind=TransactionKind(kind_raw),
            amount=amount,
            currency=account.currency,
            category=category_name,
            category_id=category_id,
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


async def create_transaction(
    kind: str,
    amount,
    description: Optional[str] = None,
    category: Optional[str] = None,
    transaction_date: Optional[str] = None,
    source: str = "manual",
) -> Optional[str]:
    """Insert one transaction from explicit fields (used by the MCP add_transaction tool).

    Unlike save_parsed there's no confidence gating — the caller already
    decided to write. Account/member default to the oldest active; currency
    comes from the account; category resolves to category_id (or fallback).
    Returns the new transaction_id, or None on validation/DB failure.
    """
    if kind not in ("expense", "income"):
        log.info("create_transaction: bad kind=%r", kind)
        return None
    amt = _parse_amount(amount)
    if amt is None:
        log.info("create_transaction: bad amount=%r", amount)
        return None
    try:
        src = TransactionSource(source)
    except ValueError:
        src = TransactionSource.manual

    async with AsyncSessionLocal() as session:
        defaults = await _pick_defaults(session)
        if defaults is None:
            return None
        member_id, account = defaults
        tx_date = _parse_date(transaction_date) or date_cls.today()
        category_name, category_id = await _resolve_category(session, category)

        tx = Transaction(
            account_id=account.account_id,
            family_member_id=member_id,
            kind=TransactionKind(kind),
            amount=amt,
            currency=account.currency,
            category=category_name,
            category_id=category_id,
            description=description,
            transaction_date=tx_date,
            value_date=tx_date,
            source=src,
        )
        session.add(tx)
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            log.exception("create_transaction insert failed: %s", exc)
            return None

        log.info("created transaction id=%s kind=%s amount=%s", tx.transaction_id, kind, amt)
        return str(tx.transaction_id)


async def update_transaction(
    transaction_id: str,
    amount=None,
    description: Optional[str] = None,
    category: Optional[str] = None,
    transaction_date: Optional[str] = None,
) -> Optional[str]:
    """Patch an existing (non-deleted) transaction by id (used by the MCP
    edit_expense tool). Only the fields passed are touched. Returns the
    transaction_id on success, None if the row is missing/deleted or a value
    is invalid."""
    try:
        tid = uuid.UUID(str(transaction_id))
    except (ValueError, TypeError, AttributeError):
        log.info("update_transaction: bad id=%r", transaction_id)
        return None

    async with AsyncSessionLocal() as session:
        tx = await session.get(Transaction, tid)
        if tx is None or tx.deleted_ts is not None:
            log.info("update_transaction: not found/deleted id=%s", transaction_id)
            return None

        if amount is not None:
            amt = _parse_amount(amount)
            if amt is None:
                log.info("update_transaction: bad amount=%r", amount)
                return None
            tx.amount = amt
        if description is not None:
            tx.description = description
        if category is not None:
            tx.category, tx.category_id = await _resolve_category(session, category)
        if transaction_date is not None:
            d = _parse_date(transaction_date)
            if d is None:
                log.info("update_transaction: bad date=%r", transaction_date)
                return None
            tx.transaction_date = d
            tx.value_date = d

        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            log.exception("update_transaction failed: %s", exc)
            return None

        log.info("updated transaction id=%s", tx.transaction_id)
        return str(tx.transaction_id)
