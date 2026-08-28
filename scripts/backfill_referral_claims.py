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
    parser.add_argument("--chunk", type=lambda x: int(x, 0), default=1_000_000)
    args = parser.parse_args()

    storage.init_pool()
    router = h.CONTRACTS["ROUTER"]

    with httpx.Client(timeout=60.0) as client:
        head_resp = client.post(
            args.rpc,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        )
        head_resp.raise_for_status()
        head = int(head_resp.json()["result"], 16)

        logs: list[dict] = []
        step = args.chunk
        lo = args.start_block
        while lo <= head:
            hi = min(lo + step - 1, head)
            resp = client.post(
                args.rpc,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_getLogs",
                    "params": [
                        {
                            "fromBlock": hex(lo),
                            "toBlock": hex(hi),
                            "address": router,
                            "topics": [CLAIM_TOPIC],
                        }
                    ],
                },
            )
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                raise RuntimeError(body["error"])
            batch = body.get("result") or []
            logs.extend(batch)
            print(f"[CLAIMS] scanned {lo}..{hi}: {len(batch)} events", flush=True)
            lo = hi + 1

    inserted = 0
    for log in logs:
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
