"""
Wrapper sobre la Telegram Bot API.
- Funciones async: usadas desde FastAPI (webhook handler, comandos).
- Funciones sync: usadas desde Celery workers.
"""
import httpx

from app.core.config import settings

_BASE = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
_FILE_BASE = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}"


# ---------------------------------------------------------------------------
# Async (FastAPI)
# ---------------------------------------------------------------------------

async def send_message(chat_id: int, text: str) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(f"{_BASE}/sendMessage", json={"chat_id": chat_id, "text": text})


# ---------------------------------------------------------------------------
# Sync (Celery workers)
# ---------------------------------------------------------------------------

def send_message_sync(chat_id: int, text: str) -> None:
    with httpx.Client() as client:
        client.post(f"{_BASE}/sendMessage", json={"chat_id": chat_id, "text": text})


def download_file_sync(file_id: str) -> bytes:
    """Descarga un archivo de Telegram dado su file_id. Retorna los bytes crudos."""
    with httpx.Client(timeout=60.0) as client:
        # Paso 1: obtener file_path
        resp = client.get(f"{_BASE}/getFile", params={"file_id": file_id})
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]

        # Paso 2: descargar el archivo
        resp = client.get(f"{_FILE_BASE}/{file_path}")
        resp.raise_for_status()
        return resp.content
