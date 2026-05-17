from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.auth import BasicAuthMiddleware
from app.core.config import settings
from app.routers import attachments, events, finance, lists, tasks, webhook, dashboard

app = FastAPI(title="SovereignBox AI", version="1.0.0")

app.add_middleware(
    BasicAuthMiddleware,
    username=settings.basic_auth_user,
    password=settings.basic_auth_pass,
)

app.include_router(webhook.router, prefix="/webhook", tags=["webhook"])
app.include_router(finance.router, prefix="/finance", tags=["finance"])
app.include_router(dashboard.router, prefix="/api", tags=["dashboard"])
app.include_router(attachments.router, prefix="/api", tags=["attachments"])
app.include_router(lists.router, prefix="/api", tags=["lists"])
app.include_router(events.router, prefix="/api", tags=["events"])
app.include_router(tasks.router, prefix="/api", tags=["tasks"])


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
