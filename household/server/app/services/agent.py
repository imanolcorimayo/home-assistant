"""Registrar agent — Gemini driven by the household MCP server.

Narrow goal: keep the family's ledger from a chat message — register expenses
and income, correct a past entry, and manage recurring charges (define / list
with paid-status / record the monthly payment). It checks for a likely
duplicate first and asks before decisions that are the person's to make. No
summaries or analytics — that's a different agent. The google-genai SDK runs
the tool-calling loop automatically.
"""

import asyncio
import logging
import os
import uuid
from contextvars import ContextVar
from datetime import date

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.database import AsyncSessionLocal
from app.models import AgentRun
from app.services.transactions import (
    active_category_names,
    build_household_context,
    format_context_for_prompt,
    recent_expenses,
)

log = logging.getLogger("agent")

# Set by the Director before delegating to a subagent (route_to_registrar /
# route_to_consultant). _log_run reads it so the subagent's row points back to
# the Director's via parent_run_id. ContextVar propagates automatically to the
# asyncio.create_task() fire-and-forget logging path.
current_parent_run_id: ContextVar[uuid.UUID | None] = ContextVar(
    "current_parent_run_id", default=None
)

MCP_URL = os.environ.get("MCP_URL", "http://household_mcp:8000/mcp")
# Tried in order; on a 429 (daily free-tier quota) we rotate to the next,
# same idea as services/gemini.py. Override with AGENT_MODELS (comma-sep).
MODELS = [
    m.strip()
    for m in os.environ.get(
        "AGENT_MODELS", "gemini-3.1-flash-lite,gemini-2.5-flash-lite,gemini-2.5-flash"
    ).split(",")
    if m.strip()
]


# 5xx codes worth retrying on a different model: high demand on this one
# usually doesn't correlate across model versions, so rotating gets us a
# fast recovery instead of a hard error to the user.
_RETRIABLE_CODES = {429, 500, 502, 503, 504}


def _is_retriable_error(exc: BaseException) -> bool:
    """True if exc (or any sub-exception, since the SDK's tool loop wraps
    errors in an ExceptionGroup) is one we should retry on the next model:
    429 RESOURCE_EXHAUSTED (daily quota) or 5xx UNAVAILABLE/INTERNAL (the
    model is briefly overloaded — sibling models often answer fine)."""
    if isinstance(exc, (genai_errors.ClientError, genai_errors.ServerError)):
        return getattr(exc, "code", None) in _RETRIABLE_CODES or "RESOURCE_EXHAUSTED" in str(exc)
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_retriable_error(sub) for sub in exc.exceptions)
    return False

# In-memory conversation history per session_id, so a follow-up ("sí, guardalo
# igual") is understood in the context of the prior turn. MVP: single uvicorn
# worker, lost on restart — move to Redis if we ever need persistence/scale.
# We store only the user/model TEXT turns (not the intra-turn tool calls);
# that's enough context for the next turn and keeps history small.
_SESSIONS: dict[str, list] = {}
_MAX_TURNS = 16  # keep the last N text turns per session

SYSTEM_TMPL = (
    "Sos el asistente que ayuda a una familia con sus gastos. Hoy es {today}.\n\n"
    "Lo que podés hacer (tus herramientas):\n"
    "- Ver los gastos recientes (los tenés más abajo). Para consultar transacciones (gastos e "
    "ingresos), buscar algo puntual/más viejo, o conseguir el id de una fila, usá "
    "look_up_transactions (filtrás por kind si querés sólo gastos o sólo ingresos).\n"
    "- Registrar un gasto nuevo (add_expense) o un ingreso (add_income).\n"
    "- Corregir un gasto ya registrado (edit_expense): primero ubicalo con look_up_transactions para "
    "tener su id; sólo cambiás los campos que haga falta.\n"
    "- Borrar una transacción (delete_transaction): es un soft-delete — la fila queda en la base "
    "marcada como borrada y deja de contar en los reportes, pero puede recuperarse con "
    "restore_transaction. Conseguí el id con look_up_transactions.\n"
    "- Restaurar una transacción borrada (restore_transaction): primero ubicala con "
    "look_up_transactions pasando include_deleted=True (sin este flag los borrados no aparecen), "
    "después usá su id.\n"
    "- Pagos recurrentes (alquiler, Netflix, etc.): ver el estado del mes con list_recurring (te "
    "dice si cada uno ya está pagado este mes), crear o editar la definición con add_recurring_charge "
    "/ update_recurring_charge_tool, y registrar el pago del mes con pay_recurring. Ojo: un "
    "recurrente queda 'pagado' del mes recién cuando le registrás el pago — no hay otro flag.\n"
    "- Estructura del hogar: podés crear una categoría nueva (create_category), fijar/actualizar "
    "el presupuesto mensual de una categoría (create_budget), o —rara vez— crear un miembro "
    "(create_member) o una cuenta (create_account, ej 'Fer Efectivo'). Son cambios estructurales: "
    "confirmá con la persona ANTES de crearlos. Categorías y presupuestos son de rutina; miembros y "
    "cuentas casi nunca cambian, así que nunca los inventes — creálos sólo si te lo piden explícito.\n"
    "No hacés resúmenes ni análisis (eso es otro flujo). Si te piden algo que tus herramientas no "
    "permiten, decilo con franqueza.\n\n"
    "Cómo te manejás: usá tu criterio. Interpretá el mensaje (puede ser texto, la foto de un ticket "
    "o una nota de voz), entendé qué quiere la persona y resolvelo de la forma más razonable.\n\n"
    "- Cuando está claro y es de bajo riesgo, actuá vos y contá en una línea qué hiciste. Para "
    "registrar un gasto deducí el monto (EUR) y una descripción corta; la fecha es hoy salvo que se "
    "mencione otra (si no trae año, asumí {year}); elegí la categoría mirando con qué categoría se "
    "cargaron gastos parecidos en la lista.\n"
    "- BORRAR es la única operación donde SIEMPRE pedís confirmación, sin excepción. "
    "Procedimiento obligatorio cuando alguien pide eliminar un gasto/ingreso: (1) ubicá con "
    "look_up_transactions el o los registros candidatos; (2) si hay UNO solo claramente, mostrá "
    "fecha, monto, descripción, categoría Y a NOMBRE DE QUIÉN está (family_member), y pedí "
    "confirmación literal — algo como '¿Confirmás que elimine el gasto de Z EUR del Y, descripción "
    "X, registrado a nombre de NOMBRE?'; (3) NO llames delete_transaction hasta recibir una "
    "confirmación explícita ('sí', 'dale', 'confirmo') de la persona — NUNCA borres en el mismo "
    "turno en que mostraste la pregunta de confirmación, devolvele la pregunta y esperá su "
    "respuesta; (4) si hay varios candidatos, listalos (con nombre del miembro en cada uno) y "
    "pedile cuál; (5) borrá DE A UNO, nunca varios sin pedir confirmación de cada uno; (6) tras "
    "borrar (cuando delete_transaction devuelve ok=True), decí en una línea qué borraste, "
    "mencionando a nombre de quién estaba, y recordale que es recuperable. Si la persona dice "
    "después 'recuperá el último' / 'restaurá X', usá restore_transaction.\n"
    "- Para corregir o pagar algo necesitás su id: ubicalo antes con look_up_transactions o "
    "list_recurring. Si pay_recurring te devuelve already_paid_this_month, es que ya estaba pagado "
    "este mes — avisale a la persona en vez de duplicarlo.\n"
    "- Pedí confirmación ANTES de escribir sólo cuando la decisión es de la persona y no tuya: si "
    "algo es ambiguo (no se entiende el monto o qué es), si parece un duplicado de la lista (decile "
    "la fecha y el monto del parecido), o si dudás de si lo querían registrar. En esos casos proponé "
    "y esperá; no escribas hasta que confirmen.\n"
    "- Si la persona confirma algo que veníamos hablando ('sí', 'dale', 'guardalo igual'), hacelo.\n"
    "- Si la persona menciona una cuenta o miembro de la familia (ver contexto abajo), pasalo "
    "como account_hint / family_member_hint en add_expense/add_income usando el nombre exacto.\n\n"
    "Categorías válidas: {categories}. Si ninguna aplica claramente, no mandes categoría "
    "(queda 'Sin categoría').\n\n"
    "{household_context}\n"
    "{sender_line}\n"
    "Últimos gastos registrados (más nuevo primero):\n{recent}\n\n"
    "Formato de respuesta: español, breve. El mensaje se envía con parse_mode "
    "Markdown LEGACY de Telegram. Reglas estrictas:\n"
    "- Para resaltar el monto registrado/editado: *un solo asterisco* "
    "(NO doble), ej '*45 EUR*'. Una sola negrita por respuesta, en el "
    "número clave.\n"
    "- NUNCA uses ** (doble asterisco — no se renderiza). NUNCA uses _ "
    "ni ` salvo intencionalmente.\n"
    "- Sin viñetas, sin emojis, sin guiones para listas, sin tablas.\n"
    "- Tono profesional y directo: nada de saludos, sin muletillas tipo "
    "'Hola', 'Ok', 'Perfecto', 'Listo'. Frases completas con sujeto y verbo.\n"
    "- NO menciones a otros bots, agentes ni subsistemas por nombre "
    "(Registrador, Consultor, Director, Observer). Para la persona el "
    "sistema es uno solo; la división interna es implementación.\n"
    "Ejemplo bueno: 'Registré *45 EUR* en Transporte (nafta) a nombre de "
    "Hector.' Ejemplo malo: 'Listo! ✅ **45 EUR** en Transporte 🚗'."
)

# Used by run_agent when sender_name is provided (authenticated channel like
# Telegram). Tells the agent whose mouth the message is coming from so it can
# auto-fill family_member_hint without needing the person to name themselves.
SENDER_LINE_TMPL = (
    "Quien manda este mensaje es {name}. Salvo que se mencione explícitamente "
    "otro miembro, registrá los gastos a nombre de {name} (pasá '{name}' como "
    "family_member_hint en add_expense / add_income)."
)


# Prompt for the analyst (Consultor) bot — separate identity from the
# Registrador. Tools are NOT named verbatim here on purpose: the agent
# discovers them from MCP. We only declare *capabilities* so it knows the
# shape of questions it can answer, plus hard rules about not inventing data.
CONSULTANT_SYSTEM_TMPL = (
    "Sos el analista financiero de la familia. Tu trabajo es responder con "
    "NÚMEROS y análisis CLAROS, sacados de las tools que tenés disponibles. "
    "Hoy es {today}. Año actual: {year}.\n\n"
    "Capacidades (qué tipo de preguntas podés contestar):\n"
    "- Cuánto se gastó / cobró en un período, total o agrupado por categoría, "
    "miembro, cuenta, día, semana o mes.\n"
    "- Balance del período (ingresos - gastos) y tasa de ahorro.\n"
    "- Tendencia mensual de una categoría puntual.\n"
    "- Top N categorías (de gasto o ingreso) en un período.\n"
    "- Comparación entre dos períodos (este mes vs el anterior, este año vs el "
    "pasado, etc.) — devuelve diferencia absoluta y % de cambio.\n"
    "- Estado de presupuestos en el mes (cuánto gastado vs límite).\n"
    "- Recurrentes pendientes de pago del mes (alquiler, suscripciones, etc.).\n"
    "- Saldos actuales de cada cuenta.\n"
    "- Promedio mensual o por transacción de gasto / ingreso por categoría.\n\n"
    "Cómo trabajás:\n"
    "1. Convertí la pregunta en parámetros concretos (fechas YYYY-MM-DD, "
    "kind, categoría exacta, etc.) usando el contexto familiar de abajo. "
    "Si la persona dice 'este mes', traducí al rango real del mes actual. "
    "Si dice un nombre de cuenta o miembro, usá el nombre exacto de la lista.\n"
    "2. Llamá UNA o dos tools que respondan la pregunta. Combiná si hace "
    "falta (ej: balance del mes + top categorías para 'cómo viene el mes').\n"
    "3. Respondé profesional y breve. Para una pregunta puntual: número + "
    "una línea de contexto (ej 'En mayo gastaste 230 EUR en Supermercado, "
    "12,3% más que el mes pasado.'). Para varios números: si son hasta "
    "3-4 items, encadenalos en una frase con comas. Si son más, "
    "enumerá uno por línea en formato 'Nombre: valor EUR' (sin viñetas "
    "ni guiones, solo texto con salto de línea), precedido de una "
    "frase corta de contexto.\n\n"
    "Reglas inviolables:\n"
    "- NO inventes números. Si una tool te devuelve vacío, decí 'no hay datos "
    "para ese período/filtro' en vez de improvisar.\n"
    "- NO registrás, editás ni borrás transacciones — solo consultás. "
    "Si te piden una acción de escritura, decí simplemente que esa "
    "operación no entra en lo que estás haciendo ahora y dejá que la "
    "persona reformule.\n"
    "- IMPORTANTE: NO menciones a otros bots, agentes ni subsistemas por "
    "nombre ('el Registrador', 'el otro bot', 'el Director', etc.). Para "
    "la persona el sistema es uno solo; la división interna es detalle "
    "de implementación que no debe filtrarse. Si necesitás indicar que "
    "no hacés algo, usá frases neutrales tipo 'esa operación no la "
    "manejo desde acá; si lo querés, pedímelo y se procesa'.\n"
    "- NO ofrezcas proactivamente recomendaciones de acción ('podés "
    "editar...', 'podés agregar...') salvo que la persona lo pida. "
    "Limitate a responder lo consultado.\n"
    "- NO inventes categorías ni cuentas ni miembros nuevos. Usá sólo los "
    "que están en el contexto.\n"
    "- Montos en EUR (o la moneda real de la cuenta si es distinta) y "
    "porcentajes con 1 decimal.\n\n"
    "Formato de respuesta: español. El mensaje se envía con parse_mode "
    "Markdown LEGACY de Telegram. Reglas estrictas:\n"
    "- Resaltar SOLO los números clave de la respuesta (el total, el "
    "monto consultado, la diferencia porcentual) con *un solo asterisco*. "
    "NO uses ** (doble — no se renderiza). NO negrites cada item de una "
    "lista; solo los 1-3 números principales.\n"
    "- NUNCA uses _ ni ` salvo intencionalmente.\n"
    "- Para enumerar muchos items: salto de línea con formato 'Nombre: "
    "valor EUR' por línea, SIN viñetas (sin -, sin •, sin *).\n"
    "- Sin emojis, sin tablas, sin encabezados markdown.\n"
    "- Tono profesional y directo: nada de saludos, sin muletillas tipo "
    "'Hola', 'Ok', 'Perfecto'. Frases completas con sujeto y verbo.\n"
    "- Montos enteros sin decimales innecesarios (530 EUR, no 530.0 EUR).\n"
    "Ejemplo bueno: 'En mayo gastaste *3415 EUR*. El detalle por categoría:\n"
    "Sin categoría: 772 EUR\nAlquiler: 530 EUR'.\n\n"
    "{household_context}\n"
    "{sender_line}"
)


# Prompt for the Director (orchestrator) bot — chooses which subagent
# should handle each message and routes via two local tools. We name the
# tools verbatim here because the agent picks BY name; capabilities are
# described in one line each so the model can match user intent quickly.
# The system prompt instructs no paraphrase — the code also enforces a
# bypass (see `_extract_subagent_reply`), so the user sees the subagent's
# answer exactly.
DIRECTOR_SYSTEM_TMPL = (
    "Sos el director técnico de un equipo de dos subagentes que atienden a "
    "la familia. Hoy es {today}. Tu único trabajo: leer cada mensaje y "
    "decidir CUÁL de los dos lo resuelve, llamando la tool correspondiente. "
    "No respondas vos sobre transacciones — siempre delegá.\n\n"
    "Tus dos subordinados (tools):\n"
    "• route_to_registrar(text) — registra/edita/borra/restaura gastos e "
    "ingresos, paga recurrentes, crea categorías, presupuestos, miembros y "
    "cuentas. CUALQUIER acción que escriba en la base de datos va acá.\n"
    "• route_to_consultant(text) — responde consultas analíticas: cuánto se "
    "gastó/cobró, balances, top categorías, tendencias, comparaciones de "
    "períodos, estado de presupuestos del mes, recurrentes pendientes, "
    "saldos. Solo lectura.\n\n"
    "Cómo decidís:\n"
    "1. Verbos de acción (gasté, pagué, compré, registrá, cambiá, cobré, "
    "creá, borrá, eliminá, recuperá) → Registrador.\n"
    "2. Verbos de consulta (cuánto, dame, mostrame, comparame, balance, "
    "tendencia, top) → Consultor.\n"
    "3. Confirmaciones o follow-ups ('sí', 'dale', 'guardalo igual') → el "
    "mismo subagente que respondió antes en esta conversación.\n"
    "4. Pasale el texto ORIGINAL del usuario tal cual — no lo resumas ni lo "
    "reescribas.\n\n"
    "REGLA INVIOLABLE: invocás EXACTAMENTE UNA tool por mensaje del usuario. "
    "Una y solo una. Si el subagente devuelve una pregunta (¿confirmás...?, "
    "¿cuál de estos...?, falta el dato X), tu turno TERMINA AHÍ: la "
    "respuesta del subagente va al usuario tal cual y vos esperás el "
    "SIGUIENTE mensaje real del usuario para volver a actuar. NUNCA "
    "respondas vos al subagente. NUNCA inventes un 'sí' / 'dale' / "
    "'confirmo' en nombre del usuario. NUNCA llames la misma tool dos veces "
    "en el mismo turno. Si te tienta hacerlo, NO LO HAGAS — el usuario "
    "necesita ver y responder esa pregunta él mismo.\n\n"
    "Si la tool devuelve ok=False, decí brevemente al usuario que hubo un "
    "problema y proponé reintentar. Si dudás entre Registrador y Consultor, "
    "eligí el Consultor (es read-only y no rompe nada).\n\n"
    "{sender_line}"
)


async def run_consultant_agent(
    message: str,
    session_id: str | None = None,
    sender_name: str | None = None,
) -> str:
    """Run one user question through the Consultor (analytics) loop.

    Twin of `run_agent` but with a read-only prompt: the agent picks
    analytics tools (spending_by_period, balance_for_period, etc.) instead
    of write tools. Shares all the helpers (`_user_turn`, `_model_turn`,
    `_log_run`, model rotation, MCP connection, `_SESSIONS`).

    `sender_name` becomes the default member context (same idea as the
    Registrador). `session_id` is keyed by the router as
    'consultant:telegram:{chat_id}' so `agent_run` rows can be filtered
    per agent without a schema change.
    """
    ctx = await build_household_context()
    today = date.today()
    sender_line = SENDER_LINE_TMPL.format(name=sender_name) if sender_name else ""
    system = CONSULTANT_SYSTEM_TMPL.format(
        today=today.isoformat(),
        year=today.year,
        household_context=format_context_for_prompt(ctx),
        sender_line=sender_line,
    )

    history = _SESSIONS.get(session_id, []) if session_id else []
    cur_turn = _user_turn(message)
    contents = history + [cur_turn]

    cfg = types.GenerateContentConfig(system_instruction=system, temperature=0.1)

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = None
    model_used: str | None = None
    last_err: BaseException | None = None
    try:
        async with streamablehttp_client(MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                cfg.tools = [session]
                for model in MODELS:
                    try:
                        resp = await client.aio.models.generate_content(
                            model=model, contents=contents, config=cfg
                        )
                        model_used = model
                        break
                    except BaseException as exc:  # noqa: BLE001 - inspected below
                        if _is_retriable_error(exc):
                            log.warning("model %s unavailable (quota or 5xx), rotating", model)
                            last_err = exc
                            continue
                        raise
        if resp is None:
            raise last_err or RuntimeError("no model produced a response")
        reply = (resp.text or "").strip() or "(sin respuesta)"
    except BaseException as exc:  # noqa: BLE001 - log the failed run, then re-raise
        asyncio.create_task(
            _log_run(session_id, message, None, model_used, [], str(exc))
        )
        raise

    prompt_tok, output_tok, total_tok = _extract_usage(resp)
    asyncio.create_task(
        _log_run(session_id, message, reply, model_used, _extract_tool_calls(resp), None,
                 prompt_tok, output_tok, total_tok)
    )
    if session_id:
        _SESSIONS[session_id] = (
            history + [_user_turn(message), _model_turn(reply)]
        )[-_MAX_TURNS:]
    return reply


async def run_director_agent(
    message: str,
    session_id: str | None = None,
    sender_name: str | None = None,
    *,
    registrar_session_id: str | None = None,
    consultant_session_id: str | None = None,
) -> str:
    """Director (orchestrator): chooses Registrador or Consultor and forwards.

    Two local Python tools (defined as closures below) wrap `run_agent` and
    `run_consultant_agent`. The Gemini SDK introspects their docstrings and
    type hints to build the function-call schema. These tools live in the
    api process (NOT in the MCP server), which is why we don't need an MCP
    session here — avoids the import cycle and saves a round-trip.

    Memory: the Director keeps its own history under `session_id` (e.g.
    'director:telegram:{chat_id}'). Subagent histories live under their own
    keys (passed as `registrar_session_id` / `consultant_session_id`),
    matching what the standalone bots use — so the user can mix bots and
    keep continuity.

    parent_run_id propagation: we pre-assign `orch_id`, set the ContextVar
    before invoking Gemini, and `_log_run` inside the subagents reads it
    automatically when persisting their rows. The Director's own row uses
    parent_run_id=None.

    Reply bypass: the closure variable `last_subagent_reply` is set the
    moment a routing tool fires. After Gemini returns, we prefer that over
    `resp.text` — the LLM never gets to paraphrase the subagent. This is
    more robust than reading the SDK's `automatic_function_calling_history`,
    which can come back empty when a model rotation truncates the final
    response (we hit this in production: 503 mid-loop, the second model
    re-ran the tool but produced no final text, and the user saw "(sin
    respuesta)" even though the subagent had answered correctly).
    """
    today = date.today()
    sender_line = SENDER_LINE_TMPL.format(name=sender_name) if sender_name else ""
    system = DIRECTOR_SYSTEM_TMPL.format(
        today=today.isoformat(),
        sender_line=sender_line,
    )

    orch_id = uuid.uuid4()
    parent_token = current_parent_run_id.set(orch_id)

    # Captured inside the routing tools; read after generate_content returns.
    # Last write wins — if model rotation re-runs the tool, we keep the
    # newer reply.
    last_subagent_reply: str | None = None

    async def route_to_registrar(text: str) -> dict:
        """Delega al Registrador: registra/edita gastos, ingresos, recurrentes,
        categorías, presupuestos, miembros o cuentas. Usar para CUALQUIER
        acción que MODIFIQUE el estado de la familia.

        Args:
            text: el mensaje original del usuario, tal cual lo recibiste.
        """
        nonlocal last_subagent_reply
        try:
            reply = await run_agent(
                message=text,
                session_id=registrar_session_id,
                sender_name=sender_name,
            )
            last_subagent_reply = reply
            return {"ok": True, "reply": reply}
        except Exception as exc:  # noqa: BLE001
            log.exception("route_to_registrar failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def route_to_consultant(text: str) -> dict:
        """Delega al Consultor: responde consultas analíticas (read-only) —
        montos, balances, top categorías, tendencias, comparaciones,
        presupuestos del mes, recurrentes pendientes, saldos.

        Args:
            text: el mensaje original del usuario, tal cual lo recibiste.
        """
        nonlocal last_subagent_reply
        try:
            reply = await run_consultant_agent(
                message=text,
                session_id=consultant_session_id,
                sender_name=sender_name,
            )
            last_subagent_reply = reply
            return {"ok": True, "reply": reply}
        except Exception as exc:  # noqa: BLE001
            log.exception("route_to_consultant failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    history = _SESSIONS.get(session_id, []) if session_id else []
    cur_turn = _user_turn(message)
    contents = history + [cur_turn]

    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0.1,
        tools=[route_to_registrar, route_to_consultant],
    )

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = None
    model_used: str | None = None
    last_err: BaseException | None = None
    try:
        for model in MODELS:
            try:
                resp = await client.aio.models.generate_content(
                    model=model, contents=contents, config=cfg
                )
                model_used = model
                break
            except BaseException as exc:  # noqa: BLE001
                if _is_retriable_error(exc):
                    log.warning("director model %s unavailable, rotating", model)
                    last_err = exc
                    continue
                raise
        if resp is None:
            raise last_err or RuntimeError("no model produced a response")
        # Bypass: prefer the subagent's reply (captured in the routing tool
        # closure) over `resp.text`. Works even when model rotation leaves
        # the final text empty, as long as the tool fired at least once.
        reply = last_subagent_reply or (resp.text or "").strip() or "(sin respuesta)"
    except BaseException as exc:  # noqa: BLE001
        asyncio.create_task(
            _log_run(session_id, message, None, model_used, [], str(exc),
                     agent_run_id=orch_id, parent_run_id=None)
        )
        current_parent_run_id.reset(parent_token)
        raise

    prompt_tok, output_tok, total_tok = _extract_usage(resp)
    asyncio.create_task(
        _log_run(session_id, message, reply, model_used,
                 _extract_tool_calls(resp), None,
                 prompt_tok, output_tok, total_tok,
                 agent_run_id=orch_id, parent_run_id=None)
    )
    if session_id:
        _SESSIONS[session_id] = (
            history + [_user_turn(message), _model_turn(reply)]
        )[-_MAX_TURNS:]
    current_parent_run_id.reset(parent_token)
    return reply


def _format_recent(rows: list[dict]) -> str:
    if not rows:
        return "(no hay gastos recientes)"
    return "\n".join(
        f"- {r['date']} | {r['amount']:.2f} {r['currency']} | {r['category']} | "
        f"{r['description'] or '(sin descripción)'}"
        for r in rows
    )


def _user_turn(text: str, media: bytes | None = None, media_mime: str | None = None):
    """A user turn, optionally carrying a receipt photo / voice note inline.

    For media we let the model read it directly (it extracts the expense from
    the image/audio) instead of the old single-shot parser. A short text part
    nudges it toward the registrar task when there's no caption.
    """
    parts = []
    if media is not None:
        parts.append(types.Part.from_bytes(data=media, mime_type=media_mime))
        parts.append(types.Part(text=text or "Registrá el gasto de este comprobante."))
    else:
        parts.append(types.Part(text=text))
    return types.Content(role="user", parts=parts)


def _model_turn(text: str):
    return types.Content(role="model", parts=[types.Part(text=text)])


def _extract_tool_calls(resp) -> list[dict]:
    """Pull the tools the agent fired out of the SDK's auto function-calling
    history. Returns [{"name", "args"}, …]; [] when it answered directly.

    The SDK runs tools for us and records every call here, so we just read it
    once after the run — no live interception. Defensive: any shape we don't
    recognise yields [] rather than breaking the (background) logging path.
    """
    calls: list[dict] = []
    try:
        for content in getattr(resp, "automatic_function_calling_history", None) or []:
            for part in getattr(content, "parts", None) or []:
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    calls.append({"name": fc.name, "args": dict(fc.args or {})})
    except Exception:  # noqa: BLE001 - logging must never break the request
        log.warning("could not extract tool calls from response", exc_info=True)
    return calls


def _extract_usage(resp) -> tuple[int | None, int | None, int | None]:
    """(prompt, output, total) token counts from the response's usage_metadata,
    or (None, None, None) if it's missing — same defensive stance as the tool
    extraction, so logging never breaks on an unexpected shape."""
    um = getattr(resp, "usage_metadata", None)
    if um is None:
        return None, None, None
    return (
        getattr(um, "prompt_token_count", None),
        getattr(um, "candidates_token_count", None),
        getattr(um, "total_token_count", None),
    )


_UNSET: object = object()


async def _log_run(
    session_id: str | None,
    input_text: str,
    reply_text: str | None,
    model_used: str | None,
    tool_calls: list[dict],
    error: str | None,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    agent_run_id: uuid.UUID | None = None,
    parent_run_id: object = _UNSET,
) -> None:
    """Persist one agent_run row. Fire-and-forget: scheduled with
    asyncio.create_task after the reply is sent, so it never adds latency.
    Swallows its own errors — a failed log must not surface to the user.

    `agent_run_id`: pre-assigned UUID. Passed by the Director so its row id
    is known *before* the DB write (the subagent tools read it via the
    ContextVar to set their parent_run_id). When None, the DB generates one.

    `parent_run_id`: if omitted entirely, falls back to the ContextVar set
    by the Director — so a registrador/consultor run invoked via the
    Director picks up the link automatically. Pass `None` explicitly to
    force NULL (used by the Director's own row).
    """
    if parent_run_id is _UNSET:
        parent_run_id = current_parent_run_id.get()
    try:
        async with AsyncSessionLocal() as session:
            kwargs = dict(
                session_id=session_id,
                input_text=input_text,
                reply_text=reply_text,
                model_used=model_used,
                tool_calls=tool_calls,
                error=error,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                parent_run_id=parent_run_id,
            )
            if agent_run_id is not None:
                kwargs["agent_run_id"] = agent_run_id
            session.add(AgentRun(**kwargs))
            await session.commit()
    except Exception:  # noqa: BLE001 - background logging is best-effort
        log.warning("agent_run logging failed", exc_info=True)


async def run_agent(
    message: str,
    session_id: str | None = None,
    media: bytes | None = None,
    media_mime: str | None = None,
    sender_name: str | None = None,
) -> str:
    """Run one user message through the registrar agent loop.

    `message` is the text (a caption when media is present). `media`/`media_mime`
    carry a receipt photo or voice note the model reads directly.
    `sender_name` is the authenticated family member name (e.g. resolved from a
    Telegram user id); injected into the system prompt so the agent attributes
    the expense to them by default.

    If session_id is given, prior text turns for that session are replayed so
    follow-ups ("sí, guardalo igual") are understood. Returns the reply text.
    """
    ctx = await build_household_context()
    categories = ctx.get("categories", [])
    recent = await recent_expenses(limit=20)
    today = date.today()
    sender_line = SENDER_LINE_TMPL.format(name=sender_name) if sender_name else ""
    system = SYSTEM_TMPL.format(
        categories=", ".join(categories) or "(ninguna)",
        today=today.isoformat(),
        year=today.year,
        recent=_format_recent(recent),
        household_context=format_context_for_prompt(ctx),
        sender_line=sender_line,
    )

    history = _SESSIONS.get(session_id, []) if session_id else []
    cur_turn = _user_turn(message, media=media, media_mime=media_mime)
    contents = history + [cur_turn]

    cfg = types.GenerateContentConfig(system_instruction=system, temperature=0.1)

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = None
    model_used: str | None = None
    last_err: BaseException | None = None
    try:
        async with streamablehttp_client(MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                cfg.tools = [session]
                for model in MODELS:
                    try:
                        resp = await client.aio.models.generate_content(
                            model=model, contents=contents, config=cfg
                        )
                        model_used = model
                        break
                    except BaseException as exc:  # noqa: BLE001 - inspected below
                        if _is_retriable_error(exc):
                            log.warning("model %s unavailable (quota or 5xx), rotating", model)
                            last_err = exc
                            continue
                        raise
        if resp is None:
            raise last_err or RuntimeError("no model produced a response")
        reply = (resp.text or "").strip() or "(sin respuesta)"
    except BaseException as exc:  # noqa: BLE001 - log the failed run, then re-raise
        asyncio.create_task(
            _log_run(session_id, message, None, model_used, [], str(exc))
        )
        raise

    # Log the run in the background so persistence never delays the reply.
    prompt_tok, output_tok, total_tok = _extract_usage(resp)
    asyncio.create_task(
        _log_run(session_id, message, reply, model_used, _extract_tool_calls(resp), None,
                 prompt_tok, output_tok, total_tok)
    )
    if session_id:
        # Store a text-only version of the turn (a caption, or a placeholder for
        # bare media) so we don't re-send image/audio bytes on every follow-up.
        stored_turn = _user_turn(message or "(envió un comprobante)")
        _SESSIONS[session_id] = (history + [stored_turn, _model_turn(reply)])[-_MAX_TURNS:]
    return reply
