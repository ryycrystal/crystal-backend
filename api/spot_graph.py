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
    ERC20_TOTAL_SUPPLY_SELECTOR,
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
VALUE_VERSION = 3
_LP_BALANCE_PREFIX = "__lpBalance:"
_LP_SUPPLY_PREFIX = "__lpSupply:"

_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def _block_at_ts(ts: int) -> int | None:
    with db_cursor() as cur:
        cur.execute(
            "SELECT block_number FROM launchpad_trades WHERE timestamp <= %s ORDER BY timestamp DESC LIMIT 1",
            (int(ts),),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


def _earliest_trade_ts() -> int | None:
    with db_cursor() as cur:
        cur.execute("SELECT MIN(timestamp) FROM launchpad_trades")
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


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


def _balances_at_many(
    wallet: str,
    pairs: list[tuple[int, int]],
    tokens: list[dict[str, Any]],
    lp_markets: list[str],
) -> dict[int, dict[str, int] | None]:
    rpc = os.getenv("SPOT_GRAPH_RPC") or os.getenv("RPC_HTTP", "https://rpc.monad.xyz")
    erc20 = [t["address"] for t in tokens if t["address"] != "native"]

    calls = [(MULTICALL3_ADDR, MULTICALL3_GET_ETH_BALANCE_SELECTOR + bytes(12) + bytes.fromhex(wallet[2:]))]
    for addr in erc20:
        calls.append((addr, ERC20_BALANCE_OF_SELECTOR + bytes(12) + bytes.fromhex(wallet[2:])))
    for market in lp_markets:
        calls.append((market, ERC20_BALANCE_OF_SELECTOR + bytes(12) + bytes.fromhex(wallet[2:])))
        calls.append((market, ERC20_TOTAL_SUPPLY_SELECTOR))
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
        lp_offset = 1 + len(erc20)
        for j, market in enumerate(lp_markets):
            ok_balance, raw_balance = decoded[lp_offset + j * 2]
            ok_supply, raw_supply = decoded[lp_offset + j * 2 + 1]
            balances[f"{_LP_BALANCE_PREFIX}{market}"] = u256_at(raw_balance, 0) if ok_balance else 0
            balances[f"{_LP_SUPPLY_PREFIX}{market}"] = u256_at(raw_supply, 0) if ok_supply else 0
        out[ts] = balances
    return out


def _vault_value_at(wallet: str, ts: int) -> Decimal:
    addr = (wallet or "").lower()
    with db_cursor() as cur:
        cur.execute(
            """
            WITH held AS (
                SELECT vault, SUM(s) AS shares FROM (
                    SELECT vault, shares AS s FROM crystal_vault_deposits
                    WHERE user_address = %s AND timestamp <= %s
                    UNION ALL
                    SELECT vault, -shares AS s FROM crystal_vault_withdrawals
                    WHERE user_address = %s AND timestamp <= %s
                ) x GROUP BY vault
            )
            SELECT h.shares, b.usd_value, b.shares
            FROM held h
            LEFT JOIN LATERAL (
                SELECT usd_value, shares
                FROM crystal_vault_balance_samples sm
                WHERE sm.vault = h.vault AND sm.timestamp <= %s
                ORDER BY sm.timestamp DESC, sm.block_number DESC
                LIMIT 1
            ) b ON TRUE
            WHERE h.shares > 0
            """,
            (addr, ts, addr, ts, ts),
        )
        rows = cur.fetchall()
    total = Decimal(0)
    for held_shares, usd_value, supply in rows:
        sup = Decimal(supply or 0)
        if sup > 0:
            total += Decimal(held_shares) / sup * Decimal(usd_value or 0)
    return total


def _lp_value_at(ts: int, balances: dict[str, int]) -> Decimal:
    positions = []
    for key, raw_balance in balances.items():
        if not key.startswith(_LP_BALANCE_PREFIX) or raw_balance <= 0:
            continue
        market = key[len(_LP_BALANCE_PREFIX) :]
        supply = int(balances.get(f"{_LP_SUPPLY_PREFIX}{market}", 0) or 0)
        if supply > 0:
            positions.append((market, int(raw_balance), supply))
    if not positions:
        return Decimal(0)
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT requested.market, latest.tvl_usd
            FROM UNNEST(%s::text[]) AS requested(market)
            LEFT JOIN LATERAL (
                SELECT tvl_usd
                FROM crystal_pool_tvl_samples sample
                WHERE sample.market = requested.market AND sample.timestamp <= %s
                ORDER BY sample.timestamp DESC, sample.block_number DESC, sample.log_index DESC
                LIMIT 1
            ) latest ON TRUE
            """,
            ([market for market, _balance, _supply in positions], int(ts)),
        )
        values = {market: Decimal(tvl or 0) for market, tvl in cur.fetchall()}
    return sum(
        (Decimal(balance) / Decimal(supply)) * values.get(market, Decimal(0)) for market, balance, supply in positions
    )


def _locked_value_at(
    orders: list[dict[str, Any]], ts: int, tokens: list[dict[str, Any]], mon_usd: Decimal, wmon: str
) -> Decimal:
    if not orders:
        return Decimal(0)
    locked = storage.locked_by_token_at(orders, ts)
    if not locked:
        return Decimal(0)
    by_addr = {t["address"]: t for t in tokens}
    total = Decimal(0)
    for addr, raw in locked.items():
        t = by_addr.get(addr)
        if t is None or raw <= 0:
            continue
        whole = Decimal(raw) / Decimal(10) ** int(t.get("decimals") or 18)
        price = _token_price_at(t, ts, mon_usd, wmon)
        if price is not None:
            total += whole * price
    return total


def _write_bucket(
    wallet: str,
    ts: int,
    block: int,
    balances: dict[str, int],
    tokens: list[dict[str, Any]],
    orders: list[dict[str, Any]] | None = None,
) -> None:
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
    vaults_usd = _vault_value_at(wallet, ts)
    if vaults_usd > 0:
        value_usd += vaults_usd
        if mon_usd > 0:
            value_native += vaults_usd / mon_usd
    lp_usd = _lp_value_at(ts, balances)
    if lp_usd > 0:
        value_usd += lp_usd
        if mon_usd > 0:
            value_native += lp_usd / mon_usd
    locked_usd = _locked_value_at(orders or [], ts, tokens, mon_usd, wmon)
    if locked_usd > 0:
        value_usd += locked_usd
        if mon_usd > 0:
            value_native += locked_usd / mon_usd
    stored_balances = {k: str(v) for k, v in balances.items()}
    stored_balances["__valueVersion"] = VALUE_VERSION
    storage.write_spot_graph_bucket(wallet, ts, block, value_usd, value_native, stored_balances)


def _wanted_buckets(now: int) -> list[int]:
    now_bucket = (now // RESOLUTION) * RESOLUTION
    since = now_bucket - LOOKBACK_BUCKETS * RESOLUTION
    floor = _earliest_trade_ts()
    if floor is not None:
        floor_bucket = ((floor // RESOLUTION) + 1) * RESOLUTION
        since = max(since, floor_bucket)
    return [t for t in range(now_bucket - RESOLUTION, since - 1, -RESOLUTION)]


def _fill(wallet: str) -> None:
    try:
        from api.spot_data import spot_token_list

        if not storage.wallet_has_crystal_activity(wallet):
            return

        tokens = spot_token_list()
        lp_markets = storage.list_lp_markets_for_graph(wallet)
        orders = storage.orders_for_graph(wallet)
        wanted = _wanted_buckets(int(time.time()))
        if not wanted:
            return
        have = storage.get_spot_graph_bucket_set(wallet, wanted[-1], VALUE_VERSION)
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
                balances_by_ts = _balances_at_many(wallet, group, tokens, lp_markets)
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
                    _write_bucket(wallet, ts, block, balances, tokens, orders)
                    wrote += 1
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


def ensure_fill(wallet: str) -> None:
    wallet = (wallet or "").lower()
    with _inflight_lock:
        if wallet in _inflight:
            return
        _inflight.add(wallet)
    threading.Thread(target=_fill, args=(wallet,), daemon=True).start()


def graph_for(wallet: str) -> dict[str, Any]:
    wallet = (wallet or "").lower()
    wanted = _wanted_buckets(int(time.time()))
    since = wanted[-1] if wanted else 0
    rows = storage.get_spot_graph_buckets(wallet, since, VALUE_VERSION)
    points = [{"t": ts, "v": float(usd)} for ts, usd, _native in rows]
    floor = int(storage.get_meta("spot_graph_floor") or 0)
    reachable = [t for t in wanted if t >= floor]
    reached = sum(1 for p in points if p["t"] >= floor)
    return {
        "resolution": RESOLUTION,
        "points": points,
        "complete": reached >= len(reachable),
    }
