import json, asyncio, time, uuid, websockets
from collections import deque

from core import chain as h
from core.sequencer import SEQUENCER

import backfill

HEAD_TIMEOUT = 60.0
BACKFILL_BATCH = 100

missing_blocks: deque[int] = deque() # queue of blocks that need backfilling
missing_set: set[int] = set() # so we dont backfill the same block twice

# enqueue a block into missing queue
def _add_missing(blk: int):
    if blk not in missing_set:
        missing_blocks.append(blk)
        missing_set.add(blk)

# background worker that replays logs for missing block ranges and feeds them to the sequencer
async def _gap_worker(event_counts):
    while True:
        if not missing_blocks:
            await asyncio.sleep(0.5)
            continue

        blk_start = missing_blocks.popleft()
        blk_end = blk_start
        missing_set.discard(blk_start)

        while missing_blocks and missing_blocks[0] == blk_end + 1 and (blk_end - blk_start + 1) < BACKFILL_BATCH:
            blk_end += 1
            missing_set.discard(missing_blocks.popleft())

        try:
            # actual fetching
            async with websockets.connect(h.WS_URL, max_size=None) as gap_ws:
                rid = str(uuid.uuid4())
                await h.rate_gate()
                await gap_ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "eth_getLogs",
                    "params": [{
                        "fromBlock": hex(blk_start),
                        "toBlock": hex(blk_end),
                        "topics": [h.TOPICS],
                    }],
                }))
                resp = await h.ack(gap_ws, rid)

            # handing logs to sequencer
            for log in resp.get("result", []):                
                topics = log.get("topics") or []
                if not topics:
                    continue
                
                tag = h.EVENT_SIGS.get(topics[0].lower())
                if not tag:
                    continue

                addr = log.get("address", "").lower()

                if tag in ("NFC", "NFB", "NFS", "NFSYNC", "NFT", "MG"):
                    if addr != h.CONTRACTS["NADFUN"].lower():
                        continue

                elif tag == "V3SWAP":
                    pass

                elif tag == "TF":
                    # Only include transfers of tokens we're tracking
                    if addr not in SEQUENCER._state.launchpad_tokens and addr not in SEQUENCER._state.token_to_v3_pool:
                        continue

                else:
                    continue

                if tag in event_counts:
                    event_counts[tag] += 1

                SEQUENCER.add_log(log)

            for blk in range(blk_start, blk_end + 1):
                SEQUENCER.note_block(blk)
                
        except TimeoutError:
            print(f"[Backfill] WS connect timeout for range {blk_start}-{blk_end}, retrying", flush=True)
            for blk in range(blk_start, blk_end + 1):
                _add_missing(blk)
            await asyncio.sleep(5.0)

        except RuntimeError as e:
            err = e.args[0]
            err_code = err.get("error", {}).get("code") if isinstance(err, dict) else None

            if err_code == -32007:
                print("[RL] Hit provider cap, retrying")
                for blk in range(blk_start, blk_end + 1):
                    _add_missing(blk)
                await asyncio.sleep(1.0)
            elif err_code == -32602:
                print(f"[Gap] Block range {blk_start}-{blk_end} too large, splitting")
                for blk in range(blk_start, blk_end + 1):
                    _add_missing(blk)
                await asyncio.sleep(0.5)
            else:
                raise
            
        except Exception as e:
            print(f"[Backfill] Fatal Error for range {blk_start}-{blk_end}: {e!r}, retrying", flush=True)
            for blk in range(blk_start, blk_end + 1):
                _add_missing(blk)
            await asyncio.sleep(5.0)


# one websocket session to subscribe to heads + logs, feed sequencer, handle head gaps and watchdog
async def _stream_once(prev_last_head: int | None) -> int | None:
    connect_kwargs = dict(
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
        max_queue=None,
        open_timeout=30,
        max_size=None,
    )
    async with websockets.connect(h.WS_URL, **connect_kwargs) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}))
        heads_sub = (await h.ack(ws, 1))["result"]

        await ws.send(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "eth_subscribe",
                "params": [
                    "logs",
                    {
                        "topics": [h.TOPICS]
                    },
                ],
            })
        )
        logs_sub = (await h.ack(ws, 2))["result"]

        event_counts = {v: 0 for v in h.EVENT_SIGS.values()}
        asyncio.create_task(_gap_worker(event_counts))

        last_head_ts = time.monotonic()
        last_head_num = prev_last_head
        first_head_seen = False

        # used to find a stalled head stream (basically if the ws dies or wifi or sm dies) and triggers backfill
        async def watchdog():
            nonlocal last_head_ts, last_head_num
            while True:
                await asyncio.sleep(0.5)
                if time.monotonic() - last_head_ts > HEAD_TIMEOUT:
                    print("[WS] No newHeads, forcing reconnect and backfill", flush=True)
                    if last_head_num is not None:
                        try:
                            await backfill.backfill(last_head_num, BACKFILL_BATCH)
                        except TimeoutError as e:
                            print(f"[WS] Backfill WS timeout {e!r}, skipping backfill", flush=True)
                        except Exception as e:
                            print(f"[WS] Backfill Failed {e!r}", flush=True)
                    await ws.close()
                    return

        asyncio.create_task(watchdog())

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") != "eth_subscription":
                continue

            sid = msg["params"]["subscription"]
            res = msg["params"]["result"]

            # head stream update
            if sid == heads_sub:
                blk = int(res["number"], 16)

                if last_head_num is not None:
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

            # sequencer feeding
            if sid == logs_sub:
                topics = res.get("topics") or []
                if not topics:
                    continue

                tag = h.EVENT_SIGS.get(topics[0].lower())
                if not tag:
                    continue

                addr = res.get("address", "").lower()

                if tag in ("NFC", "NFB", "NFS", "NFSYNC", "NFT", "MG"):
                    if addr != h.CONTRACTS["NADFUN"].lower():
                        continue

                elif tag == "V3SWAP":
                    pass

                elif tag == "TF":
                    if addr not in SEQUENCER._state.launchpad_tokens and addr not in SEQUENCER._state.token_to_v3_pool:
                        continue

                else:
                    continue

                if tag in event_counts:
                    event_counts[tag] += 1

                SEQUENCER.add_log(res)

        return last_head_num

# main loop w/optional initial backfill or live stream
async def stream_logs(start_block: int | None = None):
    last_seen = None
    delay = 0.5

    if start_block is not None:
        try:
            last_seen = await backfill.backfill(start_block, BACKFILL_BATCH)
            print(f"[WS] Backfill from {start_block} to {last_seen}", flush=True)
        except Exception as e:
            print(f"[WS] Backfill failed {e!r}", flush=True)
            last_seen = start_block - 1 if start_block > 0 else None

    while True:
        try:
            last_seen = await _stream_once(last_seen)
            delay = 0.5
        except Exception as e:
            print(f"[WS] dropped {e!r}", flush=True)

        await asyncio.sleep(delay)
        delay = min(delay * 2, 10) + (0.0 if delay >= 10 else 0.25)