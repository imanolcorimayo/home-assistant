"""
Two approaches compared on the same screenshot:

  1) ocr+llama3.2:3b — PaddleOCR reads the screenshot to plain text, then
     a small text LLM structures it into JSON. Fast on CPU (~5–15s).

  2) minicpm-v — a true multimodal vision LLM does both steps in one model.
     Slower on CPU (~1–3 min), but more robust to weird layouts.

No expected-value inputs and no scoring — outputs are shown side-by-side for
visual comparison. CSV still records each run for offline analysis.
"""

import asyncio
import csv
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

OLLAMA_URL = os.environ["OLLAMA_URL"]
IMAGES_DIR = Path("/app/images")
CSV_PATH = Path("/app/results.csv")

# Approaches available. Single-approach for now; the list-shape stays so we
# can add more later (e.g., a tesseract variant, a different text LLM, etc.).
APPROACHES = ["ocr+llama3.2:3b"]

CSV_FIELDS = [
    "timestamp", "image", "approach",
    "latency_ms", "ocr_ms", "llm_ms",
    "predicted_kind", "predicted_amount", "predicted_title",
    "predicted_date", "predicted_currency",
    "ocr_text", "raw_output", "error",
]

# Prompt for the vision LLM path (sees the image directly).
VISION_PROMPT = """Analiza la captura de pantalla adjunta y extrae LA transaccion principal que aparezca.

Devuelve SOLO un JSON con esta estructura exacta (sin texto adicional):
{
  "kind": "expense" o "income",
  "title": "nombre del comercio o emisor o concepto (max 80 chars)",
  "amount": numero positivo (la magnitud, sin signo),
  "date": "YYYY-MM-DD",
  "currency": "ARS" o "USD" o "USDT"
}

Reglas:
- Argentina: el punto es separador de miles. "$67.506" = 67506. "$67.506,08" = 67506.08.
- amount es SIEMPRE positivo. El kind dice si es gasto o ingreso.
- kind="expense" si la captura muestra: "Pago", "Compra", "Transferencia enviada", "Debito", signo "-", texto rojo/negro.
- kind="income" si la captura muestra: "Cobro", "Transferencia recibida", "Sueldo", "Devolucion", signo "+", texto verde.
- title: usa el nombre del destinatario/comercio si esta visible (ej: "GIMNASIO TORRES SA"); si no, el concepto.
- date: YYYY-MM-DD. CRITICO: si la imagen NO MUESTRA el año explicitamente, el año DEBE ser 2026. No inventes años ni adivines por contexto.
- currency: "ARS" por default; "USD" si ves "USD"/"u$s"/"dolares"; "USDT" si ves "USDT"/"tether".

Responde UNICAMENTE el JSON.
"""

# Prompt for the OCR+LLM path (only sees the OCR'd text).
TEXT_PROMPT_TEMPLATE = """Te paso el texto OCR de una captura de pago/transferencia. Extrae LA transaccion principal.

Devuelve SOLO un JSON con esta estructura exacta (sin texto adicional):
{{
  "kind": "expense" o "income",
  "title": "nombre del comercio o emisor o concepto (max 80 chars)",
  "amount": numero positivo (la magnitud),
  "date": "YYYY-MM-DD",
  "currency": "ARS" o "USD" o "USDT"
}}

Reglas:
- Argentina: el punto es separador de miles. "$67.506" = 67506. "$67.506,08" = 67506.08.
- amount es SIEMPRE positivo.
- kind="expense" si ves "Pago", "Compra", "Transferencia enviada", "Debito", "-".
- kind="income" si ves "Cobro", "Transferencia recibida", "Sueldo", "Devolucion", "+".
- title: nombre del destinatario/comercio (ej: "GIMNASIO TORRES SA") o concepto si no hay.
- date: YYYY-MM-DD. CRITICO: si el texto NO MUESTRA el año, el año DEBE ser 2026.
- currency: "ARS" por default; "USD"/"USDT" solo si aparece explicitamente.

TEXTO OCR:
{ocr_text}

Responde UNICAMENTE el JSON."""

# ─── HTTP Basic Auth ─────────────────────────────────────────────────
# Single shared username/password loaded from .env. The browser prompts
# for them and remembers them for the session. `secrets.compare_digest`
# is constant-time so an attacker can't time-probe the password.
_BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "friend")
_BASIC_AUTH_PASS = os.environ["BASIC_AUTH_PASS"]
_security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    ok_user = secrets.compare_digest(
        credentials.username.encode("utf-8"), _BASIC_AUTH_USER.encode("utf-8")
    )
    ok_pass = secrets.compare_digest(
        credentials.password.encode("utf-8"), _BASIC_AUTH_PASS.encode("utf-8")
    )
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# Applying the dependency at the app level covers every route below.
app = FastAPI(dependencies=[Depends(require_auth)])

# ─── lazy PaddleOCR init ─────────────────────────────────────────────
# First call loads ~30–50 MB of models. We do it once per process and reuse.
_OCR_INSTANCE: Any = None


def get_ocr() -> Any:
    global _OCR_INSTANCE
    if _OCR_INSTANCE is None:
        from paddleocr import PaddleOCR
        # lang='latin' covers Spanish and English. show_log=False keeps stdout clean.
        _OCR_INSTANCE = PaddleOCR(lang="latin", use_angle_cls=False, show_log=False)
    return _OCR_INSTANCE


def extract_text_lines(ocr_result: Any) -> list[str]:
    """Handle both PaddleOCR v2.x and v3.x result shapes."""
    lines: list[str] = []
    if not ocr_result:
        return lines
    # v3.x: list of dicts with 'rec_texts'
    if isinstance(ocr_result[0], dict) and "rec_texts" in ocr_result[0]:
        for page in ocr_result:
            lines.extend(page.get("rec_texts") or [])
        return lines
    # v2.x: nested list — [page][line][bbox, (text, confidence)]
    for page in ocr_result:
        if not page:
            continue
        for entry in page:
            try:
                lines.append(entry[1][0])
            except (IndexError, TypeError):
                pass
    return lines


# ─── CSV helpers ─────────────────────────────────────────────────────

def ensure_csv_header() -> None:
    if CSV_PATH.exists() and CSV_PATH.stat().st_size > 0:
        return
    with CSV_PATH.open("w", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()


def append_row(row: dict[str, Any]) -> None:
    ensure_csv_header()
    with CSV_PATH.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(row)


# ─── number parsing (for clean CSV columns) ──────────────────────────

def parse_amount(s: Any) -> float | None:
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    raw = re.sub(r"[^\d.,\-]", "", str(s))
    if not raw:
        return None
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        parts = raw.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            raw = parts[0] + "." + parts[1]
        else:
            raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


# ─── runners ─────────────────────────────────────────────────────────

async def run_vision_llm(client: httpx.AsyncClient, model: str, image_b64: str) -> dict:
    """Vision LLM path: image → JSON in one shot."""
    import base64  # noqa: F401  (kept local — base64 already imported elsewhere by caller)
    t0 = time.monotonic()
    try:
        r = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": VISION_PROMPT,
                "images": [image_b64],
                "stream": False,
                "format": "json",
                "keep_alive": "5m",
                "options": {"temperature": 0.1},
            },
            timeout=300.0,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            return {"latency_ms": latency_ms, "ocr_ms": None, "llm_ms": latency_ms,
                    "parsed": {}, "ocr_text": "", "error": f"http_{r.status_code}: {r.text[:200]}"}
        text = r.json().get("response", "")
        try:
            parsed = json.loads(text)
            return {"latency_ms": latency_ms, "ocr_ms": None, "llm_ms": latency_ms,
                    "parsed": parsed, "ocr_text": "", "error": None}
        except json.JSONDecodeError as e:
            return {"latency_ms": latency_ms, "ocr_ms": None, "llm_ms": latency_ms,
                    "parsed": {"_raw": text}, "ocr_text": "", "error": f"json_parse: {e}"}
    except Exception as e:
        return {"latency_ms": int((time.monotonic() - t0) * 1000),
                "ocr_ms": None, "llm_ms": None,
                "parsed": {}, "ocr_text": "", "error": f"exception: {e}"}


async def run_ocr_llm(client: httpx.AsyncClient, image_path: Path) -> dict:
    """OCR + text LLM path: PaddleOCR → text → llama3.2:3b → JSON."""
    # Step 1: OCR. Runs in the FastAPI process; offload to a thread so it
    # doesn't block the event loop (PaddleOCR is synchronous numpy code).
    t0 = time.monotonic()
    try:
        ocr = get_ocr()
        raw_result = await asyncio.to_thread(_run_ocr_sync, ocr, str(image_path))
        lines = extract_text_lines(raw_result)
        ocr_text = "\n".join(lines).strip()
        ocr_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        return {"latency_ms": int((time.monotonic() - t0) * 1000),
                "ocr_ms": None, "llm_ms": None,
                "parsed": {}, "ocr_text": "", "error": f"ocr_exception: {e}"}

    if not ocr_text:
        return {"latency_ms": ocr_ms, "ocr_ms": ocr_ms, "llm_ms": 0,
                "parsed": {}, "ocr_text": "",
                "error": "ocr returned no text (image unreadable?)"}

    # Step 2: text LLM.
    t1 = time.monotonic()
    try:
        r = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": "llama3.2:3b",
                "prompt": TEXT_PROMPT_TEMPLATE.format(ocr_text=ocr_text),
                "stream": False,
                "format": "json",
                "keep_alive": "5m",
                "options": {"temperature": 0.1},
            },
            timeout=120.0,
        )
        llm_ms = int((time.monotonic() - t1) * 1000)
        latency_ms = ocr_ms + llm_ms
        if r.status_code != 200:
            return {"latency_ms": latency_ms, "ocr_ms": ocr_ms, "llm_ms": llm_ms,
                    "parsed": {}, "ocr_text": ocr_text,
                    "error": f"http_{r.status_code}: {r.text[:200]}"}
        text = r.json().get("response", "")
        try:
            parsed = json.loads(text)
            return {"latency_ms": latency_ms, "ocr_ms": ocr_ms, "llm_ms": llm_ms,
                    "parsed": parsed, "ocr_text": ocr_text, "error": None}
        except json.JSONDecodeError as e:
            return {"latency_ms": latency_ms, "ocr_ms": ocr_ms, "llm_ms": llm_ms,
                    "parsed": {"_raw": text}, "ocr_text": ocr_text,
                    "error": f"json_parse: {e}"}
    except Exception as e:
        return {"latency_ms": int((time.monotonic() - t0) * 1000),
                "ocr_ms": ocr_ms, "llm_ms": None,
                "parsed": {}, "ocr_text": ocr_text, "error": f"llm_exception: {e}"}


def _run_ocr_sync(ocr: Any, path: str) -> Any:
    """Wrapper for asyncio.to_thread — try the new API first, fall back to old."""
    if hasattr(ocr, "predict"):
        try:
            return ocr.predict(path)
        except Exception:
            pass
    return ocr.ocr(path)


# ─── routes ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    with open("static/index.html") as f:
        html = f.read()
    options = "\n".join(
        f'<label class="approach"><input type="checkbox" name="approaches" value="{a}" checked> {a}</label>'
        for a in APPROACHES
    )
    return html.replace("{{APPROACH_OPTIONS}}", options)


@app.get("/results.csv")
def download_csv() -> FileResponse:
    ensure_csv_header()
    return FileResponse(CSV_PATH, media_type="text/csv", filename="results.csv")


@app.post("/run")
async def run(
    image: UploadFile = File(...),
    approaches: list[str] = Form(...),
) -> JSONResponse:
    import base64

    # Persist the uploaded image so the CSV row can reference it later.
    suffix = Path(image.filename or "img").suffix or ".png"
    image_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}{suffix}"
    image_path = IMAGES_DIR / image_id
    raw_bytes = await image.read()
    image_path.write_bytes(raw_bytes)
    image_b64 = base64.b64encode(raw_bytes).decode()

    results = []
    async with httpx.AsyncClient() as client:
        for approach in approaches:
            if approach == "ocr+llama3.2:3b":
                r = await run_ocr_llm(client, image_path)
            elif approach == "minicpm-v":
                r = await run_vision_llm(client, "minicpm-v", image_b64)
            else:
                r = {"latency_ms": 0, "ocr_ms": None, "llm_ms": None,
                     "parsed": {}, "ocr_text": "", "error": f"unknown approach: {approach}"}

            parsed = r["parsed"] or {}
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "image": image_id,
                "approach": approach,
                "latency_ms": r["latency_ms"],
                "ocr_ms": r["ocr_ms"],
                "llm_ms": r["llm_ms"],
                "predicted_kind": parsed.get("kind"),
                "predicted_amount": parse_amount(parsed.get("amount")),
                "predicted_title": parsed.get("title"),
                "predicted_date": parsed.get("date"),
                "predicted_currency": parsed.get("currency"),
                "ocr_text": r["ocr_text"][:2000],
                "raw_output": json.dumps(parsed, ensure_ascii=False)[:2000],
                "error": r["error"] or "",
            }
            append_row(row)
            results.append(row)

    return JSONResponse({"image": image_id, "results": results})
