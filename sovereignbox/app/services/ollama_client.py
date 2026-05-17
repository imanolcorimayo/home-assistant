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
      "confidence": <0.0 a 1.0>,
      "medio_pago": "<tarjeta_credito | efectivo | cuenta | null>",
      "cuenta_hint": "<hector | luisiana | casa | null>"
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

DESAMBIGUACIÓN:
- "Dacia" + cuota/préstamo/financiación/mensualidad → Gastos Fijos → Prestamos → Dacia
- "Dacia" + nafta/gasoil/combustible/peaje/autopista/lavado/mantenimiento → Gastos variables → Transporte → Dacia + sub3
- "Netflix" mensual/suscripción → Gastos variables → Suscripciones → Netflix
- "Netflix" película/evento puntual → Gastos variables → Entretenimiento → Netflix
- "Windtre" o "Illiad" → SIEMPRE Gastos Fijos → Celulares (nunca Servicios → Internet)
- Si hay dos mapeos posibles: confidence < 0.75

EJEMPLOS:
- "gasté 30€ en farmacia"              → Gastos variables, sub1:Salud,        sub2:Farmacia,      sub3:null
- "pagué el internet"                  → Gastos variables, sub1:Servicios,    sub2:Internet,      sub3:null
- "compré en el supermercado 85€"      → Gastos variables, sub1:Supermercado, sub2:null,          sub3:null
- "pagué el alquiler"                  → Gastos Fijos,     sub1:Alquiler,     sub2:null,          sub3:null, confidence:0.5
- "puse nafta a la Dacia, 60€"         → Gastos variables, sub1:Transporte,   sub2:Dacia,         sub3:Nafta
- "cobré el sueldo de Knapp"           → Entradas,         sub1:Hector,       sub2:Knapp,         sub3:null
- "ropa para Sofía 45€"                → Gastos variables, sub1:Vestimenta,   sub2:Sofia,         sub3:null
- "gasto de 25€ en supermercado"       → Gastos variables, sub1:Supermercado, sub2:null,          sub3:null
- "gasto de 30 euros en farmacia"      → Gastos variables, sub1:Salud,        sub2:Farmacia,      sub3:null
- "gasto 50€ supermercado"             → Gastos variables, sub1:Supermercado, sub2:null,          sub3:null
- "ingreso de 120€ de Knapp"           → Entradas,         sub1:Hector,       sub2:Knapp,         sub3:null
- "cobró Luisiana con Constan 800€"    → Entradas,         sub1:Luisiana,     sub2:Constan,       sub3:null
- "llegó el assegno 200€"              → Entradas,         sub1:Buroc,        sub2:Assegno,       sub3:null
- "cobré horas extras 150€"            → Entradas,         sub1:Hector,       sub2:Hs extras,     sub3:null
- "pagué el colegio 350€"              → Gastos Fijos,     sub1:Colegios,     sub2:null,          sub3:null
- "pagué el Windtre 15€"               → Gastos Fijos,     sub1:Celulares,    sub2:Windtre,       sub3:null
- "pagué la cuota del Illiad"          → Gastos Fijos,     sub1:Celulares,    sub2:Illiad,        sub3:null, confidence:0.5
- "gasté 30€ en farmacia y 85€ en el súper" → [{{Salud/Farmacia/null}}, {{Supermercado/null/null}}]
- "compré 200€ en el super con la tarjeta"   → Supermercado, medio_pago:"tarjeta_credito"
- "pagué 50€ en efectivo del fondo de casa"  → medio_pago:"efectivo", cuenta_hint:"casa"
- "Lu pagó 80€ con su débito"                → cuenta_hint:"luisiana", medio_pago:"cuenta"

MEDIO DE PAGO Y CUENTA (campos opcionales — si no se mencionan, devolvé null):
- "con la tarjeta", "con visa", "con la credit", "tarjeta de crédito" → medio_pago: "tarjeta_credito"
- "en efectivo", "cash", "billetes", "lo pagué en mano" → medio_pago: "efectivo"
- "transferí", "desde mi cuenta", "con débito", "con la tarjeta de débito" → medio_pago: "cuenta"
- "lo pagó Hector / yo" → cuenta_hint: "hector"
- "lo pagó Luisiana / Lu" → cuenta_hint: "luisiana"
- "del fondo de casa", "ahorros de casa", "del efectivo de casa" → cuenta_hint: "casa"
- Si NO se menciona explícitamente: medio_pago=null, cuenta_hint=null. NO inventes.

REGLAS:
- El texto puede ser verbal ("gasté X€", "pagué X€") o nominal ("gasto de X€ en Y",
  "ingreso de X€ de Z"). Ambas formas son movimientos válidos.
- El texto puede venir de transcripción de audio (Whisper); ignorá errores leves:
  "daca" = Dacia, "witre" = Windtre, "knap" = Knapp.
- Extrae SOLO movimientos explícitos en el texto. No inventes.
- amount siempre positivo y mayor que 0.
- transaction_date en YYYY-MM-DD; si no se menciona usa {today}.
- subcategoria3 SOLO para Transporte; en todos los demás casos: null.
- sub2 null OBLIGATORIO para: Alquiler, Colegios, Supermercado.
- Si el monto no está explícito en el texto: confidence < 0.6.
- Si no hay ningún movimiento: {{"transactions": []}}.

Texto: "{text}"
"""


_FILING_PROMPT = """\
Eres un asistente que organiza papeles físicos de una familia. Dado un comprobante \
con su categoría y fecha, sugerí una ruta de carpeta física en español, breve y \
jerárquica, separada por ' → '.

Reglas:
- Máximo 4 niveles.
- Primer nivel: tipo macro (Servicios | Salud | Transporte | Hogar | Educación | Finanzas | Documentos | Varios).
- Último nivel siempre: año o "{anio} - {mes_nombre}".
- Devuelve EXCLUSIVAMENTE un JSON válido: {{"ruta": "...", "razon": "..."}}.

Datos:
- categoria: {categoria}
- subcategoria1: {sub1}
- subcategoria2: {sub2}
- nota: {nota}
- mime: {mime}
- fecha: {fecha}

Respuesta JSON:
"""


def suggest_filing_path(
    *,
    categoria: str | None,
    subcategoria1: str | None,
    subcategoria2: str | None,
    nota: str | None,
    mime: str,
    fecha,
) -> dict:
    """Llama al LLM para sugerir dónde guardar el papel físico. Devuelve {ruta, razon}."""
    nombres_mes = ['Enero','Febrero','Marzo','Abril','Mayo','Junio',
                   'Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre']
    fecha_str = fecha.isoformat() if hasattr(fecha, "isoformat") else str(fecha)
    mes_nombre = nombres_mes[fecha.month - 1] if hasattr(fecha, "month") else "Mes"
    prompt = _FILING_PROMPT.format(
        anio=fecha.year if hasattr(fecha, "year") else "Año",
        mes_nombre=mes_nombre,
        categoria=categoria or "—",
        sub1=subcategoria1 or "—",
        sub2=subcategoria2 or "—",
        nota=(nota or "—")[:120],
        mime=mime,
        fecha=fecha_str,
    )
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "keep_alive": "5m",
                "options": {"num_ctx": 1024, "num_predict": 128, "temperature": 0.2},
            },
        )
        resp.raise_for_status()

    try:
        data = json.loads(resp.json()["response"])
    except Exception:
        data = {}

    ruta = data.get("ruta") or "Varios → " + (subcategoria1 or "Sin categoría")
    razon = data.get("razon") or "Sugerencia heurística"
    return {"ruta": ruta, "razon": razon}


_SHOPPING_PROMPT = """\
Extraé items de compras desde un mensaje en español. El usuario puede dictar
varios items en uno solo, separados por coma, "y", o salto de línea.

Devuelve EXCLUSIVAMENTE un JSON con este formato:
{{
  "items": [
    {{"texto": "<nombre del producto>", "cantidad": <numero o null>, "unidad": "<kg|l|u|null>"}}
  ]
}}

Reglas:
- "texto" es lo más conciso posible: 'leche', 'pan integral', 'tomate', no 'comprar leche'.
- "cantidad" solo si el usuario lo especifica numéricamente. Si dice "una", "dos", convertir a número.
- "unidad": 'kg' (kilos), 'g' (gramos), 'l' (litros), 'ml', 'u' (unidades). Null si no aplica.
- Ignorá verbos: "anotame", "comprar", "necesito", "agregame".
- Si no hay items reales: {{"items": []}}.

Ejemplos:
- "anotame leche, pan y 2 tomates"          → [{{"texto":"leche","cantidad":null,"unidad":null}},
                                               {{"texto":"pan","cantidad":null,"unidad":null}},
                                               {{"texto":"tomate","cantidad":2,"unidad":"u"}}]
- "necesito 2kg de papas y 1 litro de aceite"
                                            → [{{"texto":"papa","cantidad":2,"unidad":"kg"}},
                                               {{"texto":"aceite","cantidad":1,"unidad":"l"}}]
- "comprar yogur"                           → [{{"texto":"yogur","cantidad":null,"unidad":null}}]

Mensaje: "{text}"
JSON:
"""


def extract_shopping_items(text: str) -> list[dict]:
    """Devuelve lista de {texto, cantidad, unidad}. Lista vacía si no hay items."""
    prompt = _SHOPPING_PROMPT.format(text=text)
    try:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{settings.ollama_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                    "keep_alive": "5m",
                    "options": {"num_ctx": 1024, "num_predict": 256, "temperature": 0.0},
                },
            )
            resp.raise_for_status()
        data = json.loads(resp.json()["response"])
        items = data.get("items", []) if isinstance(data, dict) else []
        # Normalización defensiva
        cleaned = []
        for it in items:
            if not isinstance(it, dict):
                continue
            txt = (it.get("texto") or "").strip()
            if not txt:
                continue
            cantidad = it.get("cantidad")
            try:
                cantidad = float(cantidad) if cantidad not in (None, "", "null") else None
            except (ValueError, TypeError):
                cantidad = None
            unidad = it.get("unidad")
            if unidad in ("null", "", None):
                unidad = None
            cleaned.append({"texto": txt[:120], "cantidad": cantidad, "unidad": unidad})
        return cleaned
    except Exception as exc:
        logger.warning("extract_shopping_items falló: %s", exc)
        return []


_EVENT_PROMPT = """\
Sos un asistente que organiza la agenda de una familia italiana de habla
española en {today_dow} {today}. El usuario te dicta uno o más eventos en español.
Devuelve EXCLUSIVAMENTE un JSON con este formato:

{{
  "eventos": [
    {{
      "titulo": "<corto, ej. 'Dentista Sofía' o 'Reunión escuela'>",
      "fecha": "YYYY-MM-DD",
      "hora": "HH:MM" o null,
      "categoria": "medico|colegio|burocracia|familia|otro",
      "descripcion": "<detalle libre o null>",
      "ubicacion": "<lugar o null>"
    }}
  ]
}}

Reglas para fecha:
- "hoy" = {today}, "mañana" = mañana, "pasado" = +2 días.
- "el lunes/martes/...": el próximo día de la semana >= hoy.
- "el 15", "el 23 de mayo" → ese día (asumir mes actual si no se especifica, o el más cercano futuro).
- Si no se menciona fecha clara → omitir el evento.

Reglas para hora:
- "10hs", "10:30", "a las 10", "10 de la mañana" → "10:00", "10:30".
- "tarde": null (mejor null que adivinar).
- "de noche": null.

Reglas para categoría:
- dentista/médico/pediatra/turno/análisis/farmacia/cirugía → medico
- colegio/escuela/profesora/reunión escolar/clase de... → colegio
- trámite/AFIP/banco/notaría/oficina pública → burocracia
- cumple/asado/cena familiar/visita → familia
- resto → otro

Si no hay eventos extraíbles: {{"eventos": []}}.

Mensaje: "{text}"
JSON:
"""


def extract_events(text: str) -> list[dict]:
    """Extrae eventos del texto. Devuelve lista de dicts."""
    today = date.today()
    nombres_dia = ['lunes','martes','miércoles','jueves','viernes','sábado','domingo']
    prompt = _EVENT_PROMPT.format(
        text=text,
        today=today.isoformat(),
        today_dow=nombres_dia[today.weekday()],
    )
    try:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{settings.ollama_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                    "keep_alive": "5m",
                    "options": {"num_ctx": 1536, "num_predict": 384, "temperature": 0.1},
                },
            )
            resp.raise_for_status()
        data = json.loads(resp.json()["response"])
        eventos = data.get("eventos", []) if isinstance(data, dict) else []
        cleaned = []
        for e in eventos:
            if not isinstance(e, dict) or not e.get("titulo") or not e.get("fecha"):
                continue
            cat = e.get("categoria") or "otro"
            if cat not in {"medico", "colegio", "burocracia", "familia", "otro"}:
                cat = "otro"
            cleaned.append({
                "titulo":      str(e["titulo"])[:120],
                "fecha":       str(e["fecha"]),
                "hora":        e.get("hora"),
                "categoria":   cat,
                "descripcion": e.get("descripcion"),
                "ubicacion":   e.get("ubicacion"),
            })
        return cleaned
    except Exception as exc:
        logger.warning("extract_events falló: %s", exc)
        return []


_TASK_PROMPT = """\
Sos un asistente que organiza una lista de tareas (TODO) familiar. El usuario
te dicta una o más tareas en español. Devuelve EXCLUSIVAMENTE un JSON:

{{
  "tareas": [
    {{
      "titulo": "<verbo + objeto, breve, ej. 'Reparar canilla'>",
      "prioridad": "baja|normal|alta",
      "asignado": "hector|luisiana|null",
      "due_date": "YYYY-MM-DD" o null
    }}
  ]
}}

Reglas:
- Convertir "tengo que X" / "hay que X" en "X" verbo en infinitivo capitalizado.
- "@hector" o "@he" → asignado: "hector"
- "@luisiana" o "@lu" → asignado: "luisiana"
- Sin mención: asignado: null (cualquiera).
- "urgente", "ya", "ASAP" → prioridad: "alta"; resto "normal".
- Si menciona fecha (mañana, lunes, el 20): due_date. Sin fecha: null.

Hoy es {today}.

Ejemplos:
- "tengo que reparar la canilla"          → [{{"titulo":"Reparar canilla","prioridad":"normal","asignado":null,"due_date":null}}]
- "@lu llevar libros a la biblioteca"     → [{{"titulo":"Llevar libros a la biblioteca","prioridad":"normal","asignado":"luisiana","due_date":null}}]
- "urgente llamar al gas"                 → [{{"titulo":"Llamar al gas","prioridad":"alta","asignado":null,"due_date":null}}]
- "comprar pilas, sacar la basura"        → [{{"titulo":"Comprar pilas","prioridad":"normal","asignado":null,"due_date":null}},
                                              {{"titulo":"Sacar la basura","prioridad":"normal","asignado":null,"due_date":null}}]

Si no hay tareas: {{"tareas": []}}.

Mensaje: "{text}"
JSON:
"""


def extract_tasks(text: str) -> list[dict]:
    """Extrae tareas del texto. Devuelve lista de dicts."""
    today = date.today()
    prompt = _TASK_PROMPT.format(text=text, today=today.isoformat())
    try:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{settings.ollama_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                    "keep_alive": "5m",
                    "options": {"num_ctx": 1024, "num_predict": 256, "temperature": 0.1},
                },
            )
            resp.raise_for_status()
        data = json.loads(resp.json()["response"])
        tareas = data.get("tareas", []) if isinstance(data, dict) else []
        cleaned = []
        for t in tareas:
            if not isinstance(t, dict):
                continue
            titulo = (t.get("titulo") or "").strip()
            if not titulo:
                continue
            prio = t.get("prioridad", "normal")
            if prio not in {"baja", "normal", "alta"}:
                prio = "normal"
            asignado = t.get("asignado")
            if asignado in ("null", "", None):
                asignado = None
            cleaned.append({
                "titulo":    titulo[:500],
                "prioridad": prio,
                "asignado":  asignado,
                "due_date":  t.get("due_date") or None,
            })
        return cleaned
    except Exception as exc:
        logger.warning("extract_tasks falló: %s", exc)
        return []


def warm_up() -> None:
    """Pre-carga el modelo de Ollama en RAM para evitar el cold start de la 1ª dictada.

    Hace una request mínima sin esperar respuesta útil. Si Ollama está caído
    o el modelo no existe, loguea pero no falla — el worker arranca igual.
    """
    try:
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(
                f"{settings.ollama_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": "ok",
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {"num_ctx": 256, "num_predict": 4, "temperature": 0},
                },
            )
            resp.raise_for_status()
            logger.info("Ollama warm-up OK (modelo: %s)", settings.ollama_model)
    except Exception as exc:
        logger.warning("Ollama warm-up falló (no crítico): %s", exc)


def extract_transactions(text: str) -> list[LLMTransactionOutput]:
    today = date.today().isoformat()
    prompt = _EXTRACTION_PROMPT.format(text=text, today=today)

    with httpx.Client(timeout=180.0) as client:
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
