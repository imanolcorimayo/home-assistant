from fastapi import FastAPI

app = FastAPI(title="SovereignBox AI", version="1.0.0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
