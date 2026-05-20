"""Agent endpoint — drives Gemini against the MCP server.

This is the user-facing side (reachable through the tunnel via the chat UI).
The MCP server it talks to stays internal.
"""

import logging

from fastapi import APIRouter
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
