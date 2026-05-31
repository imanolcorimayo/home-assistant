"""assistant — the unified multi-tenant family financial assistant.

Single FastAPI app (collapses household's api + web + chat split). Multi-tenant:
every row is scoped by family_id, taken from the logged-in member's session.
Raw SQL via app.db — no ORM. Server-rendered with Jinja2.

#25 scaffold = boots + /health. #26 = Google login + session + family bootstrap
(this file wires the session, the auth router, and the logged-in landing page).
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import agent, config, db
from app.auth import current_member, router as auth_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool()
    yield
    await db.close_pool()


app = FastAPI(title="assistant", lifespan=lifespan)

# Signed session cookie (≈ PHP $_SESSION). Must come before routes use it.
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET)

app.include_router(auth_router)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

# Auto cache-bust: ?v=<style.css mtime>, bumped whenever the CSS is rebuilt.
_CSS = "app/static/style.css"
templates.env.globals["static_ver"] = int(os.path.getmtime(_CSS)) if os.path.exists(_CSS) else 0


@app.get("/")
async def home(request: Request):
    member = await current_member(request)
    if member is None:
        return RedirectResponse("/login", status_code=303)
    family = await db.fetchrow(
        "SELECT * FROM family WHERE family_id = $1", member["family_id"]
    )
    return templates.TemplateResponse(
        "home.html", {"request": request, "member": member, "family": family}
    )


@app.get("/login")
async def login(request: Request):
    # Already signed in → straight to home.
    if await current_member(request) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})


# Display pages — placeholders for now; the shell + global chat work today, the
# content lands in later steps (dashboard, movimientos list, config CRUD, sessions).
_PAGES = {
    "/movimientos":   ("Movimientos", "El listado de transacciones con búsqueda y edición llega pronto."),
    "/configuracion": ("Configuración", "Categorías, cuentas y presupuestos llegan pronto."),
}


@app.get("/movimientos")
@app.get("/configuracion")
async def page(request: Request):
    member = await current_member(request)
    if member is None:
        return RedirectResponse("/login", status_code=303)
    title, note = _PAGES[request.url.path]
    return templates.TemplateResponse(
        "placeholder.html",
        {"request": request, "member": member, "title": title, "note": note},
    )


@app.post("/chat/message")
async def chat_message(request: Request):
    """The one endpoint the chat UI talks to. family_id/member_id come from the
    session inside agent.handle — never from the request body. `session_id` (the
    conversation thread) is optional: omitted/empty starts a new thread. The
    reply carries back the session_id so the client can keep the thread going."""
    member = await current_member(request)
    if member is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    body = await request.json()
    text = (body.get("message") or "").strip()
    if not text:
        return JSONResponse({"error": "empty message"}, status_code=400)
    session_id = (body.get("session_id") or "").strip() or None
    try:
        reply, session_id = await agent.handle(text, member, session_id)
    except Exception:  # noqa: BLE001
        logging.getLogger("chat").exception("agent.handle failed")
        return JSONResponse(
            {"reply": "Hubo un problema procesando el mensaje. Probá de nuevo."},
            status_code=200,
        )
    return JSONResponse({"reply": reply, "session_id": session_id})


@app.get("/chat/sessions")
async def chat_sessions(request: Request):
    """The logged-in member's conversation threads, newest first — for the
    resume picker in the chat modal. Scoped to family + member."""
    member = await current_member(request)
    if member is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    rows = await db.fetch(
        """
        SELECT s.chat_session_id, s.title,
               COALESCE(s.updated_ts, s.created_ts) AS last_ts,
               count(r.agent_run_id) AS runs
        FROM chat_session s
        LEFT JOIN agent_run r ON r.chat_session_id = s.chat_session_id
        WHERE s.family_id = $1 AND s.member_id = $2
        GROUP BY s.chat_session_id
        ORDER BY last_ts DESC
        LIMIT 50
        """,
        member["family_id"], member["member_id"],
    )
    return JSONResponse({"sessions": [
        {"session_id": str(r["chat_session_id"]), "title": r["title"],
         "last_ts": r["last_ts"].isoformat() if r["last_ts"] else None,
         "runs": r["runs"]}
        for r in rows
    ]})


@app.get("/chat/sessions/{session_id}/messages")
async def chat_session_messages(request: Request, session_id: str):
    """Replay a thread's messages (rebuilt from agent_run) so the modal can
    render it when the member resumes it. 404 if it isn't theirs."""
    member = await current_member(request)
    if member is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    owns = await db.fetchval(
        """
        SELECT 1 FROM chat_session
        WHERE chat_session_id = $1::uuid AND family_id = $2 AND member_id = $3
        """,
        session_id, member["family_id"], member["member_id"],
    )
    if not owns:
        return JSONResponse({"error": "not found"}, status_code=404)
    rows = await db.fetch(
        """
        SELECT input_text, reply_text FROM agent_run
        WHERE chat_session_id = $1::uuid AND error IS NULL AND reply_text IS NOT NULL
        ORDER BY created_ts
        """,
        session_id,
    )
    messages = []
    for r in rows:
        messages.append({"who": "me", "text": r["input_text"] or ""})
        messages.append({"who": "bot", "text": r["reply_text"] or ""})
    return JSONResponse({"messages": messages})


@app.get("/actividad")
async def actividad(request: Request):
    """Per-session token + tool-call usage, rolled up from agent_run rows."""
    member = await current_member(request)
    if member is None:
        return RedirectResponse("/login", status_code=303)
    sessions = await db.fetch(
        """
        SELECT s.chat_session_id, s.title,
               COALESCE(s.updated_ts, s.created_ts)            AS last_ts,
               count(r.agent_run_id)                           AS runs,
               COALESCE(sum(r.total_tokens), 0)                AS tokens,
               COALESCE(sum(jsonb_array_length(r.tool_calls)), 0) AS tool_calls,
               count(r.error)                                  AS errors
        FROM chat_session s
        LEFT JOIN agent_run r ON r.chat_session_id = s.chat_session_id
        WHERE s.family_id = $1 AND s.member_id = $2
        GROUP BY s.chat_session_id
        ORDER BY last_ts DESC
        LIMIT 100
        """,
        member["family_id"], member["member_id"],
    )
    totals = {
        "sessions": len(sessions),
        "runs": sum(s["runs"] for s in sessions),
        "tokens": sum(s["tokens"] for s in sessions),
        "tool_calls": sum(s["tool_calls"] for s in sessions),
    }
    return templates.TemplateResponse(
        "actividad.html",
        {"request": request, "member": member, "sessions": sessions, "totals": totals},
    )


@app.get("/actividad/{session_id}")
async def actividad_detail(request: Request, session_id: str):
    """One conversation's runs in full: each message, its model, tokens, and the
    exact tool calls (name + params) that ran. 404 if it isn't this member's."""
    import json

    member = await current_member(request)
    if member is None:
        return RedirectResponse("/login", status_code=303)
    session = await db.fetchrow(
        """
        SELECT chat_session_id, title FROM chat_session
        WHERE chat_session_id = $1::uuid AND family_id = $2 AND member_id = $3
        """,
        session_id, member["family_id"], member["member_id"],
    )
    if session is None:
        return RedirectResponse("/actividad", status_code=303)
    rows = await db.fetch(
        """
        SELECT created_ts, input_text, reply_text, model_used, total_tokens,
               tool_calls, error
        FROM agent_run
        WHERE chat_session_id = $1::uuid
        ORDER BY created_ts
        """,
        session_id,
    )
    runs = []
    for r in rows:
        calls = r["tool_calls"]
        if isinstance(calls, str):  # asyncpg returns jsonb as text (no codec set)
            calls = json.loads(calls or "[]")
        runs.append({**dict(r), "tool_calls": calls})
    return templates.TemplateResponse(
        "actividad_detail.html",
        {"request": request, "member": member, "session": session, "runs": runs},
    )


@app.get("/health")
async def health():
    """Liveness + DB check: pool works and the schema applied."""
    one = await db.fetchval("SELECT 1")
    tables = await db.fetchval(
        """
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = 'public'
        """
    )
    return JSONResponse({"status": "ok", "db": one == 1, "public_tables": tables})
