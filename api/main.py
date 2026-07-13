from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Yuvi Bot v2 API", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api"}

