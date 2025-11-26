import json
import asyncio
import argparse
import uuid
import websockets
import urllib.request

from core import chain as h
from core.sequencer import SEQUENCER
from core import oracle
from state import RPC_HTTP

# parse cli arguments for the backfiller process
# returns an argparse namespace with start_block and batch size
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

# batch json rpc call over plain http to RPC_HTTP
# used for header seeding
def _rpc_batch_http(calls: list[dict]) -> list[dict]:
    payload = json.dumps(calls).encode()
    req = urllib.request.Request(RPC_HTTP, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())

# gets current chainhead block number
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

# fetch logs for a block range [frm, to] over websocket using eth_getLogs
# filters by the tracked addresses and topics from core.chain
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
                        "topics": [h.TOPICS],
                    }
                ],
            }
        )
    )
    resp = await h.ack(ws, rid)
    return resp["result"]

# main backfill loop
# walks from start_block up to current head in batches
# seeds headers and replaying logs into the sequencer in order
# returns the last processed block.     
async def backfill(start_block: int, batch: int) -> int:
    async with websockets.connect(h.WS_URL, max_size=None) as ws:
        head_snapshot = await get_head(ws)
        print(f"[Backfill] CH @ Init = {head_snapshot}")

        last_processed = start_block - 1

        for chunk_start in range(start_block, head_snapshot + 1, batch):
            chunk_end = min(chunk_start + batch - 1, head_snapshot)

            while True:
                current_head = await get_head(ws)
                if current_head >= chunk_end:
                    break
                await asyncio.sleep(0.5)
                
            try:
                price = oracle.fetch_mon_price(chunk_start)
                SEQUENCER._state.set_mon_price_usd(price)
                print(f"[Backfill] Batch {chunk_start}-{chunk_end} MON={price}")
            except Exception as e:
                print(f"[Backfill] Oracle Error: {e!r}")

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

        print(f"[Backfill] Complete, LP = {last_processed}")
        return last_processed