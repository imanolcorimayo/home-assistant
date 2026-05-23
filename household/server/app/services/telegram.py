"""Outbound Telegram send + media download.

Token is parametrised so the same helpers serve the Registrador bot
(`TELEGRAM_BOT_TOKEN`) and the Consultor bot (`CONSULTANT_TELEGRAM_BOT_TOKEN`).
When `token` is None, falls back to `TELEGRAM_BOT_TOKEN` — keeps existing
callers untouched.
"""

import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("telegram")


def _resolve_token(token: Optional[str]) -> str:
    return token if token else os.environ["TELEGRAM_BOT_TOKEN"]


def _base(token: Optional[str] = None) -> str:
    return f"https://api.telegram.org/bot{_resolve_token(token)}"


def _file_base(token: Optional[str] = None) -> str:
    return f"https://api.telegram.org/file/bot{_resolve_token(token)}"


async def send_message(chat_id: int, text: str, token: Optional[str] = None) -> dict:
    """POST sendMessage. Logs (but does not raise) on 4xx/5xx so the webhook
    handler can keep replying 200 to Telegram. `token` overrides the default
    bot token when sending from a non-Registrador bot."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{_base(token)}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
    if r.status_code >= 400:
        log.error("send_message %d chat=%s body=%r resp=%s",
                  r.status_code, chat_id, text, r.text)
        return {}
    data = r.json()
    log.info("sent chat=%s message_id=%s", chat_id, data.get("result", {}).get("message_id"))
    return data


async def download_media(
    file_id: str, token: Optional[str] = None
) -> tuple[bytes, Optional[str]]:
    """Two-step: getFile → file_path, then fetch /file/bot<token>/<path>.
    Telegram doesn't return a mime_type — we infer from extension (good enough
    for Gemini: 'audio/ogg' for .oga/.ogg, 'image/jpeg' for .jpg, etc).
    `token` overrides the default bot token for multi-bot setups."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        meta = await client.get(f"{_base(token)}/getFile", params={"file_id": file_id})
        meta.raise_for_status()
        file_path = meta.json()["result"]["file_path"]

        blob = await client.get(f"{_file_base(token)}/{file_path}")
        blob.raise_for_status()
        mime = _mime_from_path(file_path)
        log.info("downloaded file_id=%s path=%s mime=%s bytes=%d",
                 file_id, file_path, mime, len(blob.content))
        return blob.content, mime


def _mime_from_path(path: str) -> Optional[str]:
    p = path.lower()
    if p.endswith((".oga", ".ogg")):
        return "audio/ogg"
    if p.endswith(".mp3"):
        return "audio/mpeg"
    if p.endswith((".m4a", ".aac")):
        return "audio/mp4"
    if p.endswith(".wav"):
        return "audio/wav"
    if p.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".webp"):
        return "image/webp"
    return None
