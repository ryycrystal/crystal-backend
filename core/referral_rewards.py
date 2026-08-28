import asyncio
import os
import time

import backfill
import core.storage as storage
from core import chain as h
from core.multicall import MULTICALL3_ADDR, decode_multicall3_aggregate3_result, encode_multicall3_aggregate3

REWARDS_INTERVAL = int(os.getenv("REFERRAL_REWARDS_INTERVAL", "120"))
REWARDS_MULTICALL_CHUNK = 200
CLAIMABLE_REWARDS_SELECTOR = bytes.fromhex("6be9dcce")


def _claimable_calldata(token: str, referrer: str) -> bytes:
    t = bytes.fromhex(str(token or "").lower().replace("0x", "").rjust(64, "0"))
    r = bytes.fromhex(str(referrer or "").lower().replace("0x", "").rjust(64, "0"))
    return CLAIMABLE_REWARDS_SELECTOR + t + r


def _reward_tokens() -> list[str]:
    tokens: set[str] = set()
    try:
        with storage.db_cursor() as cur:
            cur.execute("SELECT DISTINCT quote_address FROM crystal_markets WHERE quote_address <> ''")
            tokens.update(str(r[0]).lower() for r in cur.fetchall())
            cur.execute("SELECT DISTINCT token FROM referral_rewards")
            tokens.update(str(r[0]).lower() for r in cur.fetchall())
    except Exception:
        pass
    return sorted(tokens)


async def _sweep() -> None:
    referrers = storage.list_active_referrers()
    tokens = _reward_tokens()
    if not referrers or not tokens:
        return
    pairs = [(ref, tok) for ref in referrers for tok in tokens]
    now_ts = int(time.time())
    for i in range(0, len(pairs), REWARDS_MULTICALL_CHUNK):
        chunk = pairs[i : i + REWARDS_MULTICALL_CHUNK]
        calls = [(h.CONTRACTS["ROUTER"], _claimable_calldata(tok, ref)) for ref, tok in chunk]
        data = encode_multicall3_aggregate3(calls)
        resp = await backfill.http_jsonrpc("eth_call", [{"to": MULTICALL3_ADDR, "data": data}, "latest"])
        ret = resp.get("result")
        results = decode_multicall3_aggregate3_result(ret) if isinstance(ret, str) else []
        if len(results) != len(chunk):
            raise ValueError(f"referral multicall length mismatch {len(results)} != {len(chunk)}")
        for (ref, tok), (ok, raw) in zip(chunk, results):
            if not ok or not isinstance(raw, (bytes, bytearray)) or len(raw) < 32:
                continue
            claimable = int.from_bytes(raw[:32], "big")
            storage.record_referral_reward(ref, tok, claimable, now_ts)


async def referral_rewards_worker() -> None:
    while True:
        try:
            await _sweep()
        except Exception as e:
            print(f"[REF] Rewards sweep failed {e!r}", flush=True)
        await asyncio.sleep(REWARDS_INTERVAL)
