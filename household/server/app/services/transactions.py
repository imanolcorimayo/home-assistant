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
    AccountKind,
    Category,
    FamilyMember,
    MonthlyBudget,
    RecurringCharge,
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
    limit: int = 20,
    days: int = 90,
    term: Optional[str] = None,
    kind: Optional[str] = None,
    include_deleted: bool = False,
) -> list[dict]:
    """Recent transactions, newest first.

    kind: 'expense', 'income', or None for both. Backs the agent's pre-fed list
    (expenses only) and the look_up_transactions tool (any kind).

    include_deleted: if True, also returns soft-deleted rows (each is flagged
    with `deleted=True` in the output). Used when the agent needs to restore
    a deleted transaction.

    Each row carries `kind`, `family_member` (full name), `deleted` flag, and
    `recurring_charge_id` (set when the row is a recurring payment).
    """
    since = date_cls.today() - timedelta(days=max(days, 1))
    async with AsyncSessionLocal() as session:
        q = select(Transaction).where(Transaction.transaction_date >= since)
        if not include_deleted:
            q = q.where(Transaction.deleted_ts.is_(None))
        if kind in ("expense", "income"):
            q = q.where(Transaction.kind == TransactionKind(kind))
        if term:
            like = f"%{term.strip()}%"
            q = q.where(or_(Transaction.description.ilike(like), Transaction.category.ilike(like)))
        q = q.order_by(Transaction.transaction_date.desc()).limit(min(max(limit, 1), 50))
        rows = (await session.scalars(q)).all()
        member_ids = {t.family_member_id for t in rows}
        members = (await session.scalars(
            select(FamilyMember).where(FamilyMember.family_member_id.in_(member_ids))
        )).all() if member_ids else []
        member_names = {m.family_member_id: m.full_name for m in members}
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
                "family_member": member_names.get(t.family_member_id),
                "deleted": t.deleted_ts is not None,
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


async def build_household_context() -> dict:
    """Read full household configuration from DB. Used to inject context into
    Gemini prompts (agent + parser) so it knows about real accounts, members,
    budgets, and recurring charges — not just category names.

    Single round-trip per request; the family is small enough that a single
    eager load is cheaper than lazy fetching via MCP tools."""
    async with AsyncSessionLocal() as session:
        members_rows = (await session.scalars(
            select(FamilyMember).where(FamilyMember.is_active).order_by(FamilyMember.created_ts)
        )).all()
        accounts_rows = (await session.scalars(
            select(Account).where(Account.is_active).order_by(Account.created_ts)
        )).all()
        owners = {m.family_member_id: m.full_name for m in members_rows}
        categories = await active_category_names()
        budgets_rows = (await session.scalars(
            select(MonthlyBudget).order_by(MonthlyBudget.subcategory_1)
        )).all()
        recurring_rows = (await session.scalars(
            select(RecurringCharge).where(RecurringCharge.is_active).order_by(RecurringCharge.name)
        )).all()
        accounts_by_id = {a.account_id: a.name for a in accounts_rows}

        return {
            "members": [{"name": m.full_name} for m in members_rows],
            "accounts": [
                {
                    "name": a.name,
                    "kind": a.kind.value,
                    "currency": a.currency,
                    "owner": owners.get(a.family_member_id) if a.family_member_id else None,
                }
                for a in accounts_rows
            ],
            "categories": categories,
            "budgets": [
                {"category": b.subcategory_1, "limit": float(b.limit_amount)}
                for b in budgets_rows
            ],
            "recurring": [
                {
                    "name": r.name,
                    "amount": float(r.amount),
                    "day": r.day_of_month,
                    "category": r.category,
                    "account": accounts_by_id.get(r.account_id, "?"),
                }
                for r in recurring_rows
            ],
        }


def format_context_for_prompt(ctx: dict) -> str:
    """Render the household context as compact Spanish text for system prompts.
    Sections are omitted when empty so the prompt doesn't lie about absent data."""
    lines: list[str] = ["=== Contexto familiar ==="]

    members = [m["name"] for m in ctx.get("members", [])]
    if members:
        lines.append("Miembros: " + ", ".join(members))

    accounts = ctx.get("accounts", [])
    if accounts:
        lines.append("Cuentas:")
        for a in accounts:
            owner = a.get("owner") or "compartida"
            lines.append(f"- {a['name']} ({a['kind']}, {a['currency']}) — {owner}")

    budgets = ctx.get("budgets", [])
    if budgets:
        lines.append("Presupuestos mensuales:")
        for b in budgets:
            lines.append(f"- {b['category']}: {b['limit']:g} EUR")

    recurring = ctx.get("recurring", [])
    if recurring:
        lines.append("Gastos recurrentes:")
        for r in recurring:
            lines.append(
                f"- {r['name']}: {r['amount']:g} EUR el día {r['day']} "
                f"({r['category']}, {r['account']})"
            )

    return "\n".join(lines)


async def _resolve_hint(session: AsyncSession, hint: Optional[str], column, model):
    """Generic fuzzy match: exact ci match first, then ILIKE %hint%."""
    if not hint:
        return None
    needle = hint.strip()
    if not needle:
        return None
    row = await session.scalar(
        select(model).where(func.lower(column) == needle.lower())
    )
    if row is not None:
        return row
    return await session.scalar(
        select(model).where(column.ilike(f"%{needle}%"))
    )


async def resolve_account_hint(session: AsyncSession, hint: Optional[str]) -> Optional[Account]:
    """Match a free-text account hint ('Visa', 'efectivo') to a real Account."""
    row = await _resolve_hint(session, hint, Account.name, Account)
    if row is None or not row.is_active:
        return None
    return row


async def resolve_member_hint(session: AsyncSession, hint: Optional[str]):
    """Match a free-text member hint ('Luisiana') to a real FamilyMember id."""
    row = await _resolve_hint(session, hint, FamilyMember.full_name, FamilyMember)
    if row is None or not row.is_active:
        return None
    return row.family_member_id


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


async def save_parsed(
    parsed: dict,
    sender_id: str,
    sender_member_id=None,
    source: str = "whatsapp",
) -> Optional[str]:
    """Insert one row in `transaction` from a Gemini-parsed payload.

    `sender_id` is a free-text identifier of the sender (wa_id, telegram id
    as string, etc) — stored in llm_raw_output for traceability.
    `sender_member_id` is the resolved family_member_id of the sender; when
    provided, it replaces the global default. The parser's family_member_hint
    can still override it (e.g. "Luisiana gastó X" sent by Hector).
    `source` controls the TransactionSource enum ('whatsapp' or 'telegram').

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

    try:
        src = TransactionSource(source)
    except ValueError:
        src = TransactionSource.whatsapp

    async with AsyncSessionLocal() as session:
        defaults = await _pick_defaults(session)
        if defaults is None:
            return None
        default_member_id, default_account = defaults
        if sender_member_id is not None:
            default_member_id = sender_member_id

        hinted_account = await resolve_account_hint(session, parsed.get("account_hint"))
        account = hinted_account or default_account
        hinted_member_id = await resolve_member_hint(session, parsed.get("family_member_hint"))
        member_id = hinted_member_id or default_member_id

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
            source=src,
            llm_confidence=CONFIDENCE_MAP.get(confidence_label),
            llm_raw_output={**parsed, "_sender_id": sender_id, "_source": source},
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
    account_hint: Optional[str] = None,
    family_member_hint: Optional[str] = None,
) -> Optional[str]:
    """Insert one transaction from explicit fields (used by the MCP add_transaction tool).

    Unlike save_parsed there's no confidence gating — the caller already
    decided to write. Account/member default to the oldest active; currency
    comes from the account; category resolves to category_id (or fallback).
    `account_hint` / `family_member_hint` allow fuzzy-matching real rows
    (e.g. 'Visa' -> 'Visa Hector') before falling back to defaults.
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
        default_member_id, default_account = defaults

        hinted_account = await resolve_account_hint(session, account_hint)
        account = hinted_account or default_account
        hinted_member_id = await resolve_member_hint(session, family_member_hint)
        member_id = hinted_member_id or default_member_id

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


async def soft_delete_transaction(transaction_id: str) -> Optional[dict]:
    """Mark a transaction as deleted (sets deleted_ts = now()).
    The row stays in the table so it can be restored — every analytics
    query filters `deleted_ts IS NULL`, so the gasto disappears from
    reports but is recoverable.

    Returns a dict with the transaction's snapshot at deletion time so the
    agent can confirm exactly what got removed; None if the id is invalid,
    not found, or was already deleted (idempotent: re-deleting is a no-op
    that still returns None to surface the situation to the agent).
    """
    try:
        tid = uuid.UUID(str(transaction_id))
    except (ValueError, TypeError, AttributeError):
        log.info("soft_delete_transaction: bad id=%r", transaction_id)
        return None

    async with AsyncSessionLocal() as session:
        tx = await session.get(Transaction, tid)
        if tx is None or tx.deleted_ts is not None:
            log.info("soft_delete_transaction: not found/already deleted id=%s", transaction_id)
            return None

        member = await session.get(FamilyMember, tx.family_member_id)
        snapshot = {
            "transaction_id": str(tx.transaction_id),
            "kind": tx.kind.value,
            "amount": float(tx.amount),
            "currency": tx.currency,
            "category": tx.category,
            "description": tx.description,
            "transaction_date": tx.transaction_date.isoformat(),
            "family_member": member.full_name if member else None,
        }
        tx.deleted_ts = func.now()
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            log.exception("soft_delete_transaction failed: %s", exc)
            return None

        log.info("soft-deleted transaction id=%s", tx.transaction_id)
        return snapshot


async def restore_transaction(transaction_id: str) -> Optional[dict]:
    """Undo a soft-delete: clear deleted_ts so the row re-enters reports.
    Returns a snapshot dict on success; None if id invalid, not found, or
    the row wasn't deleted to begin with.
    """
    try:
        tid = uuid.UUID(str(transaction_id))
    except (ValueError, TypeError, AttributeError):
        log.info("restore_transaction: bad id=%r", transaction_id)
        return None

    async with AsyncSessionLocal() as session:
        tx = await session.get(Transaction, tid)
        if tx is None or tx.deleted_ts is None:
            log.info("restore_transaction: not found/not deleted id=%s", transaction_id)
            return None

        member = await session.get(FamilyMember, tx.family_member_id)
        snapshot = {
            "transaction_id": str(tx.transaction_id),
            "kind": tx.kind.value,
            "amount": float(tx.amount),
            "currency": tx.currency,
            "category": tx.category,
            "description": tx.description,
            "transaction_date": tx.transaction_date.isoformat(),
            "family_member": member.full_name if member else None,
        }
        tx.deleted_ts = None
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            log.exception("restore_transaction failed: %s", exc)
            return None

        log.info("restored transaction id=%s", tx.transaction_id)
        return snapshot


# ── Household-structure creation ──────────────────────────────────────────
# Backs the agent's create_category / create_budget / create_member /
# create_account tools (issue #21, "sugerir y crear nuevos valores"). These
# mirror the web/ Ajustes write routes so chat and web stay in sync. The agent
# is told (system prompt) to confirm before creating structure — categories /
# budgets are routine, accounts / members rarely change.

_VALID_GRUPOS = {"variable", "fijo", "ingreso"}


async def create_category(name: str, grupo: Optional[str] = None) -> dict:
    """Create a category. Idempotent on name (case-insensitive): an existing
    one is returned as-is rather than duplicated. grupo, when given, must be
    one of variable/fijo/ingreso. Returns {"ok", "name", "created"} or error."""
    clean = (name or "").strip()
    if not clean:
        return {"ok": False, "error": "el nombre no puede estar vacío"}
    g = (grupo or "").strip().lower() or None
    if g is not None and g not in _VALID_GRUPOS:
        return {"ok": False, "error": "grupo debe ser variable, fijo o ingreso"}

    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(Category).where(func.lower(Category.name) == clean.lower())
        )
        if existing is not None:
            return {"ok": True, "name": existing.name, "created": False}
        cat = Category(name=clean, grupo=g)
        session.add(cat)
        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            log.exception("create_category failed: %s", exc)
            return {"ok": False, "error": "no se pudo crear la categoría"}
        log.info("created category name=%r grupo=%r", clean, g)
        return {"ok": True, "name": cat.name, "created": True}


async def create_budget(category: str, limit_amount) -> dict:
    """Set a monthly budget for a category (monthly_budget.subcategory_1 is
    unique). If one already exists for that category, its limit is updated.
    limit_amount > 0 (EUR). Returns {"ok", "category", "limit", "created"}."""
    clean = (category or "").strip()
    if not clean:
        return {"ok": False, "error": "la categoría no puede estar vacía"}
    amt = _parse_amount(limit_amount)
    if amt is None:
        return {"ok": False, "error": "límite inválido (debe ser > 0)"}

    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(MonthlyBudget).where(MonthlyBudget.subcategory_1 == clean)
        )
        created = existing is None
        if existing is not None:
            existing.limit_amount = amt
        else:
            session.add(MonthlyBudget(subcategory_1=clean, limit_amount=amt))
        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            log.exception("create_budget failed: %s", exc)
            return {"ok": False, "error": "no se pudo guardar el presupuesto"}
        log.info("set budget category=%r limit=%s created=%s", clean, amt, created)
        return {"ok": True, "category": clean, "limit": float(amt), "created": created}


async def create_member(full_name: str) -> dict:
    """Create a family member. Idempotent on name (case-insensitive). Returns
    {"ok", "name", "created"} or error."""
    clean = (full_name or "").strip()
    if not clean:
        return {"ok": False, "error": "el nombre no puede estar vacío"}

    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(FamilyMember).where(func.lower(FamilyMember.full_name) == clean.lower())
        )
        if existing is not None:
            return {"ok": True, "name": existing.full_name, "created": False}
        m = FamilyMember(full_name=clean)
        session.add(m)
        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            log.exception("create_member failed: %s", exc)
            return {"ok": False, "error": "no se pudo crear el miembro"}
        log.info("created family_member name=%r", clean)
        return {"ok": True, "name": m.full_name, "created": True}


async def create_account(
    name: str,
    kind: str,
    family_member_hint: Optional[str] = None,
    currency: str = "EUR",
    initial_balance=0,
) -> dict:
    """Create an account. kind must be one of checking/savings/cash/credit_card.
    family_member_hint (optional) ties it to a member by name; if it doesn't
    match, the account is shared (no owner). Returns {"ok", "account_id",
    "name", "created"} or error."""
    clean = (name or "").strip()
    if not clean:
        return {"ok": False, "error": "el nombre no puede estar vacío"}
    try:
        account_kind = AccountKind((kind or "").strip().lower())
    except ValueError:
        return {"ok": False, "error": "kind debe ser checking, savings, cash o credit_card"}
    bal = _parse_amount(initial_balance) if str(initial_balance).strip() not in ("", "0") else Decimal("0")
    if bal is None:
        return {"ok": False, "error": "balance inicial inválido"}

    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(Account).where(func.lower(Account.name) == clean.lower(), Account.is_active)
        )
        if existing is not None:
            return {"ok": True, "account_id": str(existing.account_id),
                    "name": existing.name, "created": False}
        member_id = await resolve_member_hint(session, family_member_hint)
        acc = Account(
            name=clean,
            kind=account_kind,
            currency=(currency or "EUR").strip().upper()[:3] or "EUR",
            initial_balance=bal,
            family_member_id=member_id,
        )
        session.add(acc)
        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            log.exception("create_account failed: %s", exc)
            return {"ok": False, "error": "no se pudo crear la cuenta"}
        log.info("created account name=%r kind=%s owner_id=%s", clean, account_kind.value,
                 member_id)
        return {"ok": True, "account_id": str(acc.account_id), "name": acc.name, "created": True}
