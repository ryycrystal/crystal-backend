import asyncio

from core.stream import stream_logs
from core.sequencer import SEQUENCER
import core.storage as storage
from core import oracle


async def main() -> None:
    storage.init_pool()
    storage.init_db()

    SEQUENCER.set_on_block(storage.record_block_processed)

    SEQUENCER._state.rebuild_from_db()

    last_blk = storage.get_last_processed_block()
    if last_blk is None:
        last_blk = 37709836

    start_blk = last_blk + 1
    print(f"[IDX] starting stream from block {start_blk}", flush=True)

    stream_task = asyncio.create_task(stream_logs(start_blk))

    await asyncio.gather(stream_task)


if __name__ == "__main__":
    asyncio.run(main())
