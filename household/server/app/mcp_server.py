"""Household MCP server — tools for both household agents (internal only).

Scope: the registrar agent (writes) AND the consultor agent (reads). Thin
tools over streamable-http: look up / add / edit expenses, add income, manage
recurring charges, plus a battery of read-only analytics. Each agent's
SYSTEM_TMPL declares which tools it uses; the SDK exposes all of them, but
the agent only invokes the ones its prompt names. Run as its own process
(`python -m app.mcp_server`); reachable at http://household_mcp:8000/mcp on
the docker network. Never tunneled.
"""

import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP

from app.services import analytics
from app.services.recurring import (
    create_recurring_charge,
    list_recurring_charges,
    pay_recurring_charge,
    update_recurring_charge,
)
from app.services.transactions import (
    create_account as create_account_svc,
    create_budget as create_budget_svc,
    create_category as create_category_svc,
    create_member as create_member_svc,
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


@mcp.tool()
async def create_category(name: str, grupo: str | None = None) -> dict:
    """Crea una categoría nueva (estructura del hogar, NO un gasto). name: el
    nombre (ej 'Mascotas'). grupo (opcional): 'variable', 'fijo' o 'ingreso',
    para agrupar en los análisis. Es idempotente: si ya existe una con ese
    nombre, te la devuelve sin duplicar (created=false). Confirmá con la
    persona antes de crear categorías nuevas."""
    return await create_category_svc(name=name, grupo=grupo)


@mcp.tool()
async def create_budget(category: str, limit_amount: float) -> dict:
    """Define (o actualiza) el presupuesto mensual de una categoría.
    category: nombre de la categoría. limit_amount > 0 (EUR). Si ya había un
    presupuesto para esa categoría, se actualiza el límite (created=false).
    Conviene que la categoría exista; confirmá el monto con la persona."""
    return await create_budget_svc(category=category, limit_amount=limit_amount)


@mcp.tool()
async def create_member(full_name: str) -> dict:
    """Crea un miembro de la familia. full_name: nombre completo. Idempotente
    por nombre. OJO: los miembros casi nunca cambian — NO inventes ni crees
    miembros por tu cuenta; hacelo sólo si la persona lo pide explícitamente y
    confirmá antes."""
    return await create_member_svc(full_name=full_name)


@mcp.tool()
async def create_account(
    name: str,
    kind: str,
    family_member_hint: str | None = None,
    currency: str = "EUR",
    initial_balance: float = 0,
) -> dict:
    """Crea una cuenta (ej 'Fer Efectivo'). name: nombre de la cuenta.
    kind: 'checking' (banco), 'savings' (ahorro), 'cash' (efectivo) o
    'credit_card' (tarjeta de crédito). family_member_hint (opcional): nombre
    del miembro dueño; si no matchea, queda compartida. currency por defecto
    EUR. initial_balance opcional. OJO: las cuentas casi nunca cambian — NO las
    inventes; creá una sólo si la persona lo pide explícitamente y confirmá
    antes (nombre y tipo)."""
    return await create_account_svc(
        name=name,
        kind=kind,
        family_member_hint=family_member_hint,
        currency=currency,
        initial_balance=initial_balance,
    )


# ============================================================
# Analytics tools — read-only. Consumed by the Consultor agent.
# ============================================================


@mcp.tool()
async def spending_by_period(
    start: str,
    end: str,
    group_by: str = "category",
    member_hint: Optional[str] = None,
    account_hint: Optional[str] = None,
) -> list[dict]:
    """Suma de GASTOS entre start y end (formato YYYY-MM-DD), agrupados.
    group_by: 'category' (default), 'member', 'account', 'day', 'week', 'month'.
    member_hint / account_hint: filtran fuzzy a un miembro o cuenta puntual.
    Devuelve filas {group, total, count} ordenadas por monto descendente."""
    return await analytics.spending_by_period(
        start, end, group_by, member_hint=member_hint, account_hint=account_hint
    )


@mcp.tool()
async def income_by_period(
    start: str,
    end: str,
    group_by: str = "category",
    member_hint: Optional[str] = None,
    account_hint: Optional[str] = None,
) -> list[dict]:
    """Suma de INGRESOS entre start y end (formato YYYY-MM-DD), agrupados.
    Mismo shape y parámetros que spending_by_period pero kind='income'."""
    return await analytics.income_by_period(
        start, end, group_by, member_hint=member_hint, account_hint=account_hint
    )


@mcp.tool()
async def balance_for_period(start: str, end: str) -> dict:
    """Balance del período (YYYY-MM-DD): suma de ingresos, suma de gastos y
    neto (income - expense). Útil para 'cómo viene el mes', 'balance del año'."""
    return await analytics.balance_for_period(start, end)


@mcp.tool()
async def savings_rate(start: str, end: str) -> dict:
    """Tasa de ahorro = (income - expense) / income * 100 en el período
    (YYYY-MM-DD). rate_pct=None si no hubo ingresos."""
    return await analytics.savings_rate(start, end)


@mcp.tool()
async def category_trend(category: str, months: int = 12) -> list[dict]:
    """Serie mensual de gastos en UNA categoría — últimos N meses (incluido el
    actual). Devuelve [{month: 'YYYY-MM', total, count}]. Útil para
    'tendencia de transporte', 'cómo viene supermercado últimos 6 meses'."""
    return await analytics.category_trend(category, months=months)


@mcp.tool()
async def top_categories(
    start: str, end: str, kind: str = "expense", limit: int = 5
) -> list[dict]:
    """Top N categorías de gasto o ingreso en el período (YYYY-MM-DD).
    kind: 'expense' o 'income'. limit acotado a [1, 50]."""
    return await analytics.top_categories(start, end, kind=kind, limit=limit)


@mcp.tool()
async def period_comparison(
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
    kind: str = "expense",
) -> dict:
    """Compara la suma total de gasto o ingreso entre dos períodos (YYYY-MM-DD).
    Devuelve {a, b, diff: b-a, pct_change}. pct_change=None si a==0."""
    return await analytics.period_comparison(
        (period_a_start, period_a_end), (period_b_start, period_b_end), kind=kind
    )


@mcp.tool()
async def budget_status(year_month: Optional[str] = None) -> list[dict]:
    """Estado de cada presupuesto mensual: limit, spent, remaining, pct_used.
    year_month en formato 'YYYY-MM' (default: mes actual). Útil para
    'voy bien con el presupuesto', 'cuánto me queda de supermercado'."""
    return await analytics.budget_status(year_month=year_month)


@mcp.tool()
async def pending_recurring(year_month: Optional[str] = None) -> list[dict]:
    """Cargos recurrentes ACTIVOS que NO se pagaron en el mes pedido.
    year_month: 'YYYY-MM' (default: mes actual). Devuelve [{name, amount,
    day_of_month, category, due_in_days}]. due_in_days es negativo si ya
    venció. Sólo útil en mes actual / pasado."""
    return await analytics.pending_recurring(year_month=year_month)


@mcp.tool()
async def account_balances() -> list[dict]:
    """Saldo actual de cada cuenta activa = saldo inicial + ingresos - gastos
    (sólo movimientos vivos). Devuelve [{account, kind, currency, balance}]."""
    return await analytics.account_balances()


@mcp.tool()
async def average_by_category(
    kind: str = "expense",
    start: Optional[str] = None,
    end: Optional[str] = None,
    group_by: str = "month",
) -> list[dict]:
    """Promedio por categoría de gasto o ingreso. group_by='month' divide el
    total por la cantidad de meses con actividad (no por meses calendario);
    group_by='tx' es promedio por transacción. start/end opcionales en
    YYYY-MM-DD; default últimos 12 meses."""
    return await analytics.average_by_category(
        kind=kind, start=start, end=end, group_by=group_by
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
