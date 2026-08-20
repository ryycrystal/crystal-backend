import asyncio
import json
import os
import time
import uuid
from collections import deque

import websockets

import backfill
import core.storage as storage
from core import chain as h
from core.sequencer import SEQUENCER

HEAD_TIMEOUT = 60.0
# replay is latency bound rather than cpu bound, so the chunk size mostly decides
# how much fixed per chunk round trip cost gets amortised
BACKFILL_BATCH = int(os.getenv("BACKFILL_BATCH", "100"))
VAULT_SAMPLER_INTERVAL = 30
VAULT_SAMPLER_MULTICALL_CHUNK = 200
MULTICALL3_ADDR = "0xca11bde05977b3631167028862be2a173976ca11"
GET_BALANCES_CALLDATA = bytes.fromhex("00113e08")
# erc20 totalSupply(), piggybacked on the same multicall so the event-replay share
# counter is reconciled against the chain on every sweep instead of drifting silently
TOTAL_SUPPLY_CALLDATA = bytes.fromhex("18160ddd")
MULTICALL3_AGGREGATE3_SELECTOR = bytes.fromhex("82ad56cb")

missing_blocks: deque[int] = deque()
missing_set: set[int] = set()


# read one uint256 at a byte offset
def _u256_at(buf: bytes, off: int) -> int:
    if off < 0 or (off + 32) > len(buf):
        return 0
    return int.from_bytes(buf[off : off + 32], "big")


# abi encode a uint256
def _abi_u256(n: int) -> bytes:
    return int(n).to_bytes(32, "big")


# abi encode an address
def _abi_addr(addr: str) -> bytes:
    h = str(addr or "").lower().replace("0x", "")[-40:].rjust(40, "0")
    return (b"\x00" * 12) + bytes.fromhex(h)


# abi encode a bool
def _abi_bool(v: bool) -> bytes:
    return _abi_u256(1 if v else 0)


# abi encode a dynamic bytes argument
def _abi_bytes(data: bytes) -> bytes:
    d = data or b""
    pad = (-len(d)) % 32
    return _abi_u256(len(d)) + d + (b"\x00" * pad)


# build multicall3 aggregate3 calldata for a batch of calls
def _encode_multicall3_aggregate3(calls: list[tuple[str, bytes]]) -> str:
    elems = []
    for target, calldata in calls:
        body = _abi_addr(target) + _abi_bool(False) + _abi_u256(96) + _abi_bytes(calldata)
        elems.append(body)
    n = len(elems)
    arr_heads_size = 32 * n
    cur = arr_heads_size
    arr_offsets = []
    for e in elems:
        arr_offsets.append(cur)
        cur += len(e)
    arr = _abi_u256(n) + b"".join(_abi_u256(o) for o in arr_offsets) + b"".join(elems)
    payload = MULTICALL3_AGGREGATE3_SELECTOR + _abi_u256(32) + arr
    return "0x" + payload.hex()


# decode multicall3 aggregate3 output into success and return bytes
def _decode_multicall3_aggregate3_result(data_hex: str) -> list[tuple[bool, bytes]]:
    if not isinstance(data_hex, str) or not data_hex.startswith("0x"):
        return []
    try:
        buf = bytes.fromhex(data_hex[2:])
    except Exception:
        return []
    top_off = _u256_at(buf, 0)
    base = top_off
    if (base + 32) > len(buf):
        return []
    n = _u256_at(buf, base)
    tuple_base = base + 32
    out = []
    for i in range(n):
        off = _u256_at(buf, tuple_base + (32 * i))
        elem = tuple_base + off
        success = bool(_u256_at(buf, elem))
        bytes_off = _u256_at(buf, elem + 32)
        bpos = elem + bytes_off
        blen = _u256_at(buf, bpos)
        bstart = bpos + 32
        bend = bstart + blen
        if bstart > len(buf):
            out.append((success, b""))
            continue
        out.append((success, buf[bstart : min(bend, len(buf))]))
    return out


# decode a vault getbalances return into quote and base
def _decode_vault_get_balances_return(ret: bytes) -> tuple[int, int] | None:
    if not isinstance(ret, (bytes, bytearray)):
        return None
    quote_bal = _u256_at(ret, 0)
    base_bal = _u256_at(ret, 32)
    return (quote_bal, base_bal)


# sample vault balances one rpc call at a time
async def _sample_vaults_serial(state, vaults: list[str], blk_hex: str, blk_num: int, ts: int):
    for vaddr in vaults:
        try:
            call_resp = await backfill.http_jsonrpc(
                "eth_call",
                [{"to": vaddr, "data": "0x00113e08"}, blk_hex],
            )
            ret = call_resp.get("result")
            if not isinstance(ret, str) or not ret.startswith("0x"):
                continue
            raw = bytes.fromhex(ret[2:])
            decoded = _decode_vault_get_balances_return(raw)
            if not decoded:
                continue
            quote_bal, base_bal = decoded
            state.record_vault_balance_sample(vaddr, blk_num, ts, quote_bal, base_bal)
        except Exception:
            continue


# sample every vault balance in one multicall, falling back to serial
async def _sample_vaults_multicall(state, vaults: list[str], blk_hex: str, blk_num: int, ts: int):
    for i in range(0, len(vaults), VAULT_SAMPLER_MULTICALL_CHUNK):
        chunk = vaults[i : i + VAULT_SAMPLER_MULTICALL_CHUNK]
        try:
            calls = []
            for v in chunk:
                calls.append((v, GET_BALANCES_CALLDATA))
                calls.append((v, TOTAL_SUPPLY_CALLDATA))
            data = _encode_multicall3_aggregate3(calls)
            call_resp = await backfill.http_jsonrpc(
                "eth_call",
                [{"to": MULTICALL3_ADDR, "data": data}, blk_hex],
            )
            ret = call_resp.get("result")
            results = _decode_multicall3_aggregate3_result(ret) if isinstance(ret, str) else []
            if len(results) != 2 * len(chunk):
                raise ValueError(f"multicall result length mismatch {len(results)} != {2 * len(chunk)}")
            for j, vaddr in enumerate(chunk):
                supply = None
                ok_ts, raw_ts = results[2 * j + 1]
                if ok_ts and isinstance(raw_ts, (bytes, bytearray)) and len(raw_ts) >= 32:
                    supply = int.from_bytes(raw_ts[:32], "big")
                    state.reconcile_vault_shares(vaddr, supply)
                ok, raw = results[2 * j]
                if ok:
                    decoded = _decode_vault_get_balances_return(raw)
                    if decoded:
                        quote_bal, base_bal = decoded
                        state.record_vault_balance_sample(vaddr, blk_num, ts, quote_bal, base_bal, shares=supply)
        except Exception as e:
            print(f"[SAMPLER] Multicall fallback for chunk {i}-{i + len(chunk) - 1}: {e!r}", flush=True)
            await _sample_vaults_serial(state, chunk, blk_hex, blk_num, ts)


# record a block whose logs still need fetching
def _add_missing(blk: int):
    if blk not in missing_set:
        missing_blocks.append(blk)
        missing_set.add(blk)


# drop missing block entries that are older than the retention window
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


# fetch and queue every indexed log for one block
async def _fetch_and_add_indexed_logs_for_block(blk: int, event_counts: dict) -> bool:
    try:
        logs = await backfill.fetch_logs_http(blk, blk)
    except Exception as e:
        print(f"[WS] Failed to fetch logs for block {blk}: {e!r}", flush=True)
        _add_missing(blk)
        return False

    try:
        storage.write_block_logs(blk, logs)
    except Exception as e:
        print(f"[WS] Failed to cache logs for block {blk}: {e!r}", flush=True)

    by_block: dict[int, list[dict]] = {}
    for log in logs:
        topics = log.get("topics") or []
        if not topics:
            continue

        tag = h.EVENT_SIGS.get(topics[0].lower())
        if not tag:
            continue

        addr = log.get("address", "").lower()
        h.register_dynamic_addresses_from_log(log)
        if not h.accepts_log_for_indexing(tag, addr):
            continue

        if tag in event_counts:
            event_counts[tag] += 1

        blk_hex = log.get("blockNumber")
        blk_num = int(blk_hex, 16) if isinstance(blk_hex, str) else int(blk_hex or 0)
        if blk_num != blk:
            continue
        by_block.setdefault(blk_num, []).append(log)

    if by_block:
        await backfill.ensure_block_timestamps(by_block)
        for log in by_block.get(blk, []):
            SEQUENCER.add_log(log)

    return True


# background loop that backfills blocks the stream missed
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
                await gap_ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "method": "eth_getLogs",
                            "params": [
                                {
                                    "fromBlock": hex(blk_start),
                                    "toBlock": hex(blk_end),
                                    "topics": [h.TOPICS],
                                }
                            ],
                        }
                    )
                )
                resp = await asyncio.wait_for(h.ack(gap_ws, rid), timeout=30.0)

            raw_by_block: dict[int, list[dict]] = {}
            for log in resp.get("result", []):
                blk_hex = log.get("blockNumber")
                blk_num = int(blk_hex, 16) if isinstance(blk_hex, str) else int(blk_hex or 0)
                if blk_start <= blk_num <= blk_end:
                    raw_by_block.setdefault(blk_num, []).append(log)

            try:
                with storage.db_cursor() as cur:
                    storage.write_block_logs_batch(
                        {blk: raw_by_block.get(blk, []) for blk in range(blk_start, blk_end + 1)}, cur
                    )
            except Exception as e:
                print(f"[Gap] Failed to cache logs for {blk_start}-{blk_end}: {e!r}", flush=True)

            by_block: dict[int, list[dict]] = {}
            for log in resp.get("result", []):
                topics = log.get("topics") or []
                if not topics:
                    continue

                tag = h.EVENT_SIGS.get(topics[0].lower())
                if not tag:
                    continue

                addr = log.get("address", "").lower()

                h.register_dynamic_addresses_from_log(log)
                if not h.accepts_log_for_indexing(tag, addr):
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

        except TimeoutError:
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


# one websocket session, subscribing to heads and draining logs until it drops
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
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "eth_subscribe",
                    "params": [
                        "logs",
                        {"topics": [h.TOPICS]},
                    ],
                }
            )
        )
        logs_sub = (await h.ack(ws, 2, prefetched_msgs))["result"]

        event_counts = {v: 0 for v in h.EVENT_SIGS.values()}
        should_exit = [False]

        gap_task = asyncio.create_task(_gap_worker(event_counts, should_exit))

        last_head_ts = time.monotonic()
        last_head_num = prev_last_head
        last_head_block_ts: int | None = None
        first_head_seen = False

        # force a reconnect when no new head arrives within the timeout
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
                    except TimeoutError:
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
                    blk_ts = (
                        int(ts_hex, 16) if isinstance(ts_hex, str) else (int(ts_hex) if ts_hex is not None else None)
                    )

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
                        fetched = await _fetch_and_add_indexed_logs_for_block(last_head_num, event_counts)
                        if not fetched:
                            last_head_ts = time.monotonic()
                            if last_head_num is not None and blk + 64 < last_head_num:
                                raise RuntimeError(
                                    f"[FORK GUARD] streamed head {blk} regressed below {last_head_num}: chain rollback"
                                )
                            last_head_num = blk
                            last_head_block_ts = blk_ts
                            continue
                        if note_ts is None and SEQUENCER._logs_by_block.get(last_head_num):
                            try:
                                note_ts = await backfill.get_block_timestamp_http(last_head_num)
                            except Exception as e:
                                print(f"[WS] Failed to fetch timestamp for block {last_head_num}: {e!r}", flush=True)
                        SEQUENCER.note_block(last_head_num, block_timestamp=note_ts)

                    last_head_ts = time.monotonic()
                    last_head_num = blk
                    last_head_block_ts = blk_ts
                    if hasattr(SEQUENCER._state, "note_head"):
                        try:
                            SEQUENCER._state.note_head(blk, blk_ts)
                        except Exception:
                            pass
                    continue

                if sid == logs_sub:
                    topics = res.get("topics") or []
                    if not topics:
                        continue

                    tag = h.EVENT_SIGS.get(topics[0].lower())
                    if not tag:
                        continue

                    addr = res.get("address", "").lower()

                    h.register_dynamic_addresses_from_log(res)
                    if not h.accepts_log_for_indexing(tag, addr):
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
                except TimeoutError:
                    print(f"[WS] {name} did not exit cleanly", flush=True)
                except Exception as e:
                    print(f"[WS] {name} error on exit: {e!r}", flush=True)

        return last_head_num


# follow the chain forever, reconnecting whenever the socket drops
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


# periodically snapshot vault balances so account value has history
async def vault_sampler(state):
    while True:
        try:
            vaults = list(getattr(state, "vaults", {}).keys())
            if not vaults:
                await asyncio.sleep(VAULT_SAMPLER_INTERVAL)
                continue

            try:
                state.sweep()
            except Exception:
                pass

            head_resp = await backfill.http_jsonrpc("eth_blockNumber", [])
            blk_hex = head_resp.get("result")
            if not isinstance(blk_hex, str):
                await asyncio.sleep(VAULT_SAMPLER_INTERVAL)
                continue

            blk_num = int(blk_hex, 16)
            ts = 0
            try:
                ts = int(state.block_ts(blk_num))
            except Exception:
                ts = 0
            if ts <= 0:
                try:
                    ts = await backfill.get_block_timestamp_http(blk_num)
                except Exception:
                    ts = int(time.time())

            await _sample_vaults_multicall(state, vaults, blk_hex, blk_num, ts)

        except Exception as e:
            print(f"[SAMPLER] {e!r}", flush=True)

        await asyncio.sleep(VAULT_SAMPLER_INTERVAL)


# rebuild state for a block range after a rollback
