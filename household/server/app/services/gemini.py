"""Gemini REST client with model rotation, daily exhaustion cache, 503 rotation.

Pattern lifted from wiseutils/pay-trackr (GeminiHandler.php) and gasto-obra
(GeminiHandler.js). Adapted for async Python; no SDK, just httpx + REST.

Behavior:
- Try each model in MODELS order.
- 429 → mark model exhausted for today, rotate to next.
- 503 / transport error → rotate immediately (no in-model retry; we have
  cheap rotation, no need to wait).
- 200 + valid response → return.
- All models exhausted/failed → return None.
- TOTAL_BUDGET caps wall-clock time across attempts so a slow request
  can't stall the WhatsApp webhook past its 20s timeout.
"""

import asyncio
import json
import logging
import os
from datetime import date
from pathlib import Path

import httpx

log = logging.getLogger("gemini")

MODELS = [
    "gemini-2.5-flash-lite",   # primary — cheapest, fast enough for parsing
    "gemini-2.5-flash",        # fallback — sturdier on tricky audio/images
    "gemini-2.5-pro",          # last resort
]

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
REQUEST_TIMEOUT = 45.0   # per-model HTTP timeout
TOTAL_BUDGET = 90.0      # whole rotation budget; keep under webhook ack window
EXHAUSTED_CACHE = Path("/tmp/household-gemini-exhausted.json")


def _load_exhausted() -> dict:
    try:
        return json.loads(EXHAUSTED_CACHE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _is_exhausted(model: str) -> bool:
    return _load_exhausted().get(model) == date.today().isoformat()


def _mark_exhausted(model: str) -> None:
    data = _load_exhausted()
    data[model] = date.today().isoformat()
    EXHAUSTED_CACHE.write_text(json.dumps(data))


async def generate(
    parts: list[dict],
    response_schema: dict | None = None,
    temperature: float = 0.2,
    max_output_tokens: int = 2000,
):
    """Call Gemini with the given content parts.

    Returns:
      - dict if response_schema is provided and parsing succeeds
      - str if no schema (raw text)
      - None on total failure
    """
    api_key = os.environ["GEMINI_API_KEY"]

    gen_config: dict = {"temperature": temperature, "maxOutputTokens": max_output_tokens}
    if response_schema is not None:
        gen_config["responseMimeType"] = "application/json"
        gen_config["responseSchema"] = response_schema

    payload = {"contents": [{"parts": parts}], "generationConfig": gen_config}

    loop = asyncio.get_event_loop()
    start = loop.time()
    tried: list[str] = []

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for model in MODELS:
            if loop.time() - start > TOTAL_BUDGET:
                log.warning("budget exceeded, skipping %s. tried=%s", model, tried)
                break
            if _is_exhausted(model):
                tried.append(f"{model}(exhausted)")
                continue

            tried.append(model)
            t0 = loop.time()
            try:
                r = await client.post(
                    f"{BASE_URL}/{model}:generateContent?key={api_key}",
                    json=payload,
                )
            except httpx.RequestError as exc:
                log.warning("%s transport error: %s", model, exc)
                continue

            dur_ms = int((loop.time() - t0) * 1000)

            if r.status_code == 429:
                _mark_exhausted(model)
                log.warning("%s 429 in %dms — marked exhausted", model, dur_ms)
                continue
            if r.status_code == 503:
                log.warning("%s 503 in %dms — rotating", model, dur_ms)
                continue
            if r.status_code != 200:
                log.warning("%s HTTP %d: %s", model, r.status_code, r.text[:300])
                continue

            body = r.json()
            try:
                text = body["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError):
                log.warning("%s unexpected shape: %s", model, str(body)[:300])
                continue

            if not text:
                log.warning("%s empty text", model)
                continue

            if response_schema is None:
                log.info("%s ok in %dms (text)", model, dur_ms)
                return text

            try:
                parsed = json.loads(text)
                log.info("%s ok in %dms (json)", model, dur_ms)
                return parsed
            except json.JSONDecodeError:
                log.warning("%s JSON parse failed: %s", model, text[:300])
                continue

    log.error("all gemini models failed. tried=%s", tried)
    return None
