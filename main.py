import uvicorn
from fastapi import FastAPI

from api.api import app as api_app
import core.storage as storage

app: FastAPI = api_app


@app.on_event("startup")
async def _startup() -> None:
    storage.init_pool()


@app.on_event("shutdown")
async def _shutdown() -> None:
    storage.close_pool()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
