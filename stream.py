import json, asyncio, time, uuid, sys, websockets
from collections import deque

import helpers as h
import backfill

from sequencer import SEQUENCER

HEAD_TIMEOUT = 5.0
BACKFILL_BATCH = 100

missing_blocks: deque[int] = deque()
missing_set: set[int] = set()


def _add_missing(blk: int):
    if blk not in missing_set:
        missing_blocks.append(blk)
        missing_set.add(blk)


async def _gap_worker(event_counts):
    while True:
        if missing_blocks:
            blk = missing_blocks.popleft()
            missing_set.discard(blk)

            async with websockets.connect(h.WS_URL) as gap_ws:
                rid = str(uuid.uuid4())
                await gap_ws.send(
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "method": "eth_getLogs",
                        "params": [{
                            "fromBlock": hex(blk),
                            "toBlock": hex(blk),
                            "address": h.ADDRS,
                            "topics": [h.TOPICS],
                        }],
                    })
                )
                resp = await h.ack(gap_ws, rid)

                for log in resp.get("result", []):
                    tag = h.EVENT_SIGS.get(log["topics"][0].lower())
                    if tag:
                        SEQUENCER.add_log(log)

                SEQUENCER.note_block(blk)

            # print(f"[Backfill] done block {blk}, counts: {{}}".format(
            #     {k: event_counts[k] for k in event_counts}
            # ))
        else:
            await asyncio.sleep(0.5)


async def _stream_once(prev_last_head: int | None) -> int | None:
    async with websockets.connect(h.WS_URL) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}))
        heads_sub = (await h.ack(ws, 1))["result"]

        await ws.send(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "eth_subscribe",
                "params": [
                    "logs",
                    {"address": h.ADDRS, "topics": [h.TOPICS]},
                ],
            })
        )
        logs_sub = (await h.ack(ws, 2))["result"]

        event_counts = {v: 0 for v in h.EVENT_SIGS.values()}
        asyncio.create_task(_gap_worker(event_counts))

        last_head_ts = time.monotonic()
        last_head_num = prev_last_head
        first_head_seen = False

        async def watchdog():
            nonlocal last_head_ts, last_head_num
            while True:
                await asyncio.sleep(0.5)
                if time.monotonic() - last_head_ts > HEAD_TIMEOUT:
                    print(f"[Error] newHeads dropped, starting backfill")
                    if last_head_num is not None:
                        await backfill.backfill(last_head_num + 1, BACKFILL_BATCH)
                    await ws.close()
                    break

        asyncio.create_task(watchdog())

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") != "eth_subscription":
                continue

            sid = msg["params"]["subscription"]
            res = msg["params"]["result"]

            if sid == heads_sub:
                blk = int(res["number"], 16)

                if last_head_num is not None:
                    # counts_snapshot = event_counts.copy()
                    # print(
                    #     f"[WS] {last_head_num}: "
                    #     f"OF {counts_snapshot['OF']}  OU {counts_snapshot['OU']}  UU {counts_snapshot['UU']}  RA {counts_snapshot['RA']}"
                    # )

                    for key in event_counts:
                        event_counts[key] = 0

                if not first_head_seen:
                    first_head_seen = True
                    if prev_last_head is not None and blk > prev_last_head + 1:
                        for m in range(prev_last_head + 1, blk):
                            _add_missing(m)

                if last_head_num is not None and blk > last_head_num + 1:
                    for m in range(last_head_num + 1, blk):
                        _add_missing(m)

                if last_head_num is not None:
                    SEQUENCER.note_block(last_head_num)

                last_head_ts = time.monotonic()
                last_head_num = blk
                continue

            if sid == logs_sub:
                tag = h.EVENT_SIGS.get(res["topics"][0].lower())
                if tag:
                    event_counts[tag] += 1
                
                SEQUENCER.add_log(res)

        return last_head_num


async def stream_logs(start_block: int | None = None):
    last_seen: int | None = None

    if start_block is not None:
        print(f"[Startup] backfilling {start_block} → head")
        last_seen = await backfill.backfill(start_block, BACKFILL_BATCH)

    while True:
        try:
            last_seen = await _stream_once(last_seen)
        except Exception as e:
            print(f"[Error] {e!r} stream dropped")
            await asyncio.sleep(0.5)


if __name__ == "__main__":
    blk = int(sys.argv[1], 0) if len(sys.argv) > 1 else None
    asyncio.run(stream_logs(blk))
