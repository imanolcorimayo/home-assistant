"""Read-only analytics queries — feed for the Consultor agent.

Each function runs ONE SQL aggregation and returns a plain dict / list of
dicts. No LLM, no MCP — those layers wrap these from `mcp_server.py`. Keeps
the queries testable in isolation and reusable from the web dashboard if it
ever needs them.

Conventions:
- Dates in/out as 'YYYY-MM-DD' strings; parsed via `date.fromisoformat`.
- Every query filters `deleted_ts IS NULL`.
- `group_by` values: 'category' | 'member' | 'account' | 'day' | 'week' | 'month'.
- `kind` values: 'expense' | 'income'.
- Money returned as `float` (NUMERIC → float for JSON serialisation).
"""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import case, cast, func, select
from sqlalchemy.types import String

from app.database import AsyncSessionLocal
from app.models import (
    Account,
    FamilyMember,
    MonthlyBudget,
    Transaction,
    TransactionKind,
)
from app.services.recurring import list_recurring_charges
from app.services.transactions import resolve_account_hint, resolve_member_hint

log = logging.getLogger("analytics")

_VALID_GROUP_BY = {"category", "member", "account", "day", "week", "month"}


def _parse_date(raw: str) -> date:
    """Strict ISO parse — callers always pass YYYY-MM-DD. Raises ValueError on
    bad input rather than silently defaulting; the agent should learn to send
    valid dates instead of garbage being swallowed."""
    return date.fromisoformat(raw)


def _month_bounds(year_month: str) -> tuple[date, date]:
    """'YYYY-MM' → (first day, last day)."""
    y, m = map(int, year_month.split("-"))
    start = date(y, m, 1)
    end = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    return start, end


def _f(v) -> float:
    """Decimal/None → float. Defensive cast for JSON-safe output."""
    if v is None:
        return 0.0
    return float(v)


async def _sum_by(
    kind: str,
    start: date,
    end: date,
    group_by: str,
    member_hint: Optional[str] = None,
    account_hint: Optional[str] = None,
) -> list[dict]:
    """Shared aggregation engine for `spending_by_period` / `income_by_period`.

    Builds a SUM(amount) + COUNT(*) GROUP BY <dimension>, filtered by kind +
    date window + optional account/member. The group column is exposed in the
    result as 'group' so callers can render uniformly."""
    if group_by not in _VALID_GROUP_BY:
        raise ValueError(f"group_by must be one of {sorted(_VALID_GROUP_BY)}")

    async with AsyncSessionLocal() as session:
        member_id = await resolve_member_hint(session, member_hint) if member_hint else None
        account = await resolve_account_hint(session, account_hint) if account_hint else None

        # Pick the grouping expression. For category/member/account we want a
        # human-readable label, so we join when needed and select .name.
        if group_by == "category":
            label = Transaction.category
            join_cls = None
        elif group_by == "member":
            label = FamilyMember.full_name
            join_cls = FamilyMember
        elif group_by == "account":
            label = Account.name
            join_cls = Account
        elif group_by == "day":
            label = cast(Transaction.transaction_date, String)
            join_cls = None
        elif group_by == "week":
            # ISO week start (Monday).
            label = func.to_char(
                func.date_trunc("week", Transaction.transaction_date), "IYYY-IW"
            )
            join_cls = None
        else:  # month
            label = func.to_char(
                func.date_trunc("month", Transaction.transaction_date), "YYYY-MM"
            )
            join_cls = None

        q = (
            select(
                label.label("group"),
                func.sum(Transaction.amount).label("total"),
                func.count(Transaction.transaction_id).label("count"),
            )
            .where(
                Transaction.deleted_ts.is_(None),
                Transaction.kind == TransactionKind(kind),
                Transaction.transaction_date >= start,
                Transaction.transaction_date <= end,
            )
            .group_by(label)
            .order_by(func.sum(Transaction.amount).desc())
        )

        if join_cls is FamilyMember:
            q = q.join(FamilyMember, Transaction.family_member_id == FamilyMember.family_member_id)
        elif join_cls is Account:
            q = q.join(Account, Transaction.account_id == Account.account_id)

        if member_id is not None:
            q = q.where(Transaction.family_member_id == member_id)
        if account is not None:
            q = q.where(Transaction.account_id == account.account_id)

        rows = (await session.execute(q)).all()
        return [{"group": r.group, "total": _f(r.total), "count": r.count} for r in rows]


async def spending_by_period(
    start: str,
    end: str,
    group_by: str = "category",
    member_hint: Optional[str] = None,
    account_hint: Optional[str] = None,
) -> list[dict]:
    """Gastos agregados entre start y end (YYYY-MM-DD), agrupados por la
    dimensión pedida. Soporta filtros opcionales por miembro y cuenta."""
    return await _sum_by("expense", _parse_date(start), _parse_date(end),
                         group_by, member_hint, account_hint)


async def income_by_period(
    start: str,
    end: str,
    group_by: str = "category",
    member_hint: Optional[str] = None,
    account_hint: Optional[str] = None,
) -> list[dict]:
    """Ingresos agregados entre start y end (YYYY-MM-DD), agrupados por
    dimensión pedida. Mismo shape que spending_by_period."""
    return await _sum_by("income", _parse_date(start), _parse_date(end),
                         group_by, member_hint, account_hint)


async def balance_for_period(start: str, end: str) -> dict:
    """Suma de ingresos y gastos en el período + neto (income - expense).
    Devuelve siempre las tres claves con 0.0 si no hay datos."""
    start_d, end_d = _parse_date(start), _parse_date(end)
    async with AsyncSessionLocal() as session:
        q = select(
            func.coalesce(
                func.sum(case((Transaction.kind == TransactionKind.income, Transaction.amount))),
                0,
            ).label("income"),
            func.coalesce(
                func.sum(case((Transaction.kind == TransactionKind.expense, Transaction.amount))),
                0,
            ).label("expense"),
        ).where(
            Transaction.deleted_ts.is_(None),
            Transaction.transaction_date >= start_d,
            Transaction.transaction_date <= end_d,
        )
        row = (await session.execute(q)).one()
        income, expense = _f(row.income), _f(row.expense)
        return {"income": income, "expense": expense, "net": income - expense}


async def savings_rate(start: str, end: str) -> dict:
    """Tasa de ahorro = (income - expense) / income * 100. Si income es 0,
    rate_pct es None (no se puede dividir; el agente debe interpretarlo)."""
    b = await balance_for_period(start, end)
    rate = round(b["net"] / b["income"] * 100, 1) if b["income"] > 0 else None
    return {**b, "rate_pct": rate}


async def category_trend(category: str, months: int = 12) -> list[dict]:
    """Serie mensual de gastos de UNA categoría — últimos N meses (incluido el
    actual). Útil para 'cómo viene transporte últimos 6 meses'."""
    today = date.today()
    # First day of the month N-1 months ago.
    start = date(today.year, today.month, 1)
    for _ in range(months - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    async with AsyncSessionLocal() as session:
        month_label = func.to_char(
            func.date_trunc("month", Transaction.transaction_date), "YYYY-MM"
        )
        q = (
            select(
                month_label.label("month"),
                func.sum(Transaction.amount).label("total"),
                func.count(Transaction.transaction_id).label("count"),
            )
            .where(
                Transaction.deleted_ts.is_(None),
                Transaction.kind == TransactionKind.expense,
                func.lower(Transaction.category) == category.strip().lower(),
                Transaction.transaction_date >= start,
            )
            .group_by(month_label)
            .order_by(month_label)
        )
        rows = (await session.execute(q)).all()
        return [{"month": r.month, "total": _f(r.total), "count": r.count} for r in rows]


async def top_categories(
    start: str, end: str, kind: str = "expense", limit: int = 5
) -> list[dict]:
    """Top N categorías de gasto o ingreso en el período, ordenadas por monto
    descendente. limit acotado a [1, 50]."""
    start_d, end_d = _parse_date(start), _parse_date(end)
    limit = max(1, min(int(limit), 50))
    async with AsyncSessionLocal() as session:
        q = (
            select(
                Transaction.category.label("category"),
                func.sum(Transaction.amount).label("total"),
                func.count(Transaction.transaction_id).label("count"),
            )
            .where(
                Transaction.deleted_ts.is_(None),
                Transaction.kind == TransactionKind(kind),
                Transaction.transaction_date >= start_d,
                Transaction.transaction_date <= end_d,
            )
            .group_by(Transaction.category)
            .order_by(func.sum(Transaction.amount).desc())
            .limit(limit)
        )
        rows = (await session.execute(q)).all()
        return [{"category": r.category, "total": _f(r.total), "count": r.count} for r in rows]


async def period_comparison(
    period_a: tuple[str, str],
    period_b: tuple[str, str],
    kind: str = "expense",
) -> dict:
    """Compara el monto total de gasto/ingreso entre dos períodos.
    Devuelve {a, b, diff: b-a, pct_change: (b-a)/a*100}. pct_change es None
    cuando a==0 (no se puede calcular crecimiento desde cero)."""
    a_start, a_end = period_a
    b_start, b_end = period_b
    a_d = (_parse_date(a_start), _parse_date(a_end))
    b_d = (_parse_date(b_start), _parse_date(b_end))

    async with AsyncSessionLocal() as session:
        async def _sum(start, end):
            q = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.deleted_ts.is_(None),
                Transaction.kind == TransactionKind(kind),
                Transaction.transaction_date >= start,
                Transaction.transaction_date <= end,
            )
            return _f((await session.execute(q)).scalar())
        a_total = await _sum(*a_d)
        b_total = await _sum(*b_d)
    diff = b_total - a_total
    pct = round(diff / a_total * 100, 1) if a_total > 0 else None
    return {"a": a_total, "b": b_total, "diff": diff, "pct_change": pct}


async def budget_status(year_month: Optional[str] = None) -> list[dict]:
    """Para cada presupuesto definido, cuánto se gastó en el mes y cuánto
    queda. year_month en formato 'YYYY-MM' (default: mes actual).

    Match: monthly_budget.subcategory_1 ↔ transaction.subcategory_1 O
    transaction.category (caso comun donde la categoría es el nombre del
    presupuesto). Esto refleja el modelo actual donde subcategory_1 no es FK
    estricta y la mayoría de las txs sólo traen `category`."""
    if year_month is None:
        today = date.today()
        year_month = f"{today.year:04d}-{today.month:02d}"
    start, end = _month_bounds(year_month)

    async with AsyncSessionLocal() as session:
        budgets = (await session.scalars(
            select(MonthlyBudget).order_by(MonthlyBudget.subcategory_1)
        )).all()

        out: list[dict] = []
        for b in budgets:
            q = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.deleted_ts.is_(None),
                Transaction.kind == TransactionKind.expense,
                Transaction.transaction_date >= start,
                Transaction.transaction_date <= end,
                func.lower(
                    func.coalesce(Transaction.subcategory_1, Transaction.category)
                ) == b.subcategory_1.lower(),
            )
            spent = _f((await session.execute(q)).scalar())
            limit = _f(b.limit_amount)
            out.append({
                "category": b.subcategory_1,
                "limit": limit,
                "spent": spent,
                "remaining": round(limit - spent, 2),
                "pct_used": round(spent / limit * 100, 1) if limit > 0 else None,
            })
        return out


async def pending_recurring(year_month: Optional[str] = None) -> list[dict]:
    """Recurrentes activos que NO se pagaron en el mes pedido. Para el mes
    actual usa la flag `paid_this_month` que ya calcula list_recurring_charges;
    para meses pasados/futuros chequea si existe alguna tx linkeada en ese mes.
    Devuelve nombre, monto, día del mes y cuenta."""
    today = date.today()
    if year_month is None:
        year_month = f"{today.year:04d}-{today.month:02d}"
    start, end = _month_bounds(year_month)
    is_current = (start.year == today.year and start.month == today.month)

    charges = await list_recurring_charges(include_inactive=False)
    pending: list[dict] = []

    async with AsyncSessionLocal() as session:
        for c in charges:
            if is_current:
                already_paid = bool(c.get("paid_this_month"))
            else:
                # For non-current months, ask the DB directly.
                q = select(func.count(Transaction.transaction_id)).where(
                    Transaction.deleted_ts.is_(None),
                    Transaction.recurring_charge_id == c["id"],
                    Transaction.transaction_date >= start,
                    Transaction.transaction_date <= end,
                )
                already_paid = ((await session.execute(q)).scalar() or 0) > 0

            if already_paid:
                continue

            # Days until due date in the asked month (positive = future, negative = overdue).
            try:
                due = date(start.year, start.month, c["day_of_month"])
                due_in_days = (due - today).days if is_current else None
            except ValueError:
                # day_of_month doesn't exist in this month (e.g. 31 in February).
                due_in_days = None

            pending.append({
                "name": c["name"],
                "amount": _f(c["amount"]),
                "day_of_month": c["day_of_month"],
                "category": c.get("category"),
                "due_in_days": due_in_days,
            })
    # Soonest first; nulls (invalid day) last.
    pending.sort(key=lambda r: (r["due_in_days"] is None, r["due_in_days"] or 0))
    return pending


async def account_balances() -> list[dict]:
    """Saldo actual de cada cuenta activa. Reproduce el cálculo del
    /settings web: initial_balance + Σ income - Σ expense de las txs vivas."""
    async with AsyncSessionLocal() as session:
        q = (
            select(
                Account.name.label("account"),
                Account.kind.label("kind"),
                Account.currency.label("currency"),
                (
                    Account.initial_balance
                    + func.coalesce(
                        func.sum(
                            case((Transaction.kind == TransactionKind.income, Transaction.amount))
                        ),
                        0,
                    )
                    - func.coalesce(
                        func.sum(
                            case((Transaction.kind == TransactionKind.expense, Transaction.amount))
                        ),
                        0,
                    )
                ).label("balance"),
            )
            .select_from(Account)
            .outerjoin(
                Transaction,
                (Transaction.account_id == Account.account_id) & Transaction.deleted_ts.is_(None),
            )
            .where(Account.is_active)
            .group_by(Account.account_id, Account.name, Account.kind, Account.currency,
                      Account.initial_balance)
            .order_by(Account.name)
        )
        rows = (await session.execute(q)).all()
        return [
            {"account": r.account, "kind": r.kind.value, "currency": r.currency,
             "balance": _f(r.balance)}
            for r in rows
        ]


async def average_by_category(
    kind: str = "expense",
    start: Optional[str] = None,
    end: Optional[str] = None,
    group_by: str = "month",
) -> list[dict]:
    """Promedio de gasto o ingreso por categoría. group_by='month' divide el
    total por la cantidad de meses con actividad en esa categoría;
    group_by='tx' devuelve el promedio por transacción. Default últimos 12
    meses si no se pasan fechas."""
    today = date.today()
    if end is None:
        end_d = today
    else:
        end_d = _parse_date(end)
    if start is None:
        # First day of the month 11 months ago.
        start_d = date(today.year, today.month, 1)
        for _ in range(11):
            start_d = (start_d - timedelta(days=1)).replace(day=1)
    else:
        start_d = _parse_date(start)

    if group_by not in ("month", "tx"):
        raise ValueError("group_by must be 'month' or 'tx'")

    async with AsyncSessionLocal() as session:
        if group_by == "month":
            # COUNT(DISTINCT month) so categories with sparse activity aren't punished
            # by the full window — e.g. spent 100 in just 2 months → avg=50, not 100/12.
            month_expr = func.date_trunc("month", Transaction.transaction_date)
            q = (
                select(
                    Transaction.category.label("category"),
                    func.sum(Transaction.amount).label("total"),
                    func.count(func.distinct(month_expr)).label("months_count"),
                )
                .where(
                    Transaction.deleted_ts.is_(None),
                    Transaction.kind == TransactionKind(kind),
                    Transaction.transaction_date >= start_d,
                    Transaction.transaction_date <= end_d,
                )
                .group_by(Transaction.category)
                .order_by(func.sum(Transaction.amount).desc())
            )
            rows = (await session.execute(q)).all()
            return [
                {
                    "category": r.category,
                    "avg_per_month": round(_f(r.total) / r.months_count, 2)
                                     if r.months_count else 0.0,
                    "total": _f(r.total),
                    "months_count": int(r.months_count or 0),
                }
                for r in rows
            ]
        else:  # group_by == 'tx'
            q = (
                select(
                    Transaction.category.label("category"),
                    func.avg(Transaction.amount).label("avg_per_tx"),
                    func.sum(Transaction.amount).label("total"),
                    func.count(Transaction.transaction_id).label("count"),
                )
                .where(
                    Transaction.deleted_ts.is_(None),
                    Transaction.kind == TransactionKind(kind),
                    Transaction.transaction_date >= start_d,
                    Transaction.transaction_date <= end_d,
                )
                .group_by(Transaction.category)
                .order_by(func.sum(Transaction.amount).desc())
            )
            rows = (await session.execute(q)).all()
            return [
                {
                    "category": r.category,
                    "avg_per_tx": round(_f(r.avg_per_tx), 2),
                    "total": _f(r.total),
                    "count": int(r.count or 0),
                }
                for r in rows
            ]
