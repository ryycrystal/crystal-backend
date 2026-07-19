import uvicorn
from fastapi import FastAPI

import core.storage as storage
from api.api import app as api_app

app: FastAPI = api_app


# open the db pool when the api boots
@app.on_event("startup")
async def _startup() -> None:
    storage.init_pool()


# close the db pool on shutdown
@app.on_event("shutdown")
async def _shutdown() -> None:
    storage.close_pool()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
