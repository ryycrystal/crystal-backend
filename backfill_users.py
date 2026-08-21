import argparse
import os

import httpx

import core.storage as storage
from core import chain as h
from modules.orderbook import USER_REGISTERED_TOPIC, parse_user_registered


# the user registry is tiny (one log per wallet ever), so one wide getLogs from
# an archive rpc replays the whole thing. re-runs converge on the primary key
def main() -> None:
    parser = argparse.ArgumentParser(description="backfill the on-chain user registry")
    parser.add_argument("--rpc", default=os.environ.get("USERS_RPC", "https://rpc.monad.xyz"))
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
                    "topics": [USER_REGISTERED_TOPIC],
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
        parsed = parse_user_registered((log.get("address") or "").lower(), log.get("topics") or [], "")
        if not parsed:
            continue
        blk_raw = log.get("blockNumber")
        blk = int(blk_raw, 16) if isinstance(blk_raw, str) else int(blk_raw or 0)
        ts_raw = log.get("blockTimestamp")
        ts = int(ts_raw, 16) if isinstance(ts_raw, str) else int(ts_raw or 0)
        storage.insert_crystal_user(parsed, blk, ts)
        inserted += 1
        print(f"[USERS] id {parsed['user_id']} -> {parsed['user']} (margin={parsed['is_margin']})", flush=True)

    print(f"[USERS] complete: {inserted} registrations", flush=True)


if __name__ == "__main__":
    main()
