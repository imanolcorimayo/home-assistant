from fastapi import FastAPI

from app.routers import finance, webhook

app = FastAPI(title="SovereignBox AI", version="1.0.0")

app.include_router(webhook.router, prefix="/webhook", tags=["webhook"])
app.include_router(finance.router, prefix="/finance", tags=["finance"])


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
