"""Page handlers. One function per page, each writing its own SQL.

Conventions from the schema: soft-deleted transactions are hidden with
`deleted_ts IS NULL`; tables are singular; money columns are numeric.
asyncpg uses $1, $2... placeholders (never string-format SQL values).
"""

import json
import math
import os
from datetime import date

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

PAGE_SIZE = 25

# Auto cache-bust: ?v=<css mtime>. Bumps whenever the stylesheet is rebuilt,
# so browsers/edge never serve a stale stylesheet — no manual version bumping.
_CSS = "app/static/style.css"
templates.env.globals["static_ver"] = int(os.path.getmtime(_CSS)) if os.path.exists(_CSS) else 0

# Period selector options (label + the SQL date predicate on transaction_date).
PERIODS = {
    "month": ("Este mes",     "date_trunc('month', transaction_date) = date_trunc('month', current_date)"),
    "last":  ("Mes anterior", "date_trunc('month', transaction_date) = date_trunc('month', current_date - interval '1 month')"),
    "year":  ("Este año",     "date_trunc('year', transaction_date) = date_trunc('year', current_date)"),
    "all":   ("Todo",         "TRUE"),
}


@router.get("/")
async def dashboard(request: Request, period: str = Query("month")):
    if period not in PERIODS:
        period = "month"
    _, predicate = PERIODS[period]

    # KPI cards: income / expense for the selected period.
    kpi = await db.fetchrow(
        f"""
        SELECT
          coalesce(sum(amount) FILTER (WHERE kind = 'income'),  0) AS income,
          coalesce(sum(amount) FILTER (WHERE kind = 'expense'), 0) AS expense
        FROM transaction
        WHERE deleted_ts IS NULL AND {predicate}
        """
    )
    net = kpi["income"] - kpi["expense"]

    # Monthly income vs expense for the last 12 months (the trend).
    rows = await db.fetch(
        """
        SELECT to_char(date_trunc('month', transaction_date), 'YYYY-MM') AS month,
               kind, sum(amount) AS total
        FROM transaction
        WHERE deleted_ts IS NULL
          AND transaction_date >= (current_date - interval '12 months')
        GROUP BY 1, 2
        ORDER BY 1 DESC, 2
        """
    )
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "rows": rows, "kpi": kpi, "net": net,
         "period": period, "periods": PERIODS},
    )


@router.get("/transactions")
async def transactions(request: Request, q: str = Query(""), page: int = Query(1)):
    q = q.strip()
    page = max(page, 1)

    # Build an optional case-insensitive search across the human-readable fields.
    where = "t.deleted_ts IS NULL"
    args: list = []
    if q:
        args.append(f"%{q}%")
        where += (f" AND (t.description ILIKE ${len(args)} OR t.category ILIKE ${len(args)}"
                  f" OR a.name ILIKE ${len(args)} OR m.full_name ILIKE ${len(args)})")

    base = """
        FROM transaction t
        JOIN account a       ON a.account_id = t.account_id
        JOIN family_member m ON m.family_member_id = t.family_member_id
        WHERE """ + where

    total = (await db.fetchrow(f"SELECT count(*) AS n {base}", *args))["n"]
    total_pages = max(math.ceil(total / PAGE_SIZE), 1)

    rows = await db.fetch(
        f"""
        SELECT t.transaction_id, t.transaction_date, t.kind, t.amount, t.currency,
               t.category, t.description, a.name AS account, m.full_name AS member
        {base}
        ORDER BY t.transaction_date DESC, t.created_ts DESC
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,
        *args, PAGE_SIZE, (page - 1) * PAGE_SIZE,
    )
    return templates.TemplateResponse(
        "transactions.html",
        {"request": request, "rows": rows, "q": q,
         "page": page, "total_pages": total_pages, "total": total},
    )


@router.get("/agent-runs")
async def agent_runs(request: Request, q: str = Query(""), page: int = Query(1)):
    """Read-only log of registrar-agent runs (issue #18). One row per message,
    showing the tools fired (with their args) and the token cost. The header
    table totals tokens per day so we can eyeball the running cost."""
    q = q.strip()
    page = max(page, 1)

    where = "TRUE"
    args: list = []
    if q:
        args.append(f"%{q}%")
        where = (f"(input_text ILIKE ${len(args)} OR reply_text ILIKE ${len(args)}"
                 f" OR model_used ILIKE ${len(args)})")

    total = (await db.fetchrow(f"SELECT count(*) AS n FROM agent_run WHERE {where}", *args))["n"]
    total_pages = max(math.ceil(total / PAGE_SIZE), 1)

    # tool_calls comes back as a JSON string (no asyncpg codec) — parse it so
    # the template can render each tool's name and the args it was called with.
    raw = await db.fetch(
        f"""
        SELECT created_ts, session_id, input_text, reply_text, model_used, error,
               tool_calls, total_tokens
        FROM agent_run
        WHERE {where}
        ORDER BY created_ts DESC
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,
        *args, PAGE_SIZE, (page - 1) * PAGE_SIZE,
    )
    rows = []
    for r in raw:
        row = dict(r)
        try:
            row["tools"] = json.loads(r["tool_calls"]) if r["tool_calls"] else []
        except (TypeError, ValueError):
            row["tools"] = []
        rows.append(row)

    # Daily token totals — the "cost" view. Newest day first, last 14 days.
    daily = await db.fetch(
        """
        SELECT date(created_ts) AS day, count(*) AS runs,
               coalesce(sum(total_tokens), 0) AS tokens
        FROM agent_run
        GROUP BY 1 ORDER BY 1 DESC LIMIT 14
        """
    )
    return templates.TemplateResponse(
        "agent_runs.html",
        {"request": request, "rows": rows, "daily": daily, "q": q,
         "page": page, "total_pages": total_pages, "total": total},
    )


def _last_12_months() -> list[str]:
    """['YYYY-MM', …] for the trailing 12 months, oldest first — the shared
    x-axis for the trend and savings-rate charts, so months with no rows
    still show as zero instead of disappearing."""
    today = date.today()
    out = []
    for i in range(11, -1, -1):
        mm, yy = today.month - i, today.year
        while mm <= 0:
            mm += 12
            yy -= 1
        out.append(f"{yy:04d}-{mm:02d}")
    return out


@router.get("/analytics")
async def analytics(request: Request):
    months = _last_12_months()
    midx = {m: i for i, m in enumerate(months)}
    cur_year = date.today().year

    # 1. Comparativa anual — income & expense per month, this year vs last.
    comp = await db.fetch(
        """
        SELECT extract(year  FROM transaction_date)::int  AS yr,
               extract(month FROM transaction_date)::int  AS mo,
               kind, sum(amount) AS total
        FROM transaction
        WHERE deleted_ts IS NULL
          AND transaction_date >= date_trunc('year', current_date) - interval '1 year'
        GROUP BY 1, 2, 3
        """
    )
    comparativa = {
        cur_year:     {"income": [0.0] * 12, "expense": [0.0] * 12},
        cur_year - 1: {"income": [0.0] * 12, "expense": [0.0] * 12},
    }
    for r in comp:
        if r["yr"] in comparativa:
            comparativa[r["yr"]][r["kind"]][r["mo"] - 1] = float(r["total"])

    # 2. Tendencia — last 12 months, the top-5 expense categories as lines.
    top = await db.fetch(
        """
        SELECT category, sum(amount) AS total
        FROM transaction
        WHERE deleted_ts IS NULL AND kind = 'expense'
          AND transaction_date >= date_trunc('month', current_date) - interval '11 months'
        GROUP BY category ORDER BY total DESC LIMIT 5
        """
    )
    top_cats = [r["category"] for r in top]
    trend = {c: [0.0] * 12 for c in top_cats}
    if top_cats:
        tser = await db.fetch(
            """
            SELECT to_char(date_trunc('month', transaction_date), 'YYYY-MM') AS month,
                   category, sum(amount) AS total
            FROM transaction
            WHERE deleted_ts IS NULL AND kind = 'expense'
              AND category = ANY($1)
              AND transaction_date >= date_trunc('month', current_date) - interval '11 months'
            GROUP BY 1, 2
            """,
            top_cats,
        )
        for r in tser:
            if r["month"] in midx:
                trend[r["category"]][midx[r["month"]]] = float(r["total"])

    # 3. Tasa de ahorro — (income - expense) / income, per month.
    bal = await db.fetch(
        """
        SELECT to_char(date_trunc('month', transaction_date), 'YYYY-MM') AS month,
               coalesce(sum(amount) FILTER (WHERE kind = 'income'),  0) AS income,
               coalesce(sum(amount) FILTER (WHERE kind = 'expense'), 0) AS expense
        FROM transaction
        WHERE deleted_ts IS NULL
          AND transaction_date >= date_trunc('month', current_date) - interval '11 months'
        GROUP BY 1
        """
    )
    savings = [0.0] * 12
    for r in bal:
        if r["month"] in midx and r["income"] > 0:
            savings[midx[r["month"]]] = round(
                float(r["income"] - r["expense"]) / float(r["income"]) * 100, 1
            )

    # 4. Gasto por categoría — current month, ranked.
    by_cat = await db.fetch(
        """
        SELECT category, sum(amount) AS total
        FROM transaction
        WHERE deleted_ts IS NULL AND kind = 'expense'
          AND date_trunc('month', transaction_date) = date_trunc('month', current_date)
        GROUP BY category ORDER BY total DESC
        """
    )

    data = {
        "months": months,
        "comparativa": {str(y): v for y, v in comparativa.items()},
        "cur_year": cur_year,
        "trend": {"cats": top_cats, "series": trend},
        "savings": savings,
    }
    by_cat_rows = [{"category": r["category"], "total": float(r["total"])} for r in by_cat]
    return templates.TemplateResponse(
        "analytics.html",
        {"request": request, "data": data, "by_cat": by_cat_rows},
    )


@router.get("/budgets")
async def budgets(request: Request):
    rows = await db.fetch(
        "SELECT subcategory_1, limit_amount FROM monthly_budget ORDER BY subcategory_1"
    )
    return templates.TemplateResponse(
        "placeholder.html", {"request": request, "title": "Presupuestos", "rows": rows}
    )


# Account types — enum value -> Spanish label for the form and table.
ACCOUNT_KINDS = {
    "checking":    "Cuenta corriente",
    "savings":     "Ahorro",
    "cash":        "Efectivo",
    "credit_card": "Tarjeta de crédito",
}


@router.get("/settings")
async def settings(request: Request):
    # Three config sections, view + add. Each fetched in display order.
    members = await db.fetch(
        """
        SELECT family_member_id, full_name, telegram_user_id, is_active
        FROM family_member ORDER BY full_name
        """
    )
    # Saldo actual = saldo inicial + ingresos − gastos (sólo movimientos vivos).
    accounts = await db.fetch(
        """
        SELECT a.account_id, a.name, a.kind, a.currency,
               a.initial_balance, a.is_active, m.full_name AS owner,
               a.initial_balance
                 + coalesce(sum(t.amount) FILTER (WHERE t.kind = 'income'),  0)
                 - coalesce(sum(t.amount) FILTER (WHERE t.kind = 'expense'), 0) AS balance
        FROM account a
        LEFT JOIN family_member m ON m.family_member_id = a.family_member_id
        LEFT JOIN transaction t
               ON t.account_id = a.account_id AND t.deleted_ts IS NULL
        GROUP BY a.account_id, m.full_name
        ORDER BY a.name
        """
    )
    categories = await db.fetch(
        """
        SELECT c.category_id, c.name, c.grupo, c.is_active,
               count(t.transaction_id) AS tx_count
        FROM category c
        LEFT JOIN transaction t
               ON t.category_id = c.category_id AND t.deleted_ts IS NULL
        GROUP BY c.category_id
        ORDER BY c.name
        """
    )
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "members": members, "accounts": accounts,
         "categories": categories, "account_kinds": ACCOUNT_KINDS},
    )


@router.post("/settings/member")
async def create_member(full_name: str = Form(...)):
    await db.execute(
        "INSERT INTO family_member (full_name) VALUES ($1)", full_name.strip()
    )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/account")
async def create_account(
    name: str = Form(...),
    kind: str = Form(...),
    family_member_id: str = Form(""),
    currency: str = Form("EUR"),
    initial_balance: str = Form("0"),
):
    await db.execute(
        """
        INSERT INTO account (name, kind, family_member_id, currency, initial_balance)
        VALUES ($1, $2::account_kind, NULLIF($3, '')::uuid, $4, $5::numeric)
        """,
        name.strip(),
        kind,
        family_member_id.strip(),
        (currency.strip() or "EUR").upper()[:3],
        initial_balance.strip() or "0",
    )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/category")
async def create_category(name: str = Form(...), grupo: str = Form("")):
    await db.execute(
        "INSERT INTO category (name, grupo) VALUES ($1, NULLIF($2, '')) ON CONFLICT (name) DO NOTHING",
        name.strip(),
        grupo.strip(),
    )
    return RedirectResponse("/settings", status_code=303)
