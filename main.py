import asyncio
import uvicorn
from fastapi import FastAPI

from api.api import app as api_app
from core.stream import stream_logs

app: FastAPI = api_app

@app.on_event("startup")
async def _boot_streamer() -> None:
    last_blk = 51379076
    start_blk = (last_blk + 1) if last_blk is not None else None

    asyncio.create_task(stream_logs(start_blk))

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
