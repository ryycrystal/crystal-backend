from __future__ import annotations

import asyncio
import time

import core.storage as storage
from core.multicall import MULTICALL3_ADDR, decode_multicall3_aggregate3_result, encode_multicall3_aggregate3

RECONCILE_INTERVAL = 120
RECONCILE_STALE_SECONDS = 300
RECONCILE_LOOKBACK_SECONDS = 86400
RECONCILE_BATCH = 200
RECONCILE_CHUNK = 40

_GET_RESERVES = bytes.fromhex("0902f1ac")
_SLOT0 = bytes.fromhex("3850c7bd")
_LIQUIDITY = bytes.fromhex("1a686502")

Q96 = 1 << 96


def _u256(data: bytes, off: int = 0) -> int:
    end = off + 32
    return int.from_bytes(data[off:end], "big") if len(data) >= end else 0


def _v2_reserves(data: bytes) -> tuple[int, int] | None:
    if len(data) < 64:
        return None
    return _u256(data, 0), _u256(data, 32)


def _v3_virtual_reserves(slot0: bytes, liquidity: bytes) -> tuple[int, int] | None:
    sqrt_price = _u256(slot0, 0)
    liq = _u256(liquidity, 0)
    if sqrt_price <= 0 or liq <= 0:
        return None
    return liq * Q96 // sqrt_price, liq * sqrt_price // Q96


async def reconcile_once(limit: int = RECONCILE_BATCH) -> dict:
    import backfill

    rows = await asyncio.to_thread(
        storage.pools_needing_reserve_refresh, limit, RECONCILE_STALE_SECONDS, RECONCILE_LOOKBACK_SECONDS
    )
    if not rows:
        return {"checked": 0, "repaired": 0, "v2": 0, "v3": 0, "skipped": 0}

    head_resp = await backfill.http_jsonrpc("eth_blockNumber", [])
    head = int(head_resp.get("result", "0x0"), 16)
    now_ts = int(time.time())

    repaired = v2_count = v3_count = skipped = 0
    for i in range(0, len(rows), RECONCILE_CHUNK):
        chunk = rows[i : i + RECONCILE_CHUNK]
        calls: list[tuple[str, bytes]] = []
        for pool, _token, _is0, _synced in chunk:
            calls += [(pool, _GET_RESERVES), (pool, _SLOT0), (pool, _LIQUIDITY)]

        try:
            resp = await backfill.http_jsonrpc(
                "eth_call",
                [{"to": MULTICALL3_ADDR, "data": encode_multicall3_aggregate3(calls, allow_failure=True)}, "latest"],
            )
        except Exception as e:
            print(f"[RESERVE-SYNC] multicall failed {e!r}", flush=True)
            continue

        ret = resp.get("result")
        results = decode_multicall3_aggregate3_result(ret) if isinstance(ret, str) else []
        if len(results) != len(calls):
            print(f"[RESERVE-SYNC] length mismatch {len(results)} != {len(calls)}", flush=True)
            continue

        for idx, (pool, _token, token_is_0, _synced) in enumerate(chunk):
            ok_v2, raw_v2 = results[idx * 3]
            ok_slot0, raw_slot0 = results[idx * 3 + 1]
            ok_liq, raw_liq = results[idx * 3 + 2]

            pair = None
            kind = ""
            if ok_v2:
                pair = _v2_reserves(raw_v2)
                kind = "v2"
            elif ok_slot0 and ok_liq:
                pair = _v3_virtual_reserves(raw_slot0, raw_liq)
                kind = "v3"

            if pair is None:
                skipped += 1
                continue

            r0, r1 = pair
            reserve_token, reserve_native = (r0, r1) if token_is_0 else (r1, r0)
            if reserve_token <= 0 or reserve_native <= 0:
                skipped += 1
                continue

            await asyncio.to_thread(storage.force_pool_reserves, pool, reserve_token, reserve_native, head, now_ts)
            repaired += 1
            if kind == "v2":
                v2_count += 1
            else:
                v3_count += 1

    return {"checked": len(rows), "repaired": repaired, "v2": v2_count, "v3": v3_count, "skipped": skipped}


async def pool_reserve_worker() -> None:
    while True:
        try:
            r = await reconcile_once()
            if r["repaired"] or r["skipped"]:
                print(
                    f"[RESERVE-SYNC] refreshed {r['repaired']}/{r['checked']} pools "
                    f"(v2 {r['v2']}, v3 {r['v3']}, skipped {r['skipped']})",
                    flush=True,
                )
        except Exception as e:
            print(f"[RESERVE-SYNC] failed {e!r}", flush=True)
        await asyncio.sleep(RECONCILE_INTERVAL)
