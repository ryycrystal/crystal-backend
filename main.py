# main.py
import asyncio
import uvicorn
from fastapi import FastAPI

from api.api import app as api_app
from core.stream import stream_logs, vault_sampler
from core.sequencer import SEQUENCER
import core.storage as storage

app: FastAPI = api_app

async def _periodic_snapshot() -> None:
    while True:
        try:
            storage.save(SEQUENCER._state)
        except Exception as e:
            print(f"[Snapshot] Error: {e!r}")
        await asyncio.sleep(60)

@app.on_event("startup")
async def _boot_streamer() -> None:
    last_blk = storage.load(SEQUENCER._state)

    SEQUENCER.set_on_block(lambda blk: storage.set_last_indexed_block(SEQUENCER._state, blk))

    start_blk = (last_blk + 1) if last_blk is not None else None

    asyncio.create_task(stream_logs(start_blk))
    asyncio.create_task(vault_sampler(SEQUENCER._state))
    asyncio.create_task(_periodic_snapshot())

@app.on_event("shutdown")
async def _persist_on_shutdown() -> None:
    try:
        storage.save(SEQUENCER._state)
    except Exception as e:
        print(f"[Snapshot] Shutdown Error: {e!r}")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
