import asyncio

from core.stream import stream_logs
from core.sequencer import SEQUENCER
import core.storage as storage
import backfill
from modules import nadfun

REINDEX = False
REINDEX_FROM_BLOCK = 37709836
REINDEX_BATCH = 100


async def main() -> None:
    storage.init_pool()
    storage.init_db()

    await nadfun.start_metadata_worker(storage)

    if REINDEX:
        start = REINDEX_FROM_BLOCK
        if start == 0:
            min_cached, _ = storage.get_cached_block_range()
            start = min_cached if min_cached else 37709836

        print(f"[IDX] Reindex mode: clearing derived state and reprocessing from block {start}", flush=True)
        last = await backfill.reindex(start, REINDEX_BATCH)
        print(f"[IDX] Reindex complete at block {last}", flush=True)

        start_blk = last + 1
    else:
        print(f"[IDX] Normal mode: rebuilding state from DB...", flush=True)
        SEQUENCER._state.rebuild_from_db()
        print(f"[IDX] Loaded {len(SEQUENCER._state.launchpad_tokens)} tokens, {len(SEQUENCER._state.v3_pools)} pools", flush=True)

        last_blk = storage.get_last_processed_block()
        if last_blk is None:
            last_blk = 37709836
            print(f"[IDX] No last processed block found, starting from genesis {last_blk}", flush=True)
        start_blk = last_blk + 1

    print(f"[IDX] Starting live stream from block {start_blk}", flush=True)

    SEQUENCER.set_next_block(start_blk)

    stream_task = asyncio.create_task(stream_logs(start_blk))

    await asyncio.gather(stream_task)

if __name__ == "__main__":
    asyncio.run(main())
