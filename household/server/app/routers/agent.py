"""Agent endpoint — drives Gemini against the MCP server.

This is the user-facing side (reachable through the tunnel via the chat UI).
The MCP server it talks to stays internal.
"""

import logging

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel

from app.services.agent import run_agent

log = logging.getLogger("agent_router")

router = APIRouter(prefix="/api", tags=["agent"])


class AgentRequest(BaseModel):
    text: str
    session_id: str | None = None


class AgentResponse(BaseModel):
    reply: str


@router.post("/agent", response_model=AgentResponse)
async def agent_endpoint(req: AgentRequest) -> AgentResponse:
    text = (req.text or "").strip()
    if not text:
        return AgentResponse(reply="mensaje vacío")
    try:
        reply = await run_agent(text, session_id=req.session_id)
    except Exception as exc:
        log.exception("agent failed: %s", exc)
        reply = f"error del agente: {exc}"
    return AgentResponse(reply=reply)


@router.post("/agent/media", response_model=AgentResponse)
async def agent_media_endpoint(
    file: UploadFile = File(...),
    caption: str | None = Form(None),
    session_id: str | None = Form(None),
) -> AgentResponse:
    """Receipt photos / voice notes, routed through the same registrar agent.

    The model reads the media directly (no separate single-shot parser), so
    dedup, categorization and session memory work the same as for text.
    """
    data = await file.read()
    if not data:
        return AgentResponse(reply="archivo vacío")
    # Strip the codecs suffix Gemini rejects ("audio/webm;codecs=opus").
    mime = (file.content_type or "application/octet-stream").split(";")[0].strip()
    log.info("agent media: mime=%s bytes=%d caption=%r", mime, len(data), caption)
    try:
        reply = await run_agent(
            (caption or "").strip(),
            session_id=session_id,
            media=data,
            media_mime=mime,
        )
    except Exception as exc:
        log.exception("agent media failed: %s", exc)
        reply = f"error del agente: {exc}"
    return AgentResponse(reply=reply)
