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
from app.services.transactions import active_category_names

log = logging.getLogger("transaction_parser")

PROMPT = """Sos un asistente que extrae UNA transacción financiera doméstica del mensaje recibido.

El mensaje puede ser texto, una nota de voz, o una foto de un ticket/comprobante.

Devolvé JSON con estos campos:
- "kind": "expense" (gasto) o "income" (ingreso)
- "amount": número, monto total (sin símbolo, sin separadores de miles, punto decimal opcional)
- "description": descripción corta y útil (ej: "Supermercado Carrefour", "Sueldo enero", "Nafta YPF")
- "category": clasificá el gasto/ingreso (ver lista más abajo)
- "transaction_date": fecha en formato YYYY-MM-DD si se menciona o aparece en el ticket; null si no
- "account_hint": texto libre identificando la cuenta/medio si se menciona (ej "efectivo", "BBVA", "Visa", "transferencia"); null si no
- "confidence": "high", "medium", "low" — qué tan seguro estás de la extracción

Reglas:
- "income" si dice "cobré", "me pagaron", "ingresó", "depósito recibido", "sueldo", "transferencia recibida"
- "expense" por defecto (compras, servicios, comida, transporte, etc.)
- Si el mensaje NO parece una transacción (saludo, pregunta, basura), devolvé "kind": null y "confidence": "low"
- En tickets/fotos: usá el TOTAL del ticket, no items individuales"""

# Base schema; the "category" enum is filled in per-call from the DB.
BASE_PROPERTIES = {
    "kind": {"type": "string", "enum": ["expense", "income"], "nullable": True},
    "amount": {"type": "number", "nullable": True},
    "description": {"type": "string", "nullable": True},
    "transaction_date": {"type": "string", "nullable": True},
    "account_hint": {"type": "string", "nullable": True},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
}


def _build_schema(categories: list[str]) -> dict:
    props = dict(BASE_PROPERTIES)
    cat = {"type": "string", "nullable": True}
    if categories:
        cat["enum"] = categories  # constrain Gemini to real, resolvable names
    props["category"] = cat
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
    categories: list[str] | None = None,
) -> dict | None:
    """Parse one message into a transaction.

    Exactly one of (text, media_bytes) should be provided. If media_bytes is given,
    media_mime is required (e.g. 'image/jpeg', 'audio/ogg').
    If both text and media are given, text is treated as a caption.
    `categories` constrains the category field; fetched from the DB if omitted.
    """
    if categories is None:
        categories = await active_category_names()

    parts: list[dict] = [{"text": PROMPT}]
    if categories:
        parts.append({"text": "\n\nLista de categorías válidas (elegí UNA exacta, o null si "
                              "ninguna aplica claramente):\n- " + "\n- ".join(categories)})

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
        response_schema=_build_schema(categories),
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
