"""household web — the family-facing informational interface.

Standalone FastAPI app (its own container, its own Cloudflare hostname).
Talks straight to Postgres with raw SQL via app.db — no shared ORM with
the server. Server-rendered with Jinja2 templates.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db
from app.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open the pool before serving, close it on shutdown.
    await db.open_pool()
    yield
    await db.close_pool()


app = FastAPI(title="household-web", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)
