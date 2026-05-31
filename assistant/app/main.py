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

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import agent, config, db, queries, storage, tools
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
    fid = member["family_id"]
    family = await db.fetchrow("SELECT * FROM family WHERE family_id = $1", fid)
    balances = await queries.account_balances(fid)
    summary = await queries.month_summary(fid)
    recent = await tools.recent_transactions(fid, limit=8)
    budgets = await queries.budgets_vs_spend(fid)
    recurring = await tools.list_recurring(fid)
    pending = [r for r in recurring if not r["paid_this_month"]]
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "member": member, "family": family,
         "balances": balances, "summary": summary, "recent": recent,
         "budgets": budgets, "pending": pending},
    )


@app.get("/login")
async def login(request: Request):
    # Already signed in → straight to home.
    if await current_member(request) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/movimientos")
async def movimientos(request: Request, q: str = "", page: int = 1):
    """Read-only transaction list (search + pagination). Edits go via the chat."""
    member = await current_member(request)
    if member is None:
        return RedirectResponse("/login", status_code=303)
    result = await queries.transactions_page(member["family_id"], q, page)
    return templates.TemplateResponse(
        "movimientos.html",
        {"request": request, "member": member, "q": q, **result},
    )


@app.get("/configuracion")
async def configuracion(request: Request):
    """Read-only view of the family's cuentas / categorías / presupuestos /
    recurrentes. Changes are made by asking the agent for now."""
    member = await current_member(request)
    if member is None:
        return RedirectResponse("/login", status_code=303)
    fid = member["family_id"]
    return templates.TemplateResponse(
        "configuracion.html",
        {"request": request, "member": member,
         "balances": await queries.account_balances(fid),
         "categories": await queries.categories(fid),
         "budgets": await queries.budgets_vs_spend(fid),
         "recurring": await tools.list_recurring(fid)},
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


@app.post("/chat/stream")
async def chat_stream(
    request: Request,
    message: str = Form(""),
    session_id: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    """Streaming version of /chat/message: an SSE feed of the agent's progress
    (tool calls as they run) ending with the reply. Multipart so it can carry
    attachments (receipt photos, audio). Same identity rule — the member comes
    from the session, never the body. Limits are enforced HERE too, not just in
    the client. The client reads this with fetch()+ReadableStream."""
    import json

    member = await current_member(request)
    if member is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    text = (message or "").strip()
    sid_in = (session_id or "").strip() or None

    # Validate + read attachments. Quality-over-quantity caps from config.
    media: list[dict] = []
    n_img = n_aud = 0
    for f in files or []:
        kind = storage.classify(f.content_type or "")
        if kind is None:
            return JSONResponse({"error": f"tipo no permitido: {f.content_type}"}, status_code=400)
        data = await f.read()
        if kind == "image":
            n_img += 1
            if n_img > config.MAX_IMAGES:
                return JSONResponse({"error": f"máximo {config.MAX_IMAGES} imágenes"}, status_code=400)
            if len(data) > config.MAX_IMAGE_BYTES:
                return JSONResponse({"error": "imagen demasiado grande"}, status_code=400)
        else:
            n_aud += 1
            if n_aud > config.MAX_AUDIOS:
                return JSONResponse({"error": f"máximo {config.MAX_AUDIOS} audios"}, status_code=400)
            if len(data) > config.MAX_AUDIO_BYTES:
                return JSONResponse({"error": "audio demasiado largo"}, status_code=400)
        media.append({"kind": kind, "mime": storage.base_mime(f.content_type),
                      "filename": f.filename or "", "data": data})

    if not text and not media:
        return JSONResponse({"error": "empty message"}, status_code=400)

    async def events():
        log = logging.getLogger("chat")
        try:
            async for ev in agent.stream(text, member, sid_in, media):
                yield f"data: {json.dumps(ev)}\n\n"
                # Once the thread id is known, persist images linked to it
                # (audio is sent to the model but not stored — per design).
                if ev.get("type") == "start":
                    for m in media:
                        if m["kind"] == "image":
                            try:
                                await storage.save_image(
                                    member["family_id"], member["member_id"],
                                    ev["session_id"], m["mime"], m["filename"], m["data"])
                            except Exception:  # noqa: BLE001
                                log.exception("save_image failed")
        except Exception:  # noqa: BLE001
            log.exception("agent.stream failed")
            yield 'data: {"type": "error", "message": "Error interno."}\n\n'
        yield 'data: {"type": "end"}\n\n'

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # Disable proxy buffering so events flush immediately (Cloudflare/nginx).
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


@app.get("/manual")
async def manual_marca():
    """Brand manual brought over from pay-trackr (the analogous project) — a
    self-contained reference of the design work, shareable as a clean URL.
    Public on purpose (no login) so it's easy to show."""
    return FileResponse("app/static/manual-marca.html")


@app.get("/media/{media_id}")
async def media_file(request: Request, media_id: str):
    """Serve a kept attachment — family-scoped, so you can only fetch your own
    family's media."""
    member = await current_member(request)
    if member is None:
        return RedirectResponse("/login", status_code=303)
    row = await db.fetchrow(
        """
        SELECT storage_path, mime FROM media
        WHERE media_id = $1::uuid AND family_id = $2 AND deleted_ts IS NULL
        """,
        media_id, member["family_id"],
    )
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(storage.abs_path(row["storage_path"]), media_type=row["mime"])


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
