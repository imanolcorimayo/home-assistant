"""The chat agent — single entry point `handle(message, member)`.

For now this IS the registrar (writes the ledger). The seam is deliberate: the
chat endpoint only ever calls `handle`, so later we can swap this body for an
orchestrator that routes between registrar / consultor / observer WITHOUT
touching the endpoint or the UI.

Ported from household's run_agent: same google-genai SDK, same model-rotation
loop. Two changes — no MCP (tools are in-process closures), and family_id /
member_id are bound from the SESSION inside the closures, never exposed to the
model as parameters. That's the tenant-isolation + identity boundary.
"""

import asyncio
import logging
import uuid
from datetime import date

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app import config, context, db, tools

log = logging.getLogger("agent")

_RETRIABLE_CODES = {429, 500, 502, 503, 504}

# In-memory history per member (MVP: single worker, lost on restart). Only text
# turns are kept — enough for follow-ups ("sí, guardalo igual") without resending
# tool traffic.
_SESSIONS: dict[str, list] = {}
_MAX_TURNS = 16
_MAX_ITERS = 8  # safety cap on tool-loop rounds per message

SYSTEM_TMPL = (
    "Sos el asistente financiero de una familia. Hoy es {today}.\n"
    "Quien te escribe es {name}: registrá los movimientos a su nombre.\n\n"
    "REGLA CRÍTICA: toda acción sobre los datos (registrar, editar, borrar, pagar un "
    "recurrente, crear categoría/cuenta/presupuesto) se hace ÚNICAMENTE llamando a la "
    "herramienta correspondiente. No tenés forma de hacerlo 'a mano': si no llamás la tool, "
    "NO pasó. NUNCA digas que registraste/borraste/creaste algo sin haber llamado la "
    "herramienta en este mismo turno.\n\n"
    "Herramientas:\n"
    "- look_up_transactions: ver transacciones (detectar duplicados, ver categorías de cosas "
    "parecidas, conseguir el id de una fila, o ver borrados con include_deleted=True).\n"
    "- add_expense / add_income: registrar un gasto o un ingreso.\n"
    "- edit_expense: corregir uno ya registrado (ubicá el id antes con look_up_transactions).\n"
    "- delete_transaction (soft-delete, recuperable) / restore_transaction.\n"
    "- list_recurring, add_recurring_charge, update_recurring_charge_tool, pay_recurring "
    "(un recurrente queda pagado del mes recién cuando le registrás el pago).\n"
    "- create_category (una) / create_categories (varias de una) / create_budget / create_account.\n\n"
    "Cómo te manejás:\n"
    "- Si el mensaje describe un gasto/ingreso claro, llamá add_expense/add_income YA: deducí "
    "el monto (EUR) y una descripción corta; la fecha es hoy salvo que digan otra (si falta el "
    "año, asumí {year}). Después contá en una línea qué registraste.\n"
    "- VARIAS acciones en un turno: si la persona menciona varios gastos, registralos TODOS "
    "(una llamada a add_expense por cada uno). Si te piden armar una lista de categorías, usá "
    "create_categories con todos los nombres de una.\n"
    "- CATEGORÍAS: son de rutina, podés crearlas sin pedir permiso. Sólo podés usar una categoría "
    "que EXISTA (ver lista abajo); si la que corresponde no está, creala primero (create_category) "
    "y registrá con esa. Las herramientas devuelven category_used: si terminaste en 'Sin "
    "categoría', decílo — NUNCA digas que usaste una categoría que no existe.\n"
    "- Pedí confirmación ANTES de escribir sólo si la decisión es de la persona: dato ambiguo, "
    "posible duplicado (decí fecha y monto del parecido), o dudás de si lo querían registrar.\n"
    "- BORRAR: SIEMPRE confirmá primero (mostrá fecha, monto, descripción y categoría) y esperá "
    "un 'sí' explícito ANTES de llamar delete_transaction; nunca en el mismo turno.\n"
    "- CUENTAS casi nunca cambian: confirmá antes de crear una. Si mencionan una cuenta puntual, "
    "pasala como account_hint.\n\n"
    "Categorías existentes: {categories}.\n\n"
    "{family_context}\n\n"
    "Últimos gastos registrados (más nuevo primero):\n{recent}\n\n"
    "Respondé en español, breve y directo: sin saludos, sin emojis. Resaltá el monto con "
    "**negrita**."
)


def _is_retriable(exc: BaseException) -> bool:
    if isinstance(exc, (genai_errors.ClientError, genai_errors.ServerError)):
        return getattr(exc, "code", None) in _RETRIABLE_CODES or "RESOURCE_EXHAUSTED" in str(exc)
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_retriable(sub) for sub in exc.exceptions)
    return False


async def _generate(client, contents, cfg):
    """One model call with rotation: try each model in order, rotating on a
    retriable error (429/5xx). Returns (response, model_used)."""
    last_err: BaseException | None = None
    for model in config.AGENT_MODELS:
        try:
            resp = await client.aio.models.generate_content(
                model=model, contents=contents, config=cfg
            )
            return resp, model
        except BaseException as exc:  # noqa: BLE001 - inspected by _is_retriable
            if _is_retriable(exc):
                log.warning("model %s unavailable, rotating", model)
                last_err = exc
                continue
            raise
    raise last_err or RuntimeError("no model produced a response")


def _user_turn(text: str):
    return types.Content(role="user", parts=[types.Part(text=text)])


def _model_turn(text: str):
    return types.Content(role="model", parts=[types.Part(text=text)])


def _extract_usage(resp):
    um = getattr(resp, "usage_metadata", None)
    if um is None:
        return None, None, None
    return (
        getattr(um, "prompt_token_count", None),
        getattr(um, "candidates_token_count", None),
        getattr(um, "total_token_count", None),
    )


async def _log_run(family_id, member_id, session_id, input_text, reply_text,
                   model_used, tool_calls, error, p_tok=None, o_tok=None, t_tok=None):
    """Persist one agent_run row, fire-and-forget. Swallows its own errors."""
    import json
    try:
        await db.execute(
            """
            INSERT INTO agent_run
                (family_id, member_id, session_id, input_text, reply_text,
                 model_used, tool_calls, error, prompt_tokens, output_tokens, total_tokens)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11)
            """,
            family_id, member_id, session_id, input_text, reply_text,
            model_used, json.dumps(tool_calls), error, p_tok, o_tok, t_tok,
        )
    except Exception:  # noqa: BLE001 - background logging is best-effort
        log.warning("agent_run logging failed", exc_info=True)


def _build_tools(family_id, member_id) -> list:
    """The registrar's tools, each a closure binding family_id/member_id so the
    model never sees (or sets) them. Docstrings ARE the schema the model reads.

    NOTE: the Gemini Developer API rejects nullable params in tool schemas, so
    optional args use sentinels ("" for text, 0 for numbers) instead of None.
    `_s`/`_n` convert those sentinels back to None before hitting the tools."""

    def _s(v):  # text sentinel: "" → None
        return v or None

    def _n(v):  # number sentinel: 0 → None
        return v if v else None

    async def look_up_transactions(term: str = "", days: int = 90, limit: int = 15,
                                   kind: str = "", include_deleted: bool = False) -> list:
        """Consulta transacciones recientes (más nueva primero). Sirve para detectar
        duplicados, ver categorías de cosas parecidas, conseguir el id de una fila, o
        encontrar borrados. term: palabra a buscar (vacío = sin filtro). kind:
        'expense', 'income' o vacío. include_deleted=True para ver borrados."""
        return await tools.look_up_transactions(family_id, term=_s(term), days=days,
                                                limit=limit, kind=_s(kind),
                                                include_deleted=include_deleted)

    async def add_expense(amount: float, description: str, category: str = "",
                          transaction_date: str = "", account_hint: str = "") -> dict:
        """Registra UN gasto. amount > 0 (EUR). description: texto corto. category:
        nombre de una categoría existente (vacío si ninguna aplica → 'Sin categoría').
        transaction_date: YYYY-MM-DD (vacío = hoy). account_hint: nombre de la cuenta
        (vacío = cuenta por defecto)."""
        return await tools.create_transaction(family_id, member_id, "expense", amount,
                                             description=description, category=_s(category),
                                             transaction_date=_s(transaction_date),
                                             account_hint=_s(account_hint))

    async def add_income(amount: float, description: str, category: str = "",
                         transaction_date: str = "", account_hint: str = "") -> dict:
        """Registra UN ingreso (sueldo, transferencia recibida, etc.). Mismos campos
        que add_expense (los vacíos toman el valor por defecto)."""
        return await tools.create_transaction(family_id, member_id, "income", amount,
                                            description=description, category=_s(category),
                                            transaction_date=_s(transaction_date),
                                            account_hint=_s(account_hint))

    async def edit_expense(transaction_id: str, amount: float = 0, description: str = "",
                           category: str = "", transaction_date: str = "") -> dict:
        """Corrige un movimiento YA registrado. Conseguí el transaction_id con
        look_up_transactions. Sólo cambia los campos que mandás (vacío/0 = sin cambio)."""
        return await tools.update_transaction(family_id, transaction_id, amount=_n(amount),
                                            description=_s(description), category=_s(category),
                                            transaction_date=_s(transaction_date))

    async def delete_transaction(transaction_id: str) -> dict:
        """Borra (soft-delete) un movimiento. SÓLO después de confirmación explícita
        de la persona. Recuperable con restore_transaction. Conseguí el id con
        look_up_transactions."""
        return await tools.soft_delete_transaction(family_id, transaction_id)

    async def restore_transaction(transaction_id: str) -> dict:
        """Recupera un movimiento borrado (limpia el borrado). Conseguí el id con
        look_up_transactions pasando include_deleted=True."""
        return await tools.restore_transaction(family_id, transaction_id)

    async def list_recurring(include_inactive: bool = False) -> list:
        """Lista los pagos recurrentes. Cada uno indica si ya está pagado este mes
        (paid_this_month) y trae su id (para pagarlo o editarlo)."""
        return await tools.list_recurring(family_id, include_inactive=include_inactive)

    async def add_recurring_charge(name: str, amount: float, day_of_month: int,
                                   category: str = "", account_hint: str = "",
                                   start_date: str = "", end_date: str = "") -> dict:
        """Define un nuevo pago recurrente (la definición, NO un pago). name: ej
        'Netflix'. amount > 0. day_of_month: 1-31. category: existente. start/end_date:
        YYYY-MM-DD (end_date vacío = sin fin)."""
        return await tools.create_recurring_charge(family_id, name=name, amount=amount,
                                                 day_of_month=day_of_month, category=_s(category),
                                                 account_hint=_s(account_hint),
                                                 start_date=_s(start_date), end_date=_s(end_date))

    async def update_recurring_charge_tool(recurring_charge_id: str, name: str = "",
                                           amount: float = 0, day_of_month: int = 0,
                                           category: str = "", end_date: str = "",
                                           status: str = "") -> dict:
        """Edita la definición de un recurrente (id de list_recurring). Sólo cambia los
        campos pasados (vacío/0 = sin cambio). status: 'active' o 'inactive' para
        activar/dar de baja (vacío = sin cambio)."""
        is_active = {"active": True, "inactive": False}.get(status.strip().lower())
        return await tools.update_recurring_charge(family_id, recurring_charge_id, name=_s(name),
                                                 amount=_n(amount), day_of_month=_n(day_of_month),
                                                 category=_s(category), end_date=_s(end_date),
                                                 is_active=is_active)

    async def pay_recurring(recurring_charge_id: str, transaction_date: str = "",
                            amount: float = 0) -> dict:
        """Registra el PAGO de un recurrente este mes (id de list_recurring). Crea el
        gasto vinculado. amount 0 = usa el del recurrente; fecha vacía = hoy. Si ya
        estaba pagado este mes, devuelve already_paid_this_month=true."""
        return await tools.pay_recurring(family_id, member_id, recurring_charge_id,
                                       transaction_date=_s(transaction_date), amount=_n(amount))

    async def create_category(name: str, grupo: str = "") -> dict:
        """Crea UNA categoría nueva. grupo opcional: 'variable', 'fijo' o 'ingreso'
        (vacío = sin grupo). Idempotente por nombre."""
        return await tools.create_category(family_id, name, grupo=_s(grupo))

    async def create_categories(names: list[str], grupo: str = "") -> dict:
        """Crea VARIAS categorías de una (idempotente por nombre). Usala cuando te
        piden armar una lista de categorías. grupo opcional aplica a todas. Devuelve
        las creadas y las que ya existían."""
        return await tools.create_categories(family_id, names, grupo=_s(grupo))

    async def create_budget(category: str, limit_amount: float) -> dict:
        """Define/actualiza el presupuesto mensual de una categoría. limit_amount > 0."""
        return await tools.create_budget(family_id, category, limit_amount)

    async def create_account(name: str, kind: str, currency: str = "EUR",
                             initial_balance: float = 0) -> dict:
        """Crea una cuenta compartida. kind: 'checking', 'savings', 'cash' o
        'credit_card'. Confirmá antes (nombre y tipo)."""
        return await tools.create_account(family_id, name, kind, currency=currency,
                                        initial_balance=initial_balance)

    return [look_up_transactions, add_expense, add_income, edit_expense,
            delete_transaction, restore_transaction, list_recurring,
            add_recurring_charge, update_recurring_charge_tool, pay_recurring,
            create_category, create_categories, create_budget, create_account]


async def handle(message: str, member) -> str:
    """Run one chat message through the agent. `member` is the asyncpg Record of
    the logged-in user; family_id/member_id are taken from it (the session),
    never from the message. Returns the reply text."""
    family_id = member["family_id"]
    member_id = member["member_id"]
    session_id = str(member_id)

    ctx = await context.build_family_context(family_id)
    recent = await tools.recent_transactions(family_id, limit=20, kind="expense")
    today = date.today()
    system = SYSTEM_TMPL.format(
        today=today.isoformat(),
        year=today.year,
        name=member["full_name"],
        categories=", ".join(ctx.get("categories", [])) or "(ninguna)",
        family_context=context.format_context_for_prompt(ctx),
        recent=context.format_recent(recent),
    )

    history = _SESSIONS.get(session_id, [])
    contents = history + [_user_turn(message)]

    tool_list = _build_tools(family_id, member_id)
    tool_map = {fn.__name__: fn for fn in tool_list}
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0.1,
        tools=tool_list,
        # We run the tool loop ourselves (below), so turn OFF the SDK's automatic
        # function calling — it would run the loop internally and hide each step,
        # leaving no point to log a tool call live, notify, or enforce policy.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        # gemini-2.5-flash's thinking mode intermittently returns an empty
        # response on this prompt+toolset; budget=0 makes tool-calling reliable.
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    tool_calls: list[dict] = []          # every call this turn → agent_run
    p_sum = o_sum = t_sum = 0            # tokens summed across iterations
    model_used = None
    reply = "(sin respuesta)"
    try:
        for _ in range(_MAX_ITERS):
            resp, model_used = await _generate(client, contents, cfg)
            pt, ot, tt = _extract_usage(resp)
            p_sum += pt or 0; o_sum += ot or 0; t_sum += tt or 0

            calls = resp.function_calls or []
            if not calls:
                reply = (resp.text or "").strip() or "(sin respuesta)"
                break

            # The control point AFC hid: record the model's call turn, then run
            # each tool ourselves — logging (and later notifying) right here.
            contents.append(resp.candidates[0].content)
            responses = []
            for c in calls:
                args = dict(c.args or {})
                fn = tool_map.get(c.name)
                if fn is None:
                    result = {"ok": False, "error": f"herramienta desconocida: {c.name}"}
                else:
                    try:
                        result = await fn(**args)
                    except Exception:  # noqa: BLE001 - surface as a tool error, keep the loop alive
                        log.exception("tool %s failed", c.name)
                        result = {"ok": False, "error": "fallo interno de la herramienta"}
                tool_calls.append({"name": c.name, "args": args})
                responses.append(
                    types.Part.from_function_response(name=c.name, response={"result": result})
                )
            contents.append(types.Content(role="user", parts=responses))
        else:
            log.warning("agent hit max iterations (%s)", _MAX_ITERS)
    except BaseException as exc:  # noqa: BLE001 - log the failed run, then re-raise
        asyncio.create_task(
            _log_run(family_id, member_id, session_id, message, None, model_used,
                     tool_calls, str(exc), p_sum, o_sum, t_sum)
        )
        raise

    asyncio.create_task(
        _log_run(family_id, member_id, session_id, message, reply, model_used,
                 tool_calls, None, p_sum, o_sum, t_sum)
    )
    _SESSIONS[session_id] = (history + [_user_turn(message), _model_turn(reply)])[-_MAX_TURNS:]
    return reply
