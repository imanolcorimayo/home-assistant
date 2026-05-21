"""Household MCP server — expense-registration tools (internal only).

Scope: the registrar agent and nothing else. Thin tools over streamable-http:
look up / add / edit expenses, add income, and manage recurring charges
(define, edit, list with paid-status, register the monthly payment). Run as
its own process (`python -m app.mcp_server`); reachable at
http://household_mcp:8000/mcp on the docker network. Never tunneled.
"""

import logging

from mcp.server.fastmcp import FastMCP

from app.services.recurring import (
    create_recurring_charge,
    list_recurring_charges,
    pay_recurring_charge,
    update_recurring_charge,
)
from app.services.transactions import (
    create_transaction,
    recent_transactions,
    update_transaction,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("mcp_server")

# host 0.0.0.0 so other containers can reach it; FastMCP defaults to 127.0.0.1.
mcp = FastMCP("household", host="0.0.0.0", port=8000)


@mcp.tool()
async def look_up_transactions(
    term: str | None = None, days: int = 90, limit: int = 15, kind: str | None = None
) -> list[dict]:
    """Consulta transacciones recientes, de la más nueva a la más vieja: sirve
    para detectar duplicados antes de registrar, ver con qué categoría se
    cargaron cosas parecidas, y para conseguir el id de una fila (para
    edit_expense).

    term: palabra a buscar en descripción o categoría (opcional).
    days: ventana hacia atrás en días (por defecto 90).
    limit: máximo de filas (máx 50).
    kind: 'expense' (gastos), 'income' (ingresos), o vacío para ambos.

    Cada fila trae kind (income/expense) y recurring_charge_id (presente cuando
    es el pago de un recurrente).
    """
    return await recent_transactions(limit=limit, days=days, term=term, kind=kind)


@mcp.tool()
async def add_expense(
    amount: float,
    description: str,
    category: str | None = None,
    transaction_date: str | None = None,
    account_hint: str | None = None,
    family_member_hint: str | None = None,
) -> dict:
    """Registra UN gasto. amount > 0 (EUR). description: texto corto.
    category: nombre de una categoría existente (si ninguna aplica, queda
    'Sin categoría'). transaction_date: YYYY-MM-DD (por defecto hoy).
    account_hint: nombre (o substring) de la cuenta a usar — ej 'Visa',
    'efectivo'. Si no matchea, cae en la cuenta default.
    family_member_hint: nombre del miembro al que se asigna el gasto.
    Si no matchea, cae en el miembro default."""
    tx_id = await create_transaction(
        kind="expense",
        amount=amount,
        description=description,
        category=category,
        transaction_date=transaction_date,
        source="manual",
        account_hint=account_hint,
        family_member_hint=family_member_hint,
    )
    if tx_id is None:
        return {"ok": False, "error": "no se pudo registrar (revisá el monto)"}
    return {"ok": True, "transaction_id": tx_id}


@mcp.tool()
async def add_income(
    amount: float,
    description: str,
    category: str | None = None,
    transaction_date: str | None = None,
    account_hint: str | None = None,
    family_member_hint: str | None = None,
) -> dict:
    """Registra UN ingreso (sueldo, transferencia recibida, etc.). amount > 0
    (EUR). description: texto corto. category: nombre de una categoría
    existente (si ninguna aplica, queda 'Sin categoría'). transaction_date:
    YYYY-MM-DD (por defecto hoy).
    account_hint: nombre (o substring) de la cuenta a usar.
    family_member_hint: nombre del miembro al que se asigna el ingreso."""
    tx_id = await create_transaction(
        kind="income",
        amount=amount,
        description=description,
        category=category,
        transaction_date=transaction_date,
        source="manual",
        account_hint=account_hint,
        family_member_hint=family_member_hint,
    )
    if tx_id is None:
        return {"ok": False, "error": "no se pudo registrar (revisá el monto)"}
    return {"ok": True, "transaction_id": tx_id}


@mcp.tool()
async def edit_expense(
    transaction_id: str,
    amount: float | None = None,
    description: str | None = None,
    category: str | None = None,
    transaction_date: str | None = None,
) -> dict:
    """Corrige un gasto YA registrado. Conseguí el transaction_id con
    look_up_transactions (cada fila trae su id). Sólo cambia los campos que mandás;
    el resto queda igual. amount > 0 (EUR); transaction_date: YYYY-MM-DD."""
    tx_id = await update_transaction(
        transaction_id=transaction_id,
        amount=amount,
        description=description,
        category=category,
        transaction_date=transaction_date,
    )
    if tx_id is None:
        return {"ok": False, "error": "no se pudo editar (id inexistente o dato inválido)"}
    return {"ok": True, "transaction_id": tx_id}


@mcp.tool()
async def list_recurring(include_inactive: bool = False) -> list[dict]:
    """Lista los pagos recurrentes (alquiler, suscripciones, etc.). Cada uno
    indica si YA está pagado este mes (paid_this_month) y la fecha del último
    pago. Usalo para ver qué falta pagar y para conseguir el id de un
    recurrente antes de pagarlo o editarlo."""
    return await list_recurring_charges(include_inactive=include_inactive)


@mcp.tool()
async def add_recurring_charge(
    name: str,
    amount: float,
    day_of_month: int,
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Define un nuevo pago recurrente (la definición, NO un pago). name: cómo
    se llama (ej 'Netflix'). amount > 0 (EUR). day_of_month: día del mes en que
    se cobra (1-31). category: categoría existente. start_date/end_date:
    YYYY-MM-DD (end_date opcional = sin fin)."""
    return await create_recurring_charge(
        name=name,
        amount=amount,
        day_of_month=day_of_month,
        category=category,
        start_date=start_date,
        end_date=end_date,
    )


@mcp.tool()
async def update_recurring_charge_tool(
    recurring_charge_id: str,
    name: str | None = None,
    amount: float | None = None,
    day_of_month: int | None = None,
    category: str | None = None,
    end_date: str | None = None,
    is_active: bool | None = None,
) -> dict:
    """Edita la definición de un recurrente (conseguí el id con list_recurring).
    Sólo cambia los campos que mandás. Para darlo de baja, is_active=False."""
    return await update_recurring_charge(
        recurring_charge_id=recurring_charge_id,
        name=name,
        amount=amount,
        day_of_month=day_of_month,
        category=category,
        end_date=end_date,
        is_active=is_active,
    )


@mcp.tool()
async def pay_recurring(
    recurring_charge_id: str,
    transaction_date: str | None = None,
    amount: float | None = None,
) -> dict:
    """Registra el PAGO de un recurrente este mes (conseguí el id con
    list_recurring). Esto crea el gasto vinculado, que es lo que marca al
    recurrente como pagado del mes. amount por defecto = el del recurrente;
    transaction_date por defecto hoy. Si ya estaba pagado este mes, la
    respuesta trae already_paid_this_month=true."""
    return await pay_recurring_charge(
        recurring_charge_id=recurring_charge_id,
        transaction_date=transaction_date,
        amount=amount,
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
