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
    "- Pagos recurrentes (alquiler, Netflix, etc.): ver el estado del mes con list_recurring (te "
    "dice si cada uno ya está pagado este mes), crear o editar la definición con add_recurring_charge "
    "/ update_recurring_charge_tool, y registrar el pago del mes con pay_recurring. Ojo: un "
    "recurrente queda 'pagado' del mes recién cuando le registrás el pago — no hay otro flag.\n"
    "- Estructura del hogar: podés crear una categoría nueva (create_category), fijar/actualizar "
    "el presupuesto mensual de una categoría (create_budget), o —rara vez— crear un miembro "
    "(create_member) o una cuenta (create_account, ej 'Fer Efectivo'). Son cambios estructurales: "
    "confirmá con la persona ANTES de crearlos. Categorías y presupuestos son de rutina; miembros y "
    "cuentas casi nunca cambian, así que nunca los inventes — creálos sólo si te lo piden explícito.\n"
    "No borrás gastos ni hacés resúmenes o análisis. Si te piden algo que tus herramientas no "
    "permiten, decilo con franqueza.\n\n"
    "Cómo te manejás: usá tu criterio. Interpretá el mensaje (puede ser texto, la foto de un ticket "
    "o una nota de voz), entendé qué quiere la persona y resolvelo de la forma más razonable.\n\n"
    "- Cuando está claro y es de bajo riesgo, actuá vos y contá en una línea qué hiciste. Para "
    "registrar un gasto deducí el monto (EUR) y una descripción corta; la fecha es hoy salvo que se "
    "mencione otra (si no trae año, asumí {year}); elegí la categoría mirando con qué categoría se "
    "cargaron gastos parecidos en la lista.\n"
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
    "Respondé siempre en español, breve."
)

# Used by run_agent when sender_name is provided (authenticated channel like
# Telegram). Tells the agent whose mouth the message is coming from so it can
# auto-fill family_member_hint without needing the person to name themselves.
SENDER_LINE_TMPL = (
    "Quien manda este mensaje es {name}. Salvo que se mencione explícitamente "
    "otro miembro, registrá los gastos a nombre de {name} (pasá '{name}' como "
    "family_member_hint en add_expense / add_income)."
)


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
) -> None:
    """Persist one agent_run row. Fire-and-forget: scheduled with
    asyncio.create_task after the reply is sent, so it never adds latency.
    Swallows its own errors — a failed log must not surface to the user."""
    try:
        async with AsyncSessionLocal() as session:
            session.add(
                AgentRun(
                    session_id=session_id,
                    input_text=input_text,
                    reply_text=reply_text,
                    model_used=model_used,
                    tool_calls=tool_calls,
                    error=error,
                    prompt_tokens=prompt_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                )
            )
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
