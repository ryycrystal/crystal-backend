import asyncio
import sys
import uvicorn
from fastapi import FastAPI

from api.api import app as api_app
from core.stream import stream_logs, vault_sampler
from core.sequencer import SEQUENCER

app: FastAPI = api_app

@app.on_event("startup")
async def _boot_streamer() -> None:
    # start_blk = int(sys.argv[1], 0) if len(sys.argv) > 1 else None
    start_blk = None
    asyncio.create_task(stream_logs(start_blk))
    asyncio.create_task(vault_sampler(SEQUENCER._state))

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
