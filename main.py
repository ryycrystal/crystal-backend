import uvicorn
from fastapi import FastAPI

import core.storage as storage
from api import x_track
from api.api import app as api_app

app: FastAPI = api_app


@app.on_event("startup")
async def _startup() -> None:
    storage.init_pool()
    x_track.start_workers()


@app.on_event("shutdown")
async def _shutdown() -> None:
    x_track.stop_workers()
    storage.close_pool()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
