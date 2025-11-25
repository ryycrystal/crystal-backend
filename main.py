import asyncio
import uvicorn
from fastapi import FastAPI

from api.api import app as api_app
from core.stream import stream_logs
from core.sequencer import SEQUENCER
import core.storage as storage

app: FastAPI = api_app

@app.on_event("startup")
async def _boot_streamer() -> None:
    storage.init_pool()
    storage.init_db()
    
    SEQUENCER.set_on_block(storage.record_block_processed)
    
    last_blk = storage.get_last_processed_block()
    if last_blk is None:
        last_blk = 37709836
        
    start_blk = last_blk + 1

    asyncio.create_task(stream_logs(start_blk))
    
@app.on_event("shutdown")
async def _shutdown() -> None:
    storage.close_pool()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
