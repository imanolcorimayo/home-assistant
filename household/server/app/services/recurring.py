"""Recurring-charge tools for the registrar agent.

A `recurring_charge` is the *definition* of a periodic payment (Netflix €12 on
the 5th, rent, etc.). It is NOT the payments themselves. A charge counts as
"paid" for a given month not by a stored flag but by the existence of a
transaction linked to it (recurring_charge_id) dated in that month — so
pay_recurring_charge inserts that linked transaction, and list_recurring_charges
derives paid/unpaid from it.

Account/member default to the oldest active ones (same MVP stance as
transactions._pick_defaults — no per-sender routing yet).
"""

import logging
import uuid
from datetime import date as date_cls
from typing import Optional

from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models import (
    Account,
    RecurringCharge,
    Transaction,
    TransactionKind,
    TransactionSource,
)
from app.services.transactions import (
    _parse_amount,
    _parse_date,
    _pick_defaults,
    _resolve_category,
)

log = logging.getLogger("recurring")


def _coerce_dom(raw) -> Optional[int]:
    """day_of_month must be 1..31 (DB check constraint); None if invalid."""
    try:
        dom = int(raw)
    except (TypeError, ValueError):
        return None
    return dom if 1 <= dom <= 31 else None


async def create_recurring_charge(
    name: str,
    amount,
    day_of_month,
    category: Optional[str] = None,
    subcategory_1: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """Define a new recurring charge. Returns {"ok", "recurring_charge_id"} or
    {"ok": False, "error"}. This stores the definition only — it does not
    register any payment (that's pay_recurring_charge)."""
    amt = _parse_amount(amount)
    if amt is None:
        return {"ok": False, "error": "monto inválido (debe ser > 0)"}
    dom = _coerce_dom(day_of_month)
    if dom is None:
        return {"ok": False, "error": "day_of_month debe ser un número entre 1 y 31"}

    async with AsyncSessionLocal() as session:
        defaults = await _pick_defaults(session)
        if defaults is None:
            return {"ok": False, "error": "no hay cuenta/miembro activo"}
        _member_id, account = defaults
        cat_name, _cat_id = await _resolve_category(session, category)

        rc = RecurringCharge(
            account_id=account.account_id,
            name=name,
            amount=amt,
            day_of_month=dom,
            category=cat_name,
            subcategory_1=subcategory_1,
            start_date=_parse_date(start_date) or date_cls.today(),
            end_date=_parse_date(end_date),
        )
        session.add(rc)
        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            log.exception("create_recurring_charge failed: %s", exc)
            return {"ok": False, "error": "no se pudo crear el recurrente"}

        log.info("created recurring_charge id=%s name=%r amount=%s dom=%s",
                 rc.recurring_charge_id, name, amt, dom)
        return {"ok": True, "recurring_charge_id": str(rc.recurring_charge_id)}


async def update_recurring_charge(
    recurring_charge_id: str,
    name: Optional[str] = None,
    amount=None,
    day_of_month=None,
    category: Optional[str] = None,
    end_date: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> dict:
    """Edit a recurring charge's definition. Only fields passed are changed.
    Set is_active=False to stop it. Returns {"ok", "recurring_charge_id"} or
    {"ok": False, "error"}."""
    try:
        rid = uuid.UUID(str(recurring_charge_id))
    except (ValueError, TypeError, AttributeError):
        return {"ok": False, "error": "id inválido"}

    async with AsyncSessionLocal() as session:
        rc = await session.get(RecurringCharge, rid)
        if rc is None:
            return {"ok": False, "error": "no existe ese recurrente"}

        if name is not None:
            rc.name = name
        if amount is not None:
            amt = _parse_amount(amount)
            if amt is None:
                return {"ok": False, "error": "monto inválido (debe ser > 0)"}
            rc.amount = amt
        if day_of_month is not None:
            dom = _coerce_dom(day_of_month)
            if dom is None:
                return {"ok": False, "error": "day_of_month debe ser un número entre 1 y 31"}
            rc.day_of_month = dom
        if category is not None:
            rc.category, _cat_id = await _resolve_category(session, category)
        if end_date is not None:
            rc.end_date = _parse_date(end_date)
        if is_active is not None:
            rc.is_active = bool(is_active)

        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            log.exception("update_recurring_charge failed: %s", exc)
            return {"ok": False, "error": "no se pudo actualizar el recurrente"}

        log.info("updated recurring_charge id=%s", rc.recurring_charge_id)
        return {"ok": True, "recurring_charge_id": str(rc.recurring_charge_id)}


async def list_recurring_charges(include_inactive: bool = False) -> list[dict]:
    """List recurring charges (active by default), each annotated with whether
    it's already been paid THIS calendar month — derived from a linked
    transaction existing in the month, not from any stored flag."""
    today = date_cls.today()
    month_start = today.replace(day=1)
    async with AsyncSessionLocal() as session:
        q = select(RecurringCharge)
        if not include_inactive:
            q = q.where(RecurringCharge.is_active)
        charges = (await session.scalars(q.order_by(RecurringCharge.day_of_month))).all()
        if not charges:
            return []

        rows = await session.execute(
            select(
                Transaction.recurring_charge_id,
                func.max(Transaction.transaction_date),
            )
            .where(
                Transaction.recurring_charge_id.in_([c.recurring_charge_id for c in charges]),
                Transaction.deleted_ts.is_(None),
                Transaction.transaction_date >= month_start,
            )
            .group_by(Transaction.recurring_charge_id)
        )
        paid = {rid: d for rid, d in rows.all()}

        return [
            {
                "id": str(c.recurring_charge_id),
                "name": c.name,
                "amount": float(c.amount),
                "day_of_month": c.day_of_month,
                "category": c.category,
                "is_active": c.is_active,
                "paid_this_month": c.recurring_charge_id in paid,
                "last_paid_date": (
                    str(paid[c.recurring_charge_id]) if c.recurring_charge_id in paid else None
                ),
            }
            for c in charges
        ]


async def pay_recurring_charge(
    recurring_charge_id: str,
    transaction_date: Optional[str] = None,
    amount=None,
) -> dict:
    """Register the payment of a recurring charge by inserting a transaction
    linked to it — that linked transaction is what marks it paid for the month.
    amount defaults to the charge's amount; date to today. Returns
    {"ok", "transaction_id", "already_paid_this_month"} or {"ok": False, "error"}.
    `already_paid_this_month` is True when a payment for that month already
    existed (the agent should normally confirm before double-paying)."""
    try:
        rid = uuid.UUID(str(recurring_charge_id))
    except (ValueError, TypeError, AttributeError):
        return {"ok": False, "error": "id inválido"}

    async with AsyncSessionLocal() as session:
        rc = await session.get(RecurringCharge, rid)
        if rc is None:
            return {"ok": False, "error": "no existe ese recurrente"}
        defaults = await _pick_defaults(session)
        if defaults is None:
            return {"ok": False, "error": "no hay cuenta/miembro activo"}
        member_id, _account = defaults

        tx_date = _parse_date(transaction_date) or date_cls.today()
        amt = _parse_amount(amount) if amount is not None else rc.amount
        if amt is None:
            return {"ok": False, "error": "monto inválido (debe ser > 0)"}

        # Was this charge already paid in tx_date's month? (informational —
        # we still insert, but tell the agent so it can warn the user.)
        month_start = tx_date.replace(day=1)
        existing = await session.scalar(
            select(Transaction.transaction_id)
            .where(
                Transaction.recurring_charge_id == rid,
                Transaction.deleted_ts.is_(None),
                Transaction.transaction_date >= month_start,
            )
            .limit(1)
        )
        already_paid = existing is not None

        charge_account = await session.get(Account, rc.account_id)
        cat_name, cat_id = await _resolve_category(session, rc.category)

        tx = Transaction(
            account_id=rc.account_id,
            family_member_id=member_id,
            recurring_charge_id=rid,
            kind=TransactionKind.expense,
            amount=amt,
            currency=charge_account.currency if charge_account else "EUR",
            category=cat_name,
            category_id=cat_id,
            subcategory_1=rc.subcategory_1,
            description=rc.name,
            transaction_date=tx_date,
            value_date=tx_date,
            source=TransactionSource.recurring,
        )
        session.add(tx)
        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            log.exception("pay_recurring_charge failed: %s", exc)
            return {"ok": False, "error": "no se pudo registrar el pago"}

        log.info("paid recurring_charge id=%s -> tx=%s amount=%s date=%s (already=%s)",
                 rid, tx.transaction_id, amt, tx_date, already_paid)
        return {
            "ok": True,
            "transaction_id": str(tx.transaction_id),
            "already_paid_this_month": already_paid,
        }
