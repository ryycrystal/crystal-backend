import json, asyncio, time, uuid, sys, websockets, urllib.request
from collections import deque
from decimal import Decimal

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
                ts = int(res["timestamp"], 16)
                SEQUENCER._state._bt.note(blk, ts)

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


def _dec_pow(n: int) -> Decimal:
    try:
        return Decimal(10) ** int(n or 0)
    except Exception:
        return Decimal(1)

def _prune_by_age(dq: deque, now_ts: int, horizon: int) -> None:
    cutoff = max(0, int(now_ts) - int(horizon))
    while dq and int(dq[0].get("timestamp", 0)) < cutoff:
        dq.popleft()

async def vault_sampler(state: _st.State):
    rid = 100000
    while True:
        try:
            if not state.vaults:
                await asyncio.sleep(30)
                continue

            head_res = _rpc_batch([{"jsonrpc":"2.0","id":rid,"method":"eth_blockNumber","params":[]}]); rid += 1
            blk_hex = head_res[0]["result"]
            blk_num = int(blk_hex, 16)

            ts = state.block_ts(blk_num)
            if ts == 0:
                _, hts = state.head_block_and_ts()
                ts = int(hts or int(time.time()))

            calls = []
            order = []
            for vaddr in list(state.vaults.keys()):
                order.append(vaddr)
                calls.append({
                    "jsonrpc":"2.0","id":rid,"method":"eth_call",
                    "params":[{"to": vaddr, "data":"0x00113e08"}, blk_hex]
                }); rid += 1

            if not calls:
                await asyncio.sleep(30)
                continue

            results = _rpc_batch(calls)

            for i, vaddr in enumerate(order):
                row = results[i] if i < len(results) else {}
                ret = row.get("result")
                if not isinstance(ret, str) or len(ret) < 2:
                    continue

                s = ret[2:].rjust(64 * 4, "0")
                try:
                    quote_bal = int(s[128:192], 16)
                    base_bal  = int(s[192:256], 16)
                except Exception:
                    continue

                v = state.vaults.get(vaddr)
                if not v:
                    continue

                qd = int(getattr(v, "quoteDecimals", 0) or 0)
                bd = int(getattr(v, "baseDecimals", 0) or 0)
                qaddr = getattr(v, "quote", "").lower()
                baddr = getattr(v, "base", "").lower()

                pq = state.tokenToPrice.get(qaddr, Decimal(0))
                pb = state.tokenToPrice.get(baddr, Decimal(0))

                q_units = Decimal(quote_bal) / _dec_pow(qd)
                b_units = Decimal(base_bal) / _dec_pow(bd)
                tvl_usd = (q_units * pq) + (b_units * pb)
                usd_value = float(tvl_usd) if tvl_usd.is_finite() else 0.0

                snap = {
                    "block": int(blk_num),
                    "timestamp": int(ts),
                    "quoteBalance": int(quote_bal),
                    "baseBalance": int(base_bal),
                    "usdValue": usd_value,
                }

                day = state.vaultBalancesDay.setdefault(vaddr, deque())
                week = state.vaultBalancesWeek.setdefault(vaddr, deque())
                month = state.vaultBalancesMonth.setdefault(vaddr, deque())
                alltm = state.vaultBalancesAllTime.setdefault(vaddr, deque())

                day.append(snap)
                week.append(snap)
                month.append(snap)
                alltm.append(snap)

                _prune_by_age(day, ts, 24 * 3600)
                _prune_by_age(week, ts, 7 * 24 * 3600)
                _prune_by_age(month, ts, 30 * 24 * 3600)

        except Exception as e:
            print(f"[SAMPLER][error] {e!r}")

        await asyncio.sleep(30)

if __name__ == "__main__":
    blk = int(sys.argv[1], 0) if len(sys.argv) > 1 else None
    asyncio.run(stream_logs(blk))
