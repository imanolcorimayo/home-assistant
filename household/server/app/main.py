from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from app.database import engine

app = FastAPI(title="household")


@app.get("/health")
async def health():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"db unreachable: {exc}")
    return {"status": "ok"}
