import argparse
import os
import time

import httpx

import core.storage as storage
from core.multicall import (
    MULTICALL3_ADDR,
    decode_multicall3_aggregate3_result,
    encode_multicall3_aggregate3,
    u256_at,
)

# these pools are not uniswap v2 pairs: getReserves() reverts on them. what a
# pool holds is simply its balance of each side, which every erc20 answers, so
# the sweep reads balanceOf for the token and the native asset instead
BALANCE_OF = bytes.fromhex("70a08231")
# two calls per pool, so the batch is expressed in pools to keep the stride
# and the slice from drifting apart
POOLS_PER_BATCH = 150


def _balance_of(holder: str) -> bytes:
    return BALANCE_OF + bytes.fromhex(holder[2:].rjust(64, "0"))


# graduated pools only learn their reserves when they next trade, so a quiet
# pool shows no liquidity until someone touches it. one multicall per batch
# reads them all straight from chain state instead of waiting for a sync log
def main() -> None:
    parser = argparse.ArgumentParser(description="read graduated pool reserves from chain state")
    parser.add_argument("--rpc", default=os.environ.get("RESERVES_RPC", "https://rpc.monad.xyz"))
    parser.add_argument("--rps", type=float, default=5.0)
    parser.add_argument("--only-missing", action="store_true", help="skip pools that already have reserves")
    args = parser.parse_args()

    storage.init_pool()
    where = "WHERE reserve_native = 0 AND reserve_token = 0" if args.only_missing else ""
    with storage.db_cursor() as cur:
        cur.execute(f"SELECT pool, token_addr, native_addr, token_is_0 FROM launchpad_pools {where} ORDER BY pool")
        pools = cur.fetchall()
    if not pools:
        print("[RESERVES] nothing to read", flush=True)
        return
    print(f"[RESERVES] reading {len(pools):,} pools", flush=True)

    client = httpx.Client(timeout=90.0)
    min_interval = 1.0 / args.rps if args.rps > 0 else 0.0
    last = 0.0
    written = empty = failed = 0
    started = time.monotonic()

    head = int(
        client.post(args.rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
        .json()["result"],
        16,
    )
    now = int(time.time())

    for i in range(0, len(pools), POOLS_PER_BATCH):
        batch = pools[i : i + POOLS_PER_BATCH]
        calls = []
        for _pool, token_addr, native_addr, _is0 in batch:
            calls.append((token_addr, _balance_of(_pool)))
            calls.append((native_addr, _balance_of(_pool)))
        payload = encode_multicall3_aggregate3(calls)

        result = None
        for attempt in range(5):
            wait = min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.monotonic()
            try:
                r = client.post(
                    args.rpc,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_call",
                        "params": [{"to": MULTICALL3_ADDR, "data": payload}, "latest"],
                    },
                )
                r.raise_for_status()
                body = r.json()
                if "error" in body:
                    raise RuntimeError(body["error"])
                result = decode_multicall3_aggregate3_result(body["result"])
                break
            except Exception as e:
                print(f"[RESERVES] batch at {i} attempt {attempt + 1}/5 failed ({e!r})", flush=True)
                time.sleep(2.0 * (attempt + 1))
        if result is None:
            failed += len(batch)
            continue

        with storage.db_cursor() as cur:
            for idx, (pool, _token_addr, _native_addr, token_is_0) in enumerate(batch):
                ok_t, data_t = result[idx * 2]
                ok_n, data_n = result[idx * 2 + 1]
                if not ok_t or not ok_n or len(data_t) < 32 or len(data_n) < 32:
                    empty += 1
                    continue
                token_bal, native_bal = u256_at(data_t, 0), u256_at(data_n, 0)
                if token_bal == 0 and native_bal == 0:
                    empty += 1
                    continue
                # the writer takes positional reserves and orients them itself,
                # so hand it the pair in the order that row records
                r0, r1 = (token_bal, native_bal) if token_is_0 else (native_bal, token_bal)
                storage.update_pool_reserves(pool, r0, r1, head, now, cur=cur)
                written += 1

        done = i + len(batch)
        if done % (POOLS_PER_BATCH * 20) < POOLS_PER_BATCH:
            rate = done / max(time.monotonic() - started, 0.001)
            eta = (len(pools) - done) / max(rate, 0.001)
            print(f"[RESERVES] {done:,}/{len(pools):,}  written {written:,}  ({eta / 60:.0f}m left)", flush=True)

    print(f"[RESERVES] complete: {written:,} written, {empty:,} empty or unreadable, {failed:,} failed", flush=True)


if __name__ == "__main__":
    main()
