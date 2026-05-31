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
    "/actividad":     ("Actividad", "Tus sesiones de chat, uso de tokens y herramientas llegan pronto."),
}


@app.get("/movimientos")
@app.get("/configuracion")
@app.get("/actividad")
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
    session inside agent.handle — never from the request body."""
    member = await current_member(request)
    if member is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    body = await request.json()
    text = (body.get("message") or "").strip()
    if not text:
        return JSONResponse({"error": "empty message"}, status_code=400)
    try:
        reply = await agent.handle(text, member)
    except Exception:  # noqa: BLE001
        logging.getLogger("chat").exception("agent.handle failed")
        return JSONResponse(
            {"reply": "Hubo un problema procesando el mensaje. Probá de nuevo."},
            status_code=200,
        )
    return JSONResponse({"reply": reply})


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
