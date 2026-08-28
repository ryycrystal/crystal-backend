import argparse
import os

import httpx

import core.storage as storage
from core import chain as h
from modules.referrals import CLAIM_TOPIC, parse_rewards_claimed


def main() -> None:
    parser = argparse.ArgumentParser(description="backfill on-chain fee claims")
    parser.add_argument("--rpc", default=os.environ.get("CLAIMS_RPC", "https://rpc.monad.xyz"))
    parser.add_argument("--start-block", type=lambda x: int(x, 0), default=92_600_000)
    args = parser.parse_args()

    storage.init_pool()
    router = h.CONTRACTS["ROUTER"]

    resp = httpx.post(
        args.rpc,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [
                {
                    "fromBlock": hex(args.start_block),
                    "toBlock": "latest",
                    "address": router,
                    "topics": [CLAIM_TOPIC],
                }
            ],
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(body["error"])

    inserted = 0
    for log in body.get("result") or []:
        data = log.get("data") or "0x"
        parsed = parse_rewards_claimed((log.get("address") or "").lower(), log.get("topics") or [], data[2:])
        if not parsed.get("tokens"):
            continue
        blk_raw = log.get("blockNumber")
        blk = int(blk_raw, 16) if isinstance(blk_raw, str) else int(blk_raw or 0)
        ts_raw = log.get("blockTimestamp")
        ts = int(ts_raw, 16) if isinstance(ts_raw, str) else int(ts_raw or 0)
        li_raw = log.get("logIndex")
        li = int(li_raw, 16) if isinstance(li_raw, str) else int(li_raw or 0)
        storage.write_referral_claims(
            blk,
            ts,
            (log.get("transactionHash") or "").lower(),
            li,
            parsed["user"],
            parsed["tokens"],
            parsed["amounts"],
        )
        inserted += 1
        print(f"[CLAIMS] {parsed['user']} claimed {len(parsed['tokens'])} tokens at block {blk}", flush=True)

    print(f"[CLAIMS] complete: {inserted} claim events", flush=True)


if __name__ == "__main__":
    main()
