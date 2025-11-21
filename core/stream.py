import json, asyncio, time, uuid, sys, websockets, urllib.request
from collections import deque
from decimal import Decimal

from core import chain as h
import backfill
from state import RPC_HTTP
import state as _st

from core.sequencer import SEQUENCER

HEAD_TIMEOUT = 60.0
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
                topics = log.get("topics") or []
                if not topics:
                    continue
                
                tag = h.EVENT_SIGS.get(topics[0].lower())
                if not tag:
                    continue
                
                if tag != "TF":
                    addr = log.get("address", "").lower()
                    if addr not in h.ADDRS:
                        continue
                    
                if tag in event_counts:
                    event_counts[tag] += 1

                SEQUENCER.add_log(log)

            for blk in range(blk_start, blk_end + 1):
                SEQUENCER.note_block(blk)

        except RuntimeError as e:
            err = e.args[0]
            if isinstance(err, dict) and err.get("error", {}).get("code") == -32007:
                print("[RL] Hit provider cap, retrying")

                for blk in range(blk_start, blk_end + 1):
                    _add_missing(blk)
                await asyncio.sleep(1.0)
            else:
                raise


async def _stream_once(prev_last_head: int | None) -> int | None:
    connect_kwargs = dict(
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
        max_queue=None,
        open_timeout=10,
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

        async def watchdog():
            nonlocal last_head_ts, last_head_num
            while True:
                await asyncio.sleep(0.5)
                if time.monotonic() - last_head_ts > HEAD_TIMEOUT:
                    print("[wd] no newHeads, forcing reconnect and backfill", flush=True)
                    if last_head_num is not None:
                        await backfill.backfill(last_head_num, BACKFILL_BATCH)
                    await ws.close()
                    return

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
                topics = res.get("topics") or []
                if not topics:
                    continue

                tag = h.EVENT_SIGS.get(topics[0].lower())
                if not tag:
                    continue

                if tag != "TF":
                    addr = res.get("address", "").lower()
                    if addr not in h.ADDRS:
                        continue

                if tag in event_counts:
                    event_counts[tag] += 1

                SEQUENCER.add_log(res)

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
            print(f"[ws] dropped {e!r}", flush=True)

        await asyncio.sleep(delay)
        delay = min(delay * 2, 10) + (0.0 if delay >= 10 else 0.25)


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
            state.sweep()

            if state.vaults:
                head_res = _rpc_batch([{
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "eth_blockNumber",
                    "params": [],
                }])
                rid += 1

                blk_hex = head_res[0]["result"]
                blk_num = int(blk_hex, 16)

                ts = int(time.time())

                calls = []
                order = []
                for vaddr in list(state.vaults.keys()):
                    order.append(vaddr)
                    calls.append({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "method": "eth_call",
                        "params": [
                            {"to": vaddr, "data": "0x00113e08"},
                            blk_hex,
                        ],
                    })
                    rid += 1

                if calls:
                    results = _rpc_batch(calls)

                    for i, vaddr in enumerate(order):
                        row = results[i] if i < len(results) else {}
                        ret = row.get("result")
                        if not isinstance(ret, str) or len(ret) < 2:
                            continue

                        s = ret[2:].rjust(64 * 4, "0")
                        try:
                            quote_bal = int(s[128:192], 16)
                            base_bal = int(s[192:256], 16)
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

            else:
                ts = int(time.time())

            for market, pool in state.ammPools.items():
                tvl = float(pool.tvlUsd)
                vol_24h = float(pool.volume24hUsd)

                fee_rate = float(pool.feeBps) / 10_000.0
                fees_24h = vol_24h * fee_rate
                pool.fees24hUsd = Decimal(fees_24h)

                if tvl > 0.0 and fees_24h > 0.0:
                    r_day = fees_24h / tvl
                    apy = (1.0 + r_day) ** 365 - 1.0
                    pool.apy24h = Decimal(apy)

                    hist = state.ammHistory.setdefault(market, [])
                    hist.append({"timestamp": int(ts), "apy": float(apy)})
                    if len(hist) > 200:
                        hist[:] = hist[-200:]
                else:
                    pool.apy24h = Decimal(0)

        except Exception as e:
            print(f"[SAMPLER][error] {e!r}")

        await asyncio.sleep(15)

if __name__ == "__main__":
    blk = int(sys.argv[1], 0) if len(sys.argv) > 1 else None
    asyncio.run(stream_logs(blk))
