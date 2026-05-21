"""Outbound Telegram send + media download."""

import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("telegram")


def _base() -> str:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    return f"https://api.telegram.org/bot{token}"


def _file_base() -> str:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    return f"https://api.telegram.org/file/bot{token}"


async def send_message(chat_id: int, text: str) -> dict:
    """POST sendMessage. Logs (but does not raise) on 4xx/5xx so the webhook
    handler can keep replying 200 to Telegram."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{_base()}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
    if r.status_code >= 400:
        log.error("send_message %d chat=%s body=%r resp=%s",
                  r.status_code, chat_id, text, r.text)
        return {}
    data = r.json()
    log.info("sent chat=%s message_id=%s", chat_id, data.get("result", {}).get("message_id"))
    return data


async def download_media(file_id: str) -> tuple[bytes, Optional[str]]:
    """Two-step: getFile → file_path, then fetch /file/bot<token>/<path>.
    Telegram doesn't return a mime_type — we infer from extension (good enough
    for Gemini: 'audio/ogg' for .oga/.ogg, 'image/jpeg' for .jpg, etc)."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        meta = await client.get(f"{_base()}/getFile", params={"file_id": file_id})
        meta.raise_for_status()
        file_path = meta.json()["result"]["file_path"]

        blob = await client.get(f"{_file_base()}/{file_path}")
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
