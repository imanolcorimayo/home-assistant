"""Extract a household transaction from text, audio, or image input.

V1 scope:
- One message in → at most one transaction out.
- No accounts/budgets/recurring resolution yet — Gemini returns hints
  (free-text 'account_hint'), we resolve to IDs later in DB layer.
- Spanish, AR-flavored vocabulary; family will write/speak that way.
"""

import base64
import logging

from app.services import gemini
from app.services.transactions import build_household_context, format_context_for_prompt

log = logging.getLogger("transaction_parser")

PROMPT = """Sos un asistente que extrae UNA transacción financiera doméstica del mensaje recibido.

El mensaje puede ser texto, una nota de voz, o una foto de un ticket/comprobante.

Devolvé JSON con estos campos:
- "kind": "expense" (gasto) o "income" (ingreso)
- "amount": número, monto total (sin símbolo, sin separadores de miles, punto decimal opcional)
- "description": descripción corta y útil (ej: "Supermercado Carrefour", "Sueldo enero", "Nafta YPF")
- "category": clasificá el gasto/ingreso (ver lista más abajo)
- "transaction_date": fecha en formato YYYY-MM-DD si se menciona o aparece en el ticket; null si no
- "account_hint": nombre exacto de UNA cuenta de la lista de abajo si se menciona; null si no se menciona o no matchea
- "family_member_hint": nombre exacto de UN miembro de la familia (ver lista) si se menciona; null si no
- "confidence": "high", "medium", "low" — qué tan seguro estás de la extracción

Reglas:
- "income" si dice "cobré", "me pagaron", "ingresó", "depósito recibido", "sueldo", "transferencia recibida"
- "expense" por defecto (compras, servicios, comida, transporte, etc.)
- Si el mensaje NO parece una transacción (saludo, pregunta, basura), devolvé "kind": null y "confidence": "low"
- En tickets/fotos: usá el TOTAL del ticket, no items individuales
- Si el gasto coincide con un GASTO RECURRENTE conocido (mismo nombre + monto + día cercano), bajá la confidence a "low" — probablemente ya quedó registrado automáticamente"""

# Base schema; the "category" / "account_hint" / "family_member_hint" enums
# are filled in per-call from the DB so Gemini only emits real, resolvable values.
BASE_PROPERTIES = {
    "kind": {"type": "string", "enum": ["expense", "income"], "nullable": True},
    "amount": {"type": "number", "nullable": True},
    "description": {"type": "string", "nullable": True},
    "transaction_date": {"type": "string", "nullable": True},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
}


def _build_schema(
    categories: list[str],
    accounts: list[str] | None = None,
    members: list[str] | None = None,
) -> dict:
    props = dict(BASE_PROPERTIES)
    cat = {"type": "string", "nullable": True}
    if categories:
        cat["enum"] = categories  # constrain Gemini to real, resolvable names
    props["category"] = cat

    acc = {"type": "string", "nullable": True}
    if accounts:
        acc["enum"] = accounts
    props["account_hint"] = acc

    mem = {"type": "string", "nullable": True}
    if members:
        mem["enum"] = members
    props["family_member_hint"] = mem

    return {
        "type": "object",
        "properties": props,
        # `required` means the model must EMIT the key — `nullable: True`
        # still lets it set the value to null. We force amount + description
        # so Gemini never silently drops them (observed with this model).
        "required": ["kind", "amount", "description", "confidence"],
    }


async def parse_transaction(
    text: str | None = None,
    media_bytes: bytes | None = None,
    media_mime: str | None = None,
    household_context: dict | None = None,
) -> dict | None:
    """Parse one message into a transaction.

    Exactly one of (text, media_bytes) should be provided. If media_bytes is given,
    media_mime is required (e.g. 'image/jpeg', 'audio/ogg').
    If both text and media are given, text is treated as a caption.
    `household_context` constrains categories/accounts/members to real values
    and is injected as text in the prompt so Gemini can match recurring charges
    and route to the right account/member. Fetched from DB if omitted.
    """
    if household_context is None:
        household_context = await build_household_context()
    categories = household_context.get("categories", [])
    accounts = [a["name"] for a in household_context.get("accounts", [])]
    members = [m["name"] for m in household_context.get("members", [])]

    parts: list[dict] = [{"text": PROMPT}]
    if categories:
        parts.append({"text": "\n\nLista de categorías válidas (elegí UNA exacta, o null si "
                              "ninguna aplica claramente):\n- " + "\n- ".join(categories)})
    parts.append({"text": "\n\n" + format_context_for_prompt(household_context)})

    if text:
        parts.append({"text": f"\n\nMensaje del usuario:\n{text}"})

    if media_bytes is not None:
        if not media_mime:
            raise ValueError("media_mime required when media_bytes is provided")
        parts.append({
            "inlineData": {
                "mimeType": media_mime,
                "data": base64.b64encode(media_bytes).decode("ascii"),
            }
        })

    result = await gemini.generate(
        parts=parts,
        response_schema=_build_schema(categories, accounts, members),
        temperature=0.2,
        max_output_tokens=500,
    )

    if result is None:
        return None
    if not isinstance(result, dict):
        log.warning("unexpected gemini result type: %s", type(result))
        return None

    log.info("parsed: %s", result)
    return result
