import asyncio
import sys
import uvicorn
from fastapi import FastAPI

from api import app as api_app
from stream import stream_logs

app: FastAPI = api_app

@app.on_event("startup")
async def _boot_streamer() -> None:
    start_blk = int(sys.argv[1], 0) if len(sys.argv) > 1 else None
    # start_blk = 37403294
    asyncio.create_task(stream_logs(start_blk))

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
