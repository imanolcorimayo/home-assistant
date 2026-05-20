import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import engine
from app.routers import agent, chat, whatsapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="household")


@app.middleware("http")
async def no_store(request, call_next):
    # MVP test tool: never cache, so UI edits show up immediately on the
    # phone instead of being masked by Safari / Cloudflare edge caching.
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(whatsapp.router)
app.include_router(chat.router)
app.include_router(agent.router)


@app.get("/health")
async def health():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"db unreachable: {exc}")
    return {"status": "ok"}


# Mount LAST: a StaticFiles mount at "/" matches every path, so it must be
# registered after all explicit routes (/webhook, /api, /health) — those are
# checked first; only unmatched paths fall through to serve the chat UI.
# Guarded by isdir since the dir is a compose bind-mount (absent in tests).
_chat_dir = "/code/chat"
if os.path.isdir(_chat_dir):
    app.mount("/", StaticFiles(directory=_chat_dir, html=True), name="chat_static")
