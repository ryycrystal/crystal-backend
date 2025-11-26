import asyncio
import uvicorn
from fastapi import FastAPI

from api.api import app as api_app
from core.stream import stream_logs
from core.sequencer import SEQUENCER
import core.storage as storage
from core import oracle

app: FastAPI = api_app

async def mon_price_poller() -> None:
    while True:
        try:
            price = oracle.fetch_mon_price(None)
            SEQUENCER._state.set_mon_price_usd(price)
        except Exception as e:
            print(f"[Oracle] MON price error: {e!r}")
        
        await asyncio.sleep(5.0)

@app.on_event("startup")
async def _boot_streamer() -> None:
    storage.init_pool()
    storage.init_db()
    
    SEQUENCER._state.rebuild_from_db()
    
    last_blk = storage.get_last_processed_block()
    if last_blk is None:
        last_blk = 37709836
        
    start_blk = last_blk + 1

    asyncio.create_task(stream_logs(start_blk))
    asyncio.create_task(mon_price_poller())
    
@app.on_event("shutdown")
async def _shutdown() -> None:
    storage.close_pool()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
