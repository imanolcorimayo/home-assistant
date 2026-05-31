"""assistant — the unified multi-tenant family financial assistant.

Single FastAPI app (collapses household's api + web + chat split). Multi-tenant:
every row is scoped by family_id, taken from the logged-in member's session.
Raw SQL via app.db — no ORM. Server-rendered with Jinja2.

#25 scaffold = boots + /health. #26 = Google login + session + family bootstrap
(this file wires the session, the auth router, and the logged-in landing page).
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import config, db
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

templates = Jinja2Templates(directory="app/templates")


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
