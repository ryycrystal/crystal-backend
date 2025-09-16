import json
import asyncio
import argparse
import uuid
import websockets

import helpers as h
from sequencer import SEQUENCER

def parse_args():
    parser = argparse.ArgumentParser(description="backfiller")
    parser.add_argument(
        "start_block",
        type=lambda x: int(x, 0),
        help="block to start backfill from (decimal or 0x-prefixed hex)",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=100,
        help="blocks per eth_getLogs query (keep < 100)",
    )
    return parser.parse_args()


async def get_head(ws) -> int:
    rid = str(uuid.uuid4())
    await h.rate_gate()
    await ws.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "eth_blockNumber",
                "params": [],
            }
        )
    )
    resp = await h.ack(ws, rid)
    return int(resp["result"], 16)


async def fetch_logs(ws, frm: int, to: int):
    rid = str(uuid.uuid4())
    await h.rate_gate()
    await ws.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "eth_getLogs",
                "params": [
                    {
                        "fromBlock": hex(frm),
                        "toBlock": hex(to),
                        "address": h.ADDRS,
                        "topics": [h.TOPICS],
                    }
                ],
            }
        )
    )
    resp = await h.ack(ws, rid)
    return resp["result"]


async def backfill(start_block: int, batch: int) -> int:
    async with websockets.connect(h.WS_URL) as ws:
        head_snapshot = await get_head(ws)
        print(f"[Backfill] init chain head = {head_snapshot}")

        last_processed = start_block - 1

        for chunk_start in range(start_block, head_snapshot + 1, batch):
            chunk_end = min(chunk_start + batch - 1, head_snapshot)

            while True:
                current_head = await get_head(ws)
                if current_head >= chunk_end:
                    break
                await asyncio.sleep(0.5)

            logs = await fetch_logs(ws, chunk_start, chunk_end)

            counts = {v: 0 for v in h.EVENT_SIGS.values()}
            for raw in logs:
                tag = h.EVENT_SIGS.get(raw["topics"][0].lower())
                if tag:
                    counts[tag] += 1
                SEQUENCER.add_log(raw)

            for blk in range(chunk_start, chunk_end + 1):
                SEQUENCER.note_block(blk)

            last_processed = chunk_end

        print(f"[Backfill] complete, last processed = {last_processed}")
        return last_processed


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(backfill(args.start_block, args.batch))
