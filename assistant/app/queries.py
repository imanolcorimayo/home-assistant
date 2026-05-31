"""Read-only queries for the display pages (Inicio / Movimientos / Configuración).

These are VIEWS only — no writes. Editing still goes through the agent for now.
Same rules as everywhere: raw parameterized SQL, every query scoped by family_id
from the session. Floats are fine here (display, not money math)."""

from app import db


# ── Inicio / dashboard ────────────────────────────────────────────────────

async def account_balances(family_id) -> list[dict]:
    """Per-account current balance = initial_balance + Σ signed transactions
    that have actually moved (value_date in (balance_date, today])."""
    rows = await db.fetch(
        """
        SELECT a.name, a.kind, a.currency,
               a.initial_balance + COALESCE(SUM(
                   CASE WHEN t.kind = 'income' THEN t.amount ELSE -t.amount END
               ), 0) AS balance
        FROM account a
        LEFT JOIN transaction t
               ON t.account_id = a.account_id
              AND t.deleted_ts IS NULL
              AND t.value_date >  a.balance_date
              AND t.value_date <= CURRENT_DATE
        WHERE a.family_id = $1 AND a.is_active
        GROUP BY a.account_id, a.name, a.kind, a.currency, a.initial_balance
        ORDER BY a.created_ts
        """,
        family_id,
    )
    return [{"name": r["name"], "kind": r["kind"], "currency": r["currency"],
             "balance": float(r["balance"])} for r in rows]


async def month_summary(family_id) -> dict:
    """Current calendar month: total expenses, income, and net."""
    r = await db.fetchrow(
        """
        SELECT
            COALESCE(SUM(amount) FILTER (WHERE kind = 'expense'), 0) AS gastos,
            COALESCE(SUM(amount) FILTER (WHERE kind = 'income'),  0) AS ingresos
        FROM transaction
        WHERE family_id = $1 AND deleted_ts IS NULL
          AND transaction_date >= date_trunc('month', CURRENT_DATE)
        """,
        family_id,
    )
    gastos, ingresos = float(r["gastos"]), float(r["ingresos"])
    return {"gastos": gastos, "ingresos": ingresos, "neto": ingresos - gastos}


async def budgets_vs_spend(family_id) -> list[dict]:
    """Each budget with this month's spend in its category."""
    rows = await db.fetch(
        """
        SELECT c.name AS category, b.limit_amount AS lim,
               COALESCE(SUM(t.amount) FILTER (
                   WHERE t.kind = 'expense'
                     AND t.transaction_date >= date_trunc('month', CURRENT_DATE)
               ), 0) AS spent
        FROM monthly_budget b
        JOIN category c ON c.category_id = b.category_id
        LEFT JOIN transaction t
               ON t.category_id = b.category_id
              AND t.family_id   = b.family_id
              AND t.deleted_ts IS NULL
        WHERE b.family_id = $1
        GROUP BY c.name, b.limit_amount
        ORDER BY c.name
        """,
        family_id,
    )
    return [{"category": r["category"], "limit": float(r["lim"]),
             "spent": float(r["spent"])} for r in rows]


# ── Movimientos list ──────────────────────────────────────────────────────

async def transactions_page(family_id, q: str | None, page: int,
                            per_page: int = 25) -> dict:
    """Paginated, optionally searched transaction list (newest first)."""
    page = max(page, 1)
    clauses = ["t.family_id = $1", "t.deleted_ts IS NULL"]
    params: list = [family_id]
    if q and q.strip():
        params.append(f"%{q.strip()}%")
        i = len(params)
        clauses.append(f"(t.description ILIKE ${i} OR c.name ILIKE ${i})")
    where = " AND ".join(clauses)

    total = await db.fetchval(
        f"SELECT count(*) FROM transaction t "
        f"JOIN category c ON c.category_id = t.category_id WHERE {where}",
        *params,
    )
    params.extend([per_page, (page - 1) * per_page])
    rows = await db.fetch(
        f"""
        SELECT t.transaction_date, t.kind, t.amount, t.description,
               c.name AS category, m.full_name AS member, a.currency
        FROM transaction t
        JOIN category c ON c.category_id = t.category_id
        JOIN member   m ON m.member_id   = t.member_id
        JOIN account  a ON a.account_id  = t.account_id
        WHERE {where}
        ORDER BY t.transaction_date DESC, t.created_ts DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )
    items = [{"date": str(r["transaction_date"]), "kind": r["kind"],
              "amount": float(r["amount"]), "description": r["description"],
              "category": r["category"], "member": r["member"],
              "currency": r["currency"]} for r in rows]
    pages = max((total + per_page - 1) // per_page, 1)
    return {"items": items, "total": total, "page": page, "pages": pages}


# ── Configuración views ───────────────────────────────────────────────────

async def categories(family_id) -> list[dict]:
    rows = await db.fetch(
        """
        SELECT name, grupo FROM category
        WHERE family_id = $1 AND is_active
        ORDER BY grupo NULLS LAST, name
        """,
        family_id,
    )
    return [{"name": r["name"], "grupo": r["grupo"]} for r in rows]
