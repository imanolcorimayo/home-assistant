"""Expense-registrar agent — Gemini driven by the household MCP server.

Single, narrow goal: register ONE expense from a message, checking for a
likely duplicate first. No summaries, no analytics — that's a different
agent. The google-genai SDK runs the tool-calling loop automatically
(look_up_expenses -> decide -> add_expense / flag).
"""

import logging
import os

from google import genai
from google.genai import types
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.services.transactions import active_category_names

log = logging.getLogger("agent")

MCP_URL = os.environ.get("MCP_URL", "http://household_mcp:8000/mcp")
MODEL = os.environ.get("AGENT_MODEL", "gemini-2.5-flash")

SYSTEM_TMPL = (
    "Sos el registrador de gastos de una familia. Tu ÚNICO trabajo es registrar UN gasto "
    "a partir del mensaje. No hacés resúmenes ni análisis ni respondés otras preguntas.\n\n"
    "Pasos:\n"
    "1. Extraé el gasto: monto (EUR) y una descripción corta. Si se menciona una fecha usala "
    "(YYYY-MM-DD); si no, es hoy.\n"
    "2. ANTES de guardar, llamá a look_up_expenses con una palabra clave del gasto para ver "
    "gastos parecidos recientes.\n"
    "3. Si encontrás uno que parece EL MISMO gasto (monto parecido + descripción parecida + "
    "fecha cercana), NO lo guardes: avisá al usuario que ya hay uno parecido (decí su fecha y "
    "monto) y preguntá si lo registra igual.\n"
    "   - Excepción: si el usuario indica explícitamente que es a propósito ('igual', 'de nuevo', "
    "'otra vez', 'sí guardalo', 'es otro'), entonces SÍ guardalo con add_expense.\n"
    "4. Si no hay duplicado, elegí la categoría más adecuada y guardá con add_expense. Para elegir "
    "la categoría, mirá con qué categoría se cargaron los gastos parecidos en look_up_expenses.\n"
    "5. Confirmá en una línea lo que hiciste (monto + categoría guardada, o el duplicado que "
    "marcaste).\n\n"
    "Categorías válidas: {categories}.\n"
    "Si ninguna aplica claramente, no mandes categoría (quedará 'Sin categoría').\n"
    "Respondé siempre en español, breve."
)


async def run_agent(message: str) -> str:
    """Run one user message through the expense-registrar loop. Returns the reply text."""
    categories = await active_category_names()
    system = SYSTEM_TMPL.format(categories=", ".join(categories) or "(ninguna)")

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resp = await client.aio.models.generate_content(
                model=MODEL,
                contents=message,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.1,
                    tools=[session],
                ),
            )
            return (resp.text or "").strip() or "(sin respuesta)"
