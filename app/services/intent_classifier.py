"""
Clasificador rápido de intent para mensajes Telegram sin comando explícito.

Objetivo: distinguir entre "es un movimiento financiero" vs "es un item de compras"
(en futuro: vs evento, vs tarea). Usado en el modo híbrido del bot — el usuario
puede tipear comandos (/compras add X) o lenguaje natural y el clasificador
decide qué hacer.

Estrategia en dos pasos para minimizar latencia:
1. Heurística rápida en Python (regex): si hay un monto explícito (€/euros/EUR),
   asumimos transaction. Si empieza con verbo de "anotar"/"comprar" sin monto,
   asumimos shopping. Si dice "tengo que" sin monto → tarea (futuro).
2. Solo si la heurística no decide con confianza, llamamos al LLM. Esto evita
   un viaje al modelo en >80% de los casos.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


# Patrones rápidos
_RE_MONTO        = re.compile(r"\d+(?:[.,]\d+)?\s*(?:€|eur|euros?|usd?\$)", re.IGNORECASE)
_RE_VERBO_GASTO  = re.compile(r"\b(?:gast[éeo]|pagu[éeo]|cobr[éeo]|cobr[óo]|recib[íi])\b", re.IGNORECASE)
_RE_VERBO_COMPRA = re.compile(r"\b(?:anota[mr]?e?|necesito|comprar|agreg[áa]me?|agregar|sum[áa]me?|agreg[áa] a la lista|para la lista)\b", re.IGNORECASE)
# Marcadores temporales típicos de eventos
_RE_DIA_FUTURO   = re.compile(r"\b(?:hoy|mañana|pasado|lunes|martes|mi[eé]rcoles|jueves|viernes|s[áa]bado|domingo|el d[íi]a \d{1,2}|el \d{1,2})\b", re.IGNORECASE)
_RE_HORA         = re.compile(r"\b\d{1,2}\s*(?::\d{2}\s*)?(?:hs|hrs|am|pm|de la (?:mañana|tarde|noche))\b|\bA?\s*las\s+\d{1,2}", re.IGNORECASE)
_RE_PALABRA_EVENTO = re.compile(r"\b(?:turno|reuni[óo]n|cita|cumple|evento|consulta|análisis|control|tr[áa]mite|ir al?|tengo)\b", re.IGNORECASE)


def classify_quick(text: str) -> str | None:
    """Heurística sin LLM. Devuelve 'transaction', 'shopping', 'event' o None."""
    t = text.strip().lower()
    if not t:
        return None
    has_amount  = bool(_RE_MONTO.search(t))
    has_compra  = bool(_RE_VERBO_COMPRA.search(t))
    has_gasto   = bool(_RE_VERBO_GASTO.search(t))
    has_dia     = bool(_RE_DIA_FUTURO.search(t))
    has_hora    = bool(_RE_HORA.search(t))
    has_evento  = bool(_RE_PALABRA_EVENTO.search(t))

    # Gasto explícito gana
    if has_amount and has_gasto:
        return "transaction"
    # Evento: marcador temporal + (palabra de evento o hora)
    if has_dia and (has_evento or has_hora) and not has_amount:
        return "event"
    if has_compra and not has_amount:
        return "shopping"
    if has_amount and not has_compra:
        return "transaction"
    return None


_CLASSIFY_PROMPT = """\
Clasifica el siguiente mensaje en ESPAÑOL en una sola categoría:
- "transaction": el usuario reporta un gasto o ingreso (con o sin monto explícito).
- "shopping":    el usuario quiere agregar algo a la lista de compras (super/almacén).
- "unknown":     no encaja en ninguna.

Devuelve EXCLUSIVAMENTE un JSON: {{"intent": "transaction"|"shopping"|"unknown", "confidence": 0.0-1.0}}.

Ejemplos:
- "gasté 25€ en farmacia"            → {{"intent":"transaction","confidence":0.99}}
- "anotame leche y pan"              → {{"intent":"shopping","confidence":0.98}}
- "necesito 2kg de tomates"          → {{"intent":"shopping","confidence":0.95}}
- "compré 200€ en el super"          → {{"intent":"transaction","confidence":0.99}}
- "comprar yogur"                    → {{"intent":"shopping","confidence":0.97}}
- "cobré el sueldo"                  → {{"intent":"transaction","confidence":0.95}}

Mensaje: "{text}"
JSON:
"""


def classify_llm(text: str) -> dict:
    """Pregunta al LLM. Devuelve {intent, confidence}."""
    prompt = _CLASSIFY_PROMPT.format(text=text)
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{settings.ollama_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                    "keep_alive": "5m",
                    "options": {"num_ctx": 1024, "num_predict": 64, "temperature": 0.0},
                },
            )
            resp.raise_for_status()
        data = json.loads(resp.json()["response"])
        return {
            "intent":     data.get("intent", "unknown"),
            "confidence": float(data.get("confidence", 0.5)),
        }
    except Exception as exc:
        logger.warning("classify_llm falló: %s", exc)
        return {"intent": "unknown", "confidence": 0.0}


def classify(text: str) -> dict:
    """Devuelve {intent, confidence, source}. Source: 'heuristic' | 'llm'."""
    quick = classify_quick(text)
    if quick:
        return {"intent": quick, "confidence": 0.95, "source": "heuristic"}
    res = classify_llm(text)
    res["source"] = "llm"
    return res
