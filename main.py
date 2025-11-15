import asyncio
import uvicorn
from fastapi import FastAPI

from api.api import app as api_app
from core.stream import stream_logs
from core.sequencer import SEQUENCER
from core.stream import vault_sampler
from state import State, SNAPSHOT_FILE

app: FastAPI = api_app


async def periodic_snapshot() -> None:
    while True:
        try:
            if getattr(SEQUENCER, "_state", None) is not None:
                SEQUENCER._state.save_to_file(SNAPSHOT_FILE)
        except Exception as e:
            print("snapshot save failed", e)
        await asyncio.sleep(30)


@app.on_event("startup")
async def _boot_streamer() -> None:
    snap = State.load_from_file(SNAPSHOT_FILE)
    if snap is not None:
        SEQUENCER._state = snap
        last_blk = snap.last_processed_block or 49389714
        print(f"loaded snapshot at block {last_blk}")
    else:
        last_blk = 49389714
        print("no snapshot found, starting from hardcoded block")

    start_blk = (last_blk + 1) if last_blk is not None else None

    asyncio.create_task(stream_logs(start_blk))
    asyncio.create_task(vault_sampler(SEQUENCER._state))
    asyncio.create_task(periodic_snapshot())


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
