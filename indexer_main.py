import asyncio

from core.stream import stream_logs
from core.sequencer import SEQUENCER
import core.storage as storage
import backfill

REINDEX = False
REINDEX_FROM_BLOCK = 37709836
REINDEX_BATCH = 100


async def main() -> None:
    storage.init_pool()
    storage.init_db()

    SEQUENCER.set_on_block(storage.record_block_processed)

    SEQUENCER._state.rebuild_from_db()

    if REINDEX:
        start = REINDEX_FROM_BLOCK
        if start == 0:
            min_cached, _ = storage.get_cached_block_range()
            start = min_cached if min_cached else 37709836

        print(f"[IDX] Reindex from block {start}", flush=True)
        last = await backfill.reindex(start, REINDEX_BATCH)
        print(f"[IDX] Reindex complete at block {last}", flush=True)

        start_blk = last + 1
    else:
        last_blk = storage.get_last_processed_block()
        if last_blk is None:
            last_blk = 37709836
        start_blk = last_blk + 1

    print(f"[IDX] Starting from block {start_blk}", flush=True)

    stream_task = asyncio.create_task(stream_logs(start_blk))

    await asyncio.gather(stream_task)

if __name__ == "__main__":
    asyncio.run(main())
