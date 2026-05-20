"""Household MCP server — expense-registration tools (internal only).

Scope: the expense registrar agent and nothing else. Two thin tools over
streamable-http: look up recent expenses (for duplicate detection + seeing
how similar purchases were categorized) and add one expense. Run as its own
process (`python -m app.mcp_server`); reachable at
http://household_mcp:8000/mcp on the docker network. Never tunneled.
"""

import logging
from datetime import date as date_cls
from datetime import timedelta

from mcp.server.fastmcp import FastMCP
from sqlalchemy import or_, select

from app.database import AsyncSessionLocal
from app.models import Transaction, TransactionKind
from app.services.transactions import create_transaction

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("mcp_server")

# host 0.0.0.0 so other containers can reach it; FastMCP defaults to 127.0.0.1.
mcp = FastMCP("household", host="0.0.0.0", port=8000)


@mcp.tool()
async def look_up_expenses(term: str | None = None, days: int = 90, limit: int = 15) -> list[dict]:
    """Devuelve gastos recientes, del más nuevo al más viejo. Úsalo ANTES de
    registrar: para detectar un posible duplicado y para ver con qué categoría
    se cargaron compras parecidas.

    term: palabra a buscar en la descripción o categoría (opcional).
    days: ventana hacia atrás en días (por defecto 90).
    limit: máximo de filas (máx 50).
    """
    since = date_cls.today() - timedelta(days=max(days, 1))
    async with AsyncSessionLocal() as s:
        q = select(Transaction).where(
            Transaction.deleted_ts.is_(None),
            Transaction.kind == TransactionKind.expense,
            Transaction.transaction_date >= since,
        )
        if term:
            like = f"%{term.strip()}%"
            q = q.where(or_(Transaction.description.ilike(like), Transaction.category.ilike(like)))
        q = q.order_by(Transaction.transaction_date.desc()).limit(min(max(limit, 1), 50))
        rows = (await s.scalars(q)).all()
        return [
            {
                "date": str(t.transaction_date),
                "amount": float(t.amount),
                "currency": t.currency,
                "category": t.category,
                "description": t.description,
            }
            for t in rows
        ]


@mcp.tool()
async def add_expense(
    amount: float,
    description: str,
    category: str | None = None,
    transaction_date: str | None = None,
) -> dict:
    """Registra UN gasto. amount > 0 (EUR). description: texto corto.
    category: nombre de una categoría existente (si ninguna aplica, queda
    'Sin categoría'). transaction_date: YYYY-MM-DD (por defecto hoy)."""
    tx_id = await create_transaction(
        kind="expense",
        amount=amount,
        description=description,
        category=category,
        transaction_date=transaction_date,
        source="manual",
    )
    if tx_id is None:
        return {"ok": False, "error": "no se pudo registrar (revisá el monto)"}
    return {"ok": True, "transaction_id": tx_id}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
