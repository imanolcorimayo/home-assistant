"""
Wrapper síncrono para Ollama — solo se llama desde Celery workers.
"""
import json
import logging
from datetime import date

import httpx
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.finance import LLMTransactionOutput

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """\
Eres un asistente contable de una familia. Analiza el texto y extrae TODOS \
los movimientos de dinero mencionados. Devuelve EXCLUSIVAMENTE un objeto JSON \
válido — sin texto adicional, sin markdown:

{{
  "transactions": [
    {{
      "amount": <número positivo>,
      "currency": "EUR",
      "categoria": "<Entradas | Gastos Fijos | Gastos variables>",
      "subcategoria1": "<ver mapa>",
      "subcategoria2": "<ver mapa, o null>",
      "subcategoria3": "<solo para Transporte: Combustible|Nafta|Mantenimiento|Aseguracion|Impuesto|Revision tecnica|Reparacion|Lavado, o null>",
      "nota": "<descripción corta en español, máximo 10 palabras>",
      "transaction_date": "{today}",
      "confidence": <0.0 a 1.0>
    }}
  ]
}}

MAPA DE JERARQUÍA:

categoria "Entradas":
  sub1 "Hector"   → sub2: Knapp | Domingos Knapp | Hs extras | 13ma | 14ma | Premio
  sub1 "Luisiana" → sub2: Constan | Charly | Roberta | Peluqueria | En Mano | Casa
  sub1 "Buroc"    → sub2: Assegno | Tramite 730 | Dedicata a Te

categoria "Gastos Fijos":
  sub1 "Alquiler"  → sub2: null
  sub1 "Prestamos" → sub2: Dacia | Apartamento | Tarjeta
  sub1 "Colegios"  → sub2: null
  sub1 "Celulares" → sub2: Windtre | Illiad

categoria "Gastos variables":
  sub1 "Salud"           → sub2: Psicologa | Farmacia | Urgencias | Estudios | Gym
  sub1 "Servicios"       → sub2: Gas | Agua | Electricidad | Internet | Basura | Burocracia
  sub1 "Entretenimiento" → sub2: Netflix | Comer afuera | Fiesta
  sub1 "Transporte"      → sub2: Dacia | Ferrari | Autopista | Estacionamiento
                           sub3: Combustible | Nafta | Mantenimiento | Aseguracion | Impuesto | Revision tecnica | Reparacion | Lavado
  sub1 "Suscripciones"   → sub2: Impresora HP | Google ONE | Netflix
  sub1 "Supermercado"    → sub2: null
  sub1 "Estudio"         → sub2: Robotica | Ingles Hector | Ingles Luisiana
  sub1 "Vestimenta"      → sub2: Luisiana | Hector | Sofia | Noah
  sub1 "Bazar"           → sub2: Casa | Vari

EJEMPLOS:
- "gasté 30€ en farmacia"         → categoria:"Gastos variables", sub1:"Salud",      sub2:"Farmacia",  sub3:null
- "pagué el internet"             → categoria:"Gastos variables", sub1:"Servicios",  sub2:"Internet",  sub3:null
- "compré en el supermercado 85€" → categoria:"Gastos variables", sub1:"Supermercado", sub2:null,      sub3:null
- "pagué el alquiler"             → categoria:"Gastos Fijos",     sub1:"Alquiler",   sub2:null,        sub3:null
- "puse nafta a la Dacia, 60€"    → categoria:"Gastos variables", sub1:"Transporte", sub2:"Dacia",     sub3:"Nafta"
- "cobré el sueldo de Knapp"      → categoria:"Entradas",         sub1:"Hector",     sub2:"Knapp",     sub3:null
- "ropa para Sofía 45€"           → categoria:"Gastos variables", sub1:"Vestimenta", sub2:"Sofia",     sub3:null

REGLAS:
- Extrae SOLO movimientos explícitos en el texto. No inventes.
- amount siempre positivo y mayor que 0.
- transaction_date en YYYY-MM-DD; si no se menciona usa {today}.
- subcategoria3 SOLO para Transporte; en todos los demás casos: null.
- Si no hay ningún movimiento: {{"transactions": []}}.

Texto: "{text}"
"""


def extract_transactions(text: str) -> list[LLMTransactionOutput]:
    today = date.today().isoformat()
    prompt = _EXTRACTION_PROMPT.format(text=text, today=today)

    with httpx.Client(timeout=90.0) as client:
        resp = client.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "keep_alive": "5m",
                "options": {
                    "num_ctx": 4096,
                    "num_predict": 512,
                    "temperature": 0.1,
                    "num_thread": 6,
                },
            },
        )
        resp.raise_for_status()

    raw_list = json.loads(resp.json()["response"]).get("transactions", [])

    valid = []
    for item in raw_list:
        try:
            valid.append(LLMTransactionOutput.model_validate(item))
        except ValidationError as e:
            logger.warning("Transacción descartada: %s — %s", item, e)

    return valid
