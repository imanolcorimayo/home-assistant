"""Page handlers. One function per page, each writing its own SQL.

Conventions from the schema: soft-deleted transactions are hidden with
`deleted_ts IS NULL`; tables are singular; money columns are numeric.
asyncpg uses $1, $2... placeholders (never string-format SQL).
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/")
async def dashboard(request: Request):
    # Monthly income vs expense for the last 12 months.
    rows = await db.fetch(
        """
        SELECT to_char(date_trunc('month', transaction_date), 'YYYY-MM') AS month,
               kind,
               sum(amount) AS total
        FROM transaction
        WHERE deleted_ts IS NULL
          AND transaction_date >= (current_date - interval '12 months')
        GROUP BY 1, 2
        ORDER BY 1 DESC, 2
        """
    )
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "rows": rows}
    )


@router.get("/transactions")
async def transactions(request: Request):
    rows = await db.fetch(
        """
        SELECT t.transaction_id, t.transaction_date, t.kind, t.amount, t.currency,
               t.category, t.description, a.name AS account, m.full_name AS member
        FROM transaction t
        JOIN account a       ON a.account_id = t.account_id
        JOIN family_member m ON m.family_member_id = t.family_member_id
        WHERE t.deleted_ts IS NULL
        ORDER BY t.transaction_date DESC, t.created_ts DESC
        LIMIT 100
        """
    )
    return templates.TemplateResponse(
        "transactions.html", {"request": request, "rows": rows}
    )


@router.get("/categories")
async def categories(request: Request):
    rows = await db.fetch(
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
        "categories.html", {"request": request, "rows": rows}
    )


@router.post("/categories")
async def create_category(name: str = Form(...), grupo: str = Form("")):
    # Idempotent insert; ignore if the name already exists.
    await db.execute(
        """
        INSERT INTO category (name, grupo)
        VALUES ($1, NULLIF($2, ''))
        ON CONFLICT (name) DO NOTHING
        """,
        name.strip(),
        grupo.strip(),
    )
    return RedirectResponse("/categories", status_code=303)
