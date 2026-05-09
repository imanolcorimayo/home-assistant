from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers import attachments, finance, lists, webhook, dashboard

app = FastAPI(title="SovereignBox AI", version="1.0.0")

app.include_router(webhook.router, prefix="/webhook", tags=["webhook"])
app.include_router(finance.router, prefix="/finance", tags=["finance"])
app.include_router(dashboard.router, prefix="/api", tags=["dashboard"])
app.include_router(attachments.router, prefix="/api", tags=["attachments"])
app.include_router(lists.router, prefix="/api", tags=["lists"])


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
