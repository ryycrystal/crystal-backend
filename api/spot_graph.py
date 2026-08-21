# spot portfolio value history as server computed hourly buckets.
#
# the frontend used to rebuild this series itself: up to ~721 archive reads per
# page load at guessed block numbers, truncated to 13 tokens to survive. a past
# bucket can never change, so the server computes each one once from a single
# archive multicall, stores it forever, and every later view by anyone is a pure
# db read. timestamps map to blocks and mon/usd through the already indexed
# trades table, so only the balance read itself ever touches rpc

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from decimal import Decimal
from typing import Any

import core.storage as storage
from core.multicall import (
    ERC20_BALANCE_OF_SELECTOR,
    MULTICALL3_ADDR,
    MULTICALL3_GET_ETH_BALANCE_SELECTOR,
    decode_multicall3_aggregate3_result,
    encode_multicall3_aggregate3,
    u256_at,
)
from core.storage import db_cursor

RESOLUTION = int(os.getenv("SPOT_GRAPH_RESOLUTION", "3600"))
LOOKBACK_BUCKETS = int(os.getenv("SPOT_GRAPH_LOOKBACK", "720"))
BUCKETS_PER_RPC_BATCH = int(os.getenv("SPOT_GRAPH_RPC_BATCH", "10"))
FILL_PACE_SECONDS = float(os.getenv("SPOT_GRAPH_PACE", "0.2"))
MAX_CONSECUTIVE_FAILURES = int(os.getenv("SPOT_GRAPH_MAX_FAILURES", "5"))
_WEI = Decimal(10) ** 18
_MIN_PRICE_TRADE_WEI = 10**16

_inflight: set[str] = set()
_inflight_lock = threading.Lock()


# newest indexed block at or before a timestamp, from the dense trade history
def _block_at_ts(ts: int) -> int | None:
    with db_cursor() as cur:
        cur.execute(
            "SELECT block_number FROM launchpad_trades WHERE timestamp <= %s ORDER BY timestamp DESC LIMIT 1",
            (int(ts),),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


# earliest indexed trade timestamp, the floor below which no bucket is knowable
def _earliest_trade_ts() -> int | None:
    with db_cursor() as cur:
        cur.execute("SELECT MIN(timestamp) FROM launchpad_trades")
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


# mon/usd at a timestamp, implied by the nearest sizeable trade's stored usd value
def _mon_usd_at(ts: int) -> Decimal:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT usd_amount, native_amount FROM launchpad_trades
            WHERE timestamp <= %s AND native_amount >= %s AND usd_amount > 0
            ORDER BY timestamp DESC LIMIT 1
            """,
            (int(ts), _MIN_PRICE_TRADE_WEI),
        )
        row = cur.fetchone()
    if not row:
        return Decimal(0)
    usd, native = row[0] or Decimal(0), Decimal(int(row[1] or 0))
    if native <= 0:
        return Decimal(0)
    return usd / (native / _WEI)


# usd price per whole token at a timestamp. usdc is a dollar, native and wmon are
# the oracle, amm backed tokens come from their reserve ratio at that time, and
# anything unpriceable is null so it is excluded rather than valued with a guess
def _token_price_at(token: dict[str, Any], ts: int, mon_usd: Decimal, wmon: str) -> Decimal | None:
    addr = token["address"]
    ticker = (token.get("ticker") or "").upper()
    if ticker == "USDC":
        return Decimal(1)
    if addr == "native" or addr == wmon:
        return mon_usd if mon_usd > 0 else None
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT e.reserve_quote, e.reserve_base, m.base_decimals
            FROM crystal_pool_sync_events e
            JOIN crystal_markets m ON m.market = e.market
            WHERE LOWER(m.base_address) = %s AND LOWER(m.quote_address) = %s AND e.timestamp <= %s
            ORDER BY e.timestamp DESC LIMIT 1
            """,
            (addr, wmon, int(ts)),
        )
        row = cur.fetchone()
    if not row or mon_usd <= 0:
        return None
    rq, rb, base_decimals = Decimal(int(row[0] or 0)), Decimal(int(row[1] or 0)), int(row[2] or 18)
    if rq <= 0 or rb <= 0:
        return None
    price_native = (rq / _WEI) / (rb / Decimal(10) ** base_decimals)
    return price_native * mon_usd


# archive balances for several buckets in one json-rpc http batch: per bucket one
# multicall3 aggregate carrying the native read plus every erc20 balanceOf
def _balances_at_many(
    wallet: str, pairs: list[tuple[int, int]], tokens: list[dict[str, Any]]
) -> dict[int, dict[str, int] | None]:
    # historical eth_call needs archive state; the general rpc's depth varies by
    # which node the load balancer serves, so the filler gets its own endpoint
    rpc = os.getenv("SPOT_GRAPH_RPC") or os.getenv("RPC_HTTP", "https://rpc.monad.xyz")
    erc20 = [t["address"] for t in tokens if t["address"] != "native"]

    calls = [(MULTICALL3_ADDR, MULTICALL3_GET_ETH_BALANCE_SELECTOR + bytes(12) + bytes.fromhex(wallet[2:]))]
    for addr in erc20:
        calls.append((addr, ERC20_BALANCE_OF_SELECTOR + bytes(12) + bytes.fromhex(wallet[2:])))
    data = encode_multicall3_aggregate3(calls)

    batch = [
        {
            "jsonrpc": "2.0",
            "id": i,
            "method": "eth_call",
            "params": [{"to": MULTICALL3_ADDR, "data": data}, hex(int(block))],
        }
        for i, (_ts, block) in enumerate(pairs)
    ]
    req = urllib.request.Request(rpc, data=json.dumps(batch).encode(), headers={"Content-Type": "application/json"})
    results = json.load(urllib.request.urlopen(req, timeout=30))
    by_id = {r.get("id"): r for r in results if isinstance(r, dict)}

    out: dict[int, dict[str, int] | None] = {}
    for i, (ts, _block) in enumerate(pairs):
        res = (by_id.get(i) or {}).get("result")
        decoded = decode_multicall3_aggregate3_result(res) if isinstance(res, str) else []
        if len(decoded) != len(calls):
            out[ts] = None
            continue
        balances: dict[str, int] = {}
        ok0, raw0 = decoded[0]
        balances["native"] = u256_at(raw0, 0) if ok0 else 0
        for j, addr in enumerate(erc20):
            okj, rawj = decoded[j + 1]
            balances[addr] = u256_at(rawj, 0) if okj else 0
        out[ts] = balances
    return out


# value one bucket's balances and persist it
def _write_bucket(wallet: str, ts: int, block: int, balances: dict[str, int], tokens: list[dict[str, Any]]) -> None:
    mon_usd = _mon_usd_at(ts)
    wmon = next((t["address"] for t in tokens if (t.get("ticker") or "").upper() == "WMON"), "")
    value_usd = Decimal(0)
    value_native = Decimal(0)
    for t in tokens:
        raw = balances.get(t["address"], 0)
        if raw <= 0:
            continue
        whole = Decimal(raw) / Decimal(10) ** int(t.get("decimals") or 18)
        price = _token_price_at(t, ts, mon_usd, wmon)
        if price is not None:
            value_usd += whole * price
            if mon_usd > 0:
                value_native += whole * price / mon_usd
    storage.write_spot_graph_bucket(
        wallet, ts, block, value_usd, value_native, {k: str(v) for k, v in balances.items()}
    )


# the bucket grid this wallet should have, newest first, open bucket excluded
def _wanted_buckets(now: int) -> list[int]:
    now_bucket = (now // RESOLUTION) * RESOLUTION
    since = now_bucket - LOOKBACK_BUCKETS * RESOLUTION
    floor = _earliest_trade_ts()
    if floor is not None:
        floor_bucket = ((floor // RESOLUTION) + 1) * RESOLUTION
        since = max(since, floor_bucket)
    return [t for t in range(now_bucket - RESOLUTION, since - 1, -RESOLUTION)]


# fill every missing bucket for one wallet, newest first so the chart's right
# edge appears immediately. rows are immutable so re-runs converge to complete
def _fill(wallet: str) -> None:
    try:
        from api.spot_data import spot_token_list

        if not storage.wallet_has_crystal_activity(wallet):
            return

        tokens = spot_token_list()
        wanted = _wanted_buckets(int(time.time()))
        if not wanted:
            return
        have = storage.get_spot_graph_bucket_set(wallet, wanted[-1])
        missing = [t for t in wanted if t not in have]
        failures = 0
        empty_batches = 0
        empty_streak_top = 0
        for i in range(0, len(missing), BUCKETS_PER_RPC_BATCH):
            group = []
            for ts in missing[i : i + BUCKETS_PER_RPC_BATCH]:
                block = _block_at_ts(ts)
                if block is not None:
                    group.append((ts, block))
            if not group:
                continue
            try:
                balances_by_ts = _balances_at_many(wallet, group, tokens)
            except Exception as e:
                failures += 1
                print(f"[GRAPH] balance batch failed for {wallet} ({e!r})", flush=True)
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"[GRAPH] giving up fill for {wallet} after {failures} failures", flush=True)
                    return
                time.sleep(2.0 * failures)
                continue
            failures = 0
            wrote = 0
            for ts, block in group:
                balances = balances_by_ts.get(ts)
                if balances is not None:
                    _write_bucket(wallet, ts, block, balances, tokens)
                    wrote += 1
            # the fill runs newest first, so two straight batches of per-call
            # misses means the rpc's archive depth is behind us: everything
            # older will miss too, and future hours land inside the window. the
            # floor is recorded so completeness stops chasing unreachable history
            if wrote == 0:
                if empty_batches == 0:
                    empty_streak_top = max(ts for ts, _b in group)
                empty_batches += 1
                if empty_batches >= 2:
                    floor = empty_streak_top + RESOLUTION
                    storage.set_meta("spot_graph_floor", str(int(floor)))
                    print(f"[GRAPH] archive depth reached for {wallet}, floor {floor}", flush=True)
                    return
            else:
                empty_batches = 0
            time.sleep(FILL_PACE_SECONDS)
    except Exception as e:
        print(f"[GRAPH] fill crashed for {wallet}: {e!r}", flush=True)
    finally:
        with _inflight_lock:
            _inflight.discard(wallet)


# kick a background fill unless one is already running for this wallet
def ensure_fill(wallet: str) -> None:
    wallet = (wallet or "").lower()
    with _inflight_lock:
        if wallet in _inflight:
            return
        _inflight.add(wallet)
    threading.Thread(target=_fill, args=(wallet,), daemon=True).start()


# the cached series plus a completeness flag the client can re-poll on.
# complete means "filled down to what the archive can answer": buckets below the
# recorded archive floor are unreachable and must not keep the client polling
def graph_for(wallet: str) -> dict[str, Any]:
    wallet = (wallet or "").lower()
    wanted = _wanted_buckets(int(time.time()))
    since = wanted[-1] if wanted else 0
    rows = storage.get_spot_graph_buckets(wallet, since)
    points = [{"t": ts, "v": float(usd)} for ts, usd, _native in rows]
    floor = int(storage.get_meta("spot_graph_floor") or 0)
    reachable = [t for t in wanted if t >= floor]
    reached = sum(1 for p in points if p["t"] >= floor)
    return {
        "resolution": RESOLUTION,
        "points": points,
        "complete": reached >= len(reachable),
    }
