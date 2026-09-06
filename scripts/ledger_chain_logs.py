"""Fetch a token's own logs straight from the chain for a block range, 100 blocks per request.

Prod's log cache only holds logs for addresses the indexer was tracking when it fetched the block, so a token
registered late (a nad.fun token registered at migration) has a history the cache never saw. This script pulls
that history from the public RPC, which caps eth_getLogs at 100 blocks and rejects topic filters, and writes
{block: [log, ...]} JSON that scripts/ledger_replay.py merges through --chain-logs-file.

  python scripts/ledger_chain_logs.py --address 0xTOKEN --from 85000000 --to 85819843 --out james_curve.json
  python scripts/ledger_chain_logs.py --address 0xTOKEN --to 85819843 --until-mint --floor 74700000 --out ...

--until-mint walks backwards from --to and stops once it has seen a Transfer from the zero address (the mint)
followed by --quiet-windows empty windows, or when it reaches --floor.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

WINDOW = 100
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_WORD = "0x" + "0" * 64


def get_logs(rpc: str, addresses: list[str], lo: int, hi: int, attempts: int = 6) -> list[dict]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getLogs",
        "params": [{"address": addresses, "fromBlock": hex(lo), "toBlock": hex(hi)}],
    }
    body = json.dumps(payload).encode()
    delay = 1.0
    last = None
    for _ in range(attempts):
        req = urllib.request.Request(rpc, data=body, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                reply = json.loads(resp.read())
            if isinstance(reply, dict) and isinstance(reply.get("result"), list):
                return reply["result"]
            last = reply.get("error") if isinstance(reply, dict) else reply
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
        time.sleep(delay)
        delay = min(delay * 2, 20)
    raise RuntimeError(f"eth_getLogs {lo}-{hi} failed: {str(last)[:160]}")


def is_mint(log: dict) -> bool:
    topics = log.get("topics") or []
    return len(topics) >= 3 and str(topics[0]).lower() == TRANSFER_TOPIC and str(topics[1]).lower() == ZERO_WORD


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", action="append", required=True, help="contract address to fetch logs for (repeatable)")
    ap.add_argument("--from", dest="from_block", type=int, help="first block (forward mode)")
    ap.add_argument("--to", dest="to_block", type=int, required=True, help="last block, inclusive")
    ap.add_argument("--until-mint", action="store_true", help="walk backwards from --to until the mint is behind us")
    ap.add_argument("--floor", type=int, default=0, help="lowest block to visit in backward mode")
    ap.add_argument("--quiet-windows", type=int, default=50, help="empty windows to see after the mint before stopping")
    ap.add_argument("--rps", type=float, default=8.0, help="requests per second")
    ap.add_argument("--rpc", default=os.environ.get("RPC_HTTP", "https://rpc.monad.xyz"))
    ap.add_argument("--out", required=True, help="JSON file of {block: [logs]}; an existing file is extended")
    args = ap.parse_args()

    addresses = [a.lower() for a in args.address]
    found: dict[int, list[dict]] = {}
    if os.path.exists(args.out):
        found = {int(k): v for k, v in json.load(open(args.out)).items()}
        print(f"[CHAIN] {len(found):,} blocks already in {args.out}", flush=True)
    interval = 1.0 / max(args.rps, 0.1)
    requests = 0
    started = time.time()
    mint_seen = False
    quiet_after_mint = 0

    def save() -> None:
        json.dump({str(k): v for k, v in sorted(found.items())}, open(args.out, "w"))

    if args.until_mint:
        hi = args.to_block
        while hi >= args.floor:
            lo = max(hi - WINDOW + 1, args.floor)
            logs = get_logs(args.rpc, addresses, lo, hi)
            requests += 1
            for log in logs:
                blk = int(str(log.get("blockNumber")), 16)
                found.setdefault(blk, []).append(log)
                if is_mint(log):
                    mint_seen = True
                    print(f"[CHAIN] mint seen in block {blk:,}", flush=True)
            if mint_seen:
                quiet_after_mint = quiet_after_mint + 1 if not logs else 0
                if quiet_after_mint >= args.quiet_windows:
                    print(f"[CHAIN] {args.quiet_windows} empty windows below the mint; stopping at {lo:,}", flush=True)
                    break
            if requests % 200 == 0:
                rate = requests / max(time.time() - started, 0.001)
                print(f"[CHAIN] down to {lo:,}: {len(found):,} blocks with logs, {rate:.1f} req/s", flush=True)
                save()
            hi = lo - 1
            time.sleep(interval)
    else:
        if args.from_block is None:
            raise SystemExit("--from is required unless --until-mint is given")
        lo = args.from_block
        while lo <= args.to_block:
            hi = min(lo + WINDOW - 1, args.to_block)
            logs = get_logs(args.rpc, addresses, lo, hi)
            requests += 1
            for log in logs:
                blk = int(str(log.get("blockNumber")), 16)
                found.setdefault(blk, []).append(log)
            if requests % 200 == 0:
                rate = requests / max(time.time() - started, 0.001)
                print(f"[CHAIN] up to {hi:,}: {len(found):,} blocks with logs, {rate:.1f} req/s", flush=True)
                save()
            lo = hi + 1
            time.sleep(interval)

    # de-duplicate in case an existing file overlapped the range
    for blk, logs in found.items():
        seen: set[tuple[str, str]] = set()
        unique = []
        for log in logs:
            key = (str(log.get("transactionHash")).lower(), str(log.get("logIndex")).lower())
            if key not in seen:
                seen.add(key)
                unique.append(log)
        found[blk] = unique
    save()
    total = sum(len(v) for v in found.values())
    print(f"[CHAIN] done: {requests:,} requests, {len(found):,} blocks, {total:,} logs -> {args.out}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
