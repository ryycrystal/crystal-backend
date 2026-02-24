import json, asyncio, time, uuid, websockets
from collections import deque

from core import chain as h
from core.sequencer import SEQUENCER

import backfill

HEAD_TIMEOUT = 60.0
BACKFILL_BATCH = 100

missing_blocks: deque[int] = deque()
missing_set: set[int] = set()


def _add_missing(blk: int):
    if blk not in missing_set:
        missing_blocks.append(blk)
        missing_set.add(blk)


def _clear_stale_blocks(current_head: int):
    global missing_blocks, missing_set
    seq_next = SEQUENCER._next_block
    if current_head is None or seq_next is None:
        return
    new_blocks = deque()
    new_set = set()
    for blk in missing_blocks:
        if blk >= seq_next:
            new_blocks.append(blk)
            new_set.add(blk)
    cleared = len(missing_blocks) - len(new_blocks)
    if cleared > 0:
        print(f"[Gap] Cleared {cleared} already-processed queued blocks (< {seq_next})", flush=True)
    missing_blocks = new_blocks
    missing_set = new_set


async def _gap_worker(event_counts, should_exit_flag: list):
    while not should_exit_flag[0]:
        if not missing_blocks:
            await asyncio.sleep(0.5)
            continue

        blk_start = missing_blocks.popleft()
        blk_end = blk_start
        missing_set.discard(blk_start)

        while missing_blocks and missing_blocks[0] == blk_end + 1 and (blk_end - blk_start + 1) < BACKFILL_BATCH:
            blk_end += 1
            missing_set.discard(missing_blocks.popleft())

        seq_next = SEQUENCER._next_block
        if seq_next is not None and blk_end < seq_next:
            print(f"[Gap] Skipping already-processed range {blk_start}-{blk_end}, sequencer at {seq_next}", flush=True)
            continue

        try:
            async with websockets.connect(h.WS_URL, max_size=None, open_timeout=15, close_timeout=5) as gap_ws:
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
                resp = await asyncio.wait_for(h.ack(gap_ws, rid), timeout=30.0)

            by_block: dict[int, list[dict]] = {}
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
                    pass

                else:
                    continue

                if tag in event_counts:
                    event_counts[tag] += 1

                blk_hex = log.get("blockNumber")
                blk_num = int(blk_hex, 16) if isinstance(blk_hex, str) else int(blk_hex or 0)
                by_block.setdefault(blk_num, []).append(log)

            if by_block:
                await backfill.ensure_block_timestamps(by_block)
                for blk in sorted(by_block):
                    for log in by_block[blk]:
                        SEQUENCER.add_log(log)

            for blk in range(blk_start, blk_end + 1):
                blk_logs = by_block.get(blk, [])
                blk_ts = None
                if blk_logs:
                    ts_raw = blk_logs[0].get("blockTimestamp")
                    if isinstance(ts_raw, str):
                        blk_ts = int(ts_raw, 16)
                    elif ts_raw is not None:
                        blk_ts = int(ts_raw)
                SEQUENCER.note_block(blk, block_timestamp=blk_ts)

        except asyncio.CancelledError:
            print(f"[Gap] Cancelled while processing {blk_start}-{blk_end}, re-queuing", flush=True)
            for blk in range(blk_start, blk_end + 1):
                _add_missing(blk)
            raise

        except (TimeoutError, asyncio.TimeoutError):
            print(f"[Backfill] WS timeout for range {blk_start}-{blk_end}, retrying", flush=True)
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
        prefetched_msgs: list[dict] = []
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}))
        heads_sub = (await h.ack(ws, 1, prefetched_msgs))["result"]

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
        logs_sub = (await h.ack(ws, 2, prefetched_msgs))["result"]

        event_counts = {v: 0 for v in h.EVENT_SIGS.values()}
        should_exit = [False]

        gap_task = asyncio.create_task(_gap_worker(event_counts, should_exit))

        last_head_ts = time.monotonic()
        last_head_num = prev_last_head
        last_head_block_ts: int | None = None
        first_head_seen = False

        async def watchdog():
            nonlocal last_head_ts, last_head_num
            while not should_exit[0]:
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
                    should_exit[0] = True
                    try:
                        await asyncio.wait_for(ws.close(), timeout=5.0)
                    except asyncio.TimeoutError:
                        print("[WS] Close timed out, forcing", flush=True)
                    return

        watchdog_task = asyncio.create_task(watchdog())

        try:
            while True:
                if should_exit[0]:
                    break

                if prefetched_msgs:
                    msg = prefetched_msgs.pop(0)
                else:
                    msg = json.loads(await ws.recv())
                if msg.get("method") != "eth_subscription":
                    continue

                sid = msg["params"]["subscription"]
                res = msg["params"]["result"]

                if sid == heads_sub:
                    blk = int(res["number"], 16)
                    ts_hex = res.get("timestamp")
                    blk_ts = int(ts_hex, 16) if isinstance(ts_hex, str) else (int(ts_hex) if ts_hex is not None else None)

                    if last_head_num is not None:
                        for key in event_counts:
                            event_counts[key] = 0

                    if not first_head_seen:
                        first_head_seen = True
                        _clear_stale_blocks(blk)
                        if prev_last_head is not None and blk > prev_last_head + 1:
                            for m in range(prev_last_head + 1, blk):
                                _add_missing(m)

                    if last_head_num is not None and blk > last_head_num + 1:
                        for m in range(last_head_num + 1, blk):
                            _add_missing(m)

                    if last_head_num is not None:
                        note_ts = last_head_block_ts
                        if note_ts is None and SEQUENCER._logs_by_block.get(last_head_num):
                            try:
                                note_ts = await backfill.get_block_timestamp_http(last_head_num)
                            except Exception as e:
                                print(f"[WS] Failed to fetch timestamp for block {last_head_num}: {e!r}", flush=True)
                        SEQUENCER.note_block(last_head_num, block_timestamp=note_ts)

                    last_head_ts = time.monotonic()
                    last_head_num = blk
                    last_head_block_ts = blk_ts
                    continue

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
                        pass

                    else:
                        continue

                    if tag in event_counts:
                        event_counts[tag] += 1

                    SEQUENCER.add_log(res)

        finally:
            should_exit[0] = True
            gap_task.cancel()
            watchdog_task.cancel()

            for task, name in [(gap_task, "gap_worker"), (watchdog_task, "watchdog")]:
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    print(f"[WS] {name} did not exit cleanly", flush=True)
                except Exception as e:
                    print(f"[WS] {name} error on exit: {e!r}", flush=True)

        return last_head_num


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
