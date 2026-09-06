"""Fetch a contract's own logs straight from the chain for a block range, 100 blocks per window.

Prod's log cache only holds logs for addresses the indexer was tracking when it fetched the block, so a token
registered late (a nad.fun token registered at migration) has a history the cache never saw. This script pulls
that history from the public RPC, which caps eth_getLogs at 100 blocks, rejects topic filters, and allows
JSON-RPC batches of a handful of windows per request. It writes {block: [log, ...]} JSON that
scripts/ledger_replay.py merges through --chain-logs-file.

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


def get_logs_batch(
    rpc: str, addresses: list[str], windows: list[tuple[int, int]], attempts: int = 8
) -> list[list[dict]]:
    """One HTTP request per call; windows that fail (rate limit, transport) are retried with backoff."""
    results: list[list[dict] | None] = [None] * len(windows)
    pending = list(range(len(windows)))
    delay = 1.0
    last = None
    for _ in range(attempts):
        if not pending:
            break
        payload = [
            {
                "jsonrpc": "2.0",
                "id": i,
                "method": "eth_getLogs",
                "params": [{"address": addresses, "fromBlock": hex(windows[i][0]), "toBlock": hex(windows[i][1])}],
            }
            for i in pending
        ]
        req = urllib.request.Request(
            rpc, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                replies = json.loads(resp.read())
            if isinstance(replies, dict):
                replies = [replies]
            by_id = {r.get("id"): r for r in replies if isinstance(r, dict)}
            retry = []
            for i in pending:
                reply = by_id.get(i)
                if reply is not None and isinstance(reply.get("result"), list):
                    results[i] = reply["result"]
                else:
                    last = reply.get("error") if reply else "missing reply"
                    retry.append(i)
            pending = retry
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
        if pending:
            time.sleep(delay)
            delay = min(delay * 2, 30)
    if pending:
        raise RuntimeError(f"eth_getLogs kept failing for {len(pending)} windows: {str(last)[:160]}")
    return [r or [] for r in results]


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
    ap.add_argument("--batch", type=int, default=10, help="windows per HTTP request")
    ap.add_argument("--rps", type=float, default=2.0, help="HTTP requests per second")
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

    def save() -> None:
        json.dump({str(k): v for k, v in sorted(found.items())}, open(args.out, "w"))

    def absorb(logs: list[dict]) -> bool:
        minted = False
        for log in logs:
            blk = int(str(log.get("blockNumber")), 16)
            found.setdefault(blk, []).append(log)
            if is_mint(log):
                minted = True
                print(f"[CHAIN] mint seen in block {blk:,}", flush=True)
        return minted

    def progress(edge: int) -> None:
        rate = requests / max(time.time() - started, 0.001)
        print(f"[CHAIN] at {edge:,}: {len(found):,} blocks with logs, {rate:.1f} req/s", flush=True)
        save()

    if args.until_mint:
        hi = args.to_block
        mint_seen = False
        quiet_after_mint = 0
        while hi >= args.floor:
            windows = []
            for _ in range(args.batch):
                if hi < args.floor:
                    break
                lo = max(hi - WINDOW + 1, args.floor)
                windows.append((lo, hi))
                hi = lo - 1
            for logs in get_logs_batch(args.rpc, addresses, windows):  # newest window first
                if absorb(logs):
                    mint_seen = True
                if mint_seen:
                    quiet_after_mint = quiet_after_mint + 1 if not logs else 0
            requests += 1
            if mint_seen and quiet_after_mint >= args.quiet_windows:
                print(
                    f"[CHAIN] {args.quiet_windows} empty windows below the mint; stopping at {windows[-1][0]:,}",
                    flush=True,
                )
                break
            if requests % 50 == 0:
                progress(windows[-1][0])
            time.sleep(interval)
    else:
        if args.from_block is None:
            raise SystemExit("--from is required unless --until-mint is given")
        lo = args.from_block
        while lo <= args.to_block:
            windows = []
            for _ in range(args.batch):
                if lo > args.to_block:
                    break
                hi = min(lo + WINDOW - 1, args.to_block)
                windows.append((lo, hi))
                lo = hi + 1
            for logs in get_logs_batch(args.rpc, addresses, windows):
                absorb(logs)
            requests += 1
            if requests % 50 == 0:
                progress(windows[-1][1])
            time.sleep(interval)

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
