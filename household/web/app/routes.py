"""Page handlers. One function per page, each writing its own SQL.

Conventions from the schema: soft-deleted transactions are hidden with
`deleted_ts IS NULL`; tables are singular; money columns are numeric.
asyncpg uses $1, $2... placeholders (never string-format SQL values).
"""

import math
import os

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


@router.get("/categories")
async def categories(request: Request, q: str = Query("")):
    q = q.strip()
    where = "TRUE"
    args: list = []
    if q:
        args.append(f"%{q}%")
        where = f"c.name ILIKE ${len(args)}"

    rows = await db.fetch(
        f"""
        SELECT c.category_id, c.name, c.grupo, c.is_active,
               count(t.transaction_id) AS tx_count
        FROM category c
        LEFT JOIN transaction t
               ON t.category_id = c.category_id AND t.deleted_ts IS NULL
        WHERE {where}
        GROUP BY c.category_id
        ORDER BY c.name
        """,
        *args,
    )
    return templates.TemplateResponse(
        "categories.html", {"request": request, "rows": rows, "q": q}
    )


@router.post("/categories")
async def create_category(name: str = Form(...), grupo: str = Form("")):
    await db.execute(
        "INSERT INTO category (name, grupo) VALUES ($1, NULLIF($2, '')) ON CONFLICT (name) DO NOTHING",
        name.strip(),
        grupo.strip(),
    )
    return RedirectResponse("/categories", status_code=303)


@router.get("/budgets")
async def budgets(request: Request):
    rows = await db.fetch(
        "SELECT subcategory_1, limit_amount FROM monthly_budget ORDER BY subcategory_1"
    )
    return templates.TemplateResponse(
        "placeholder.html", {"request": request, "title": "Presupuestos", "rows": rows}
    )


@router.get("/settings")
async def settings(request: Request):
    return templates.TemplateResponse(
        "placeholder.html", {"request": request, "title": "Ajustes", "rows": []}
    )
