import json, asyncio, time, uuid, sys, websockets, urllib.request
from collections import deque

from core import chain as h
import backfill
from state import RPC_HTTP
import state as _st

from core.sequencer import SEQUENCER

HEAD_TIMEOUT = 5.0
BACKFILL_BATCH = 100

missing_blocks: deque[int] = deque()
missing_set: set[int] = set()

def _rpc_batch(calls: list[dict]) -> list[dict]:
    payload = json.dumps(calls).encode()
    req = urllib.request.Request(RPC_HTTP, data=payload, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

async def vault_sampler(state: _st.State):
    rid = 100000
    while True:
        try:
            meta = state.vault_meta()
            if not meta:
                await asyncio.sleep(300)
                continue

            calls = [{"jsonrpc":"2.0","id":rid,"method":"eth_blockNumber","params":[]}]
            rid += 1
            res0 = _rpc_batch(calls)[0]["result"]
            blk_hex = res0
            blk_num = int(blk_hex, 16)
            ts = state._bt.ts(blk_num)

            calls = []
            for v, (_quote, _base) in meta.items():
                calls.append({
                    "jsonrpc":"2.0","id":rid,"method":"eth_call",
                    "params":[{"to": v.lower(), "data": "0x00113e08"}, blk_hex]
                }); rid += 1

            results = _rpc_batch(calls)
            
            i = 0
            for v, _ in meta.items():
                ret = results[i]["result"]; i += 1
                s = ret[2:].rjust(64 * 4, "0")
                quote_bal = int(s[128:192], 16)
                base_bal = int(s[192:256], 16)
                state.apply_vault_snapshot(v, blk_num, ts, 0, quote_bal, base_bal, total_shares=0)

        except Exception:
            print(f"[SAMPLER][error] {e!r}")
            pass

        await asyncio.sleep(300)


def _add_missing(blk: int):
    if blk not in missing_set:
        missing_blocks.append(blk)
        missing_set.add(blk)


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
            async with websockets.connect(h.WS_URL) as gap_ws:
                rid = str(uuid.uuid4())
                await h.rate_gate()
                await gap_ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "eth_getLogs",
                    "params": [{
                        "fromBlock": hex(blk_start),
                        "toBlock": hex(blk_end),
                        "address": h.ADDRS,
                        "topics": [h.TOPICS],
                    }],
                }))
                resp = await h.ack(gap_ws, rid)

            for log in resp.get("result", []):
                tag = h.EVENT_SIGS.get(log["topics"][0].lower())
                if tag:
                    SEQUENCER.add_log(log)

            for blk in range(blk_start, blk_end + 1):
                SEQUENCER.note_block(blk)

        except RuntimeError as e:
            err = e.args[0]
            if isinstance(err, dict) and err.get("error", {}).get("code") == -32007:
                print("[RL] hit provider cap, retrying after 1 s")

                for blk in range(blk_start, blk_end + 1):
                    _add_missing(blk)
                await asyncio.sleep(1.0)
            else:
                raise


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
