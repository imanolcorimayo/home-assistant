"""assistant — the unified multi-tenant family financial assistant.

Single FastAPI app (collapses household's api + web + chat split). Multi-tenant:
every row is scoped by family_id. Talks straight to Postgres with raw SQL via
app.db — no ORM. Server-rendered with Jinja2 (pages land in later issues).

This is the #25 scaffold: it boots, opens the DB pool, and exposes /health so
we can confirm the schema applied. Login (#26) and the chat surface (#27) build
on top of this skeleton.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open the pool before serving, close it on shutdown.
    await db.open_pool()
    yield
    await db.close_pool()


app = FastAPI(title="assistant", lifespan=lifespan)


@app.get("/health")
async def health():
    """Liveness + DB check. Confirms the pool works and the schema applied by
    counting the tables we expect. Useful right after first boot."""
    one = await db.fetchval("SELECT 1")
    tables = await db.fetchval(
        """
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = 'public'
        """
    )
    return JSONResponse(
        {"status": "ok", "db": one == 1, "public_tables": tables}
    )
