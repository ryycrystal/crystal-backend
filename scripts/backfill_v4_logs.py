"""Fetch Uniswap V4 PoolManager logs (Swap, Initialize) for chosen blocks into a local store.

The log cache only holds logs whose topics were indexed when the block was
ingested, and the V4 topics were added on 2026-09-05, so older blocks have no
PoolManager logs at all. The side replay then has to impute every V4 leg. This
fills the gap for exactly the blocks that need it: every block where a side
replay wrote a reconciliation leg, or an explicit block list (for example the
blocks where a tracked token's transfer touches the PoolManager).

Blocks are fetched in exact ranges no wider than 100 blocks (the RPC's getLogs
limit), and only the wanted blocks' logs are kept, because the PoolManager is
busy and a full window carries ~150 logs of other pools.

Logs go to STORE_URL (a local database), table v4_logs(number, logs), and the
replay merges them into each chunk with --extra-logs-url. Prod is not touched.
Resumable: fetched blocks are recorded in v4_done (and any block inside a
window recorded in v4_windows by an earlier run counts as done).

    STORE_URL=postgresql://...@localhost/crystal_v4_logs \\
      python scripts/backfill_v4_logs.py --side-db postgresql://.../crystal_replay [--side-db ...] [--rps 40]
      python scripts/backfill_v4_logs.py --blocks-file blocks.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

import aiohttp
import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.chain as h  # noqa: E402
from modules.univ4 import V4_INITIALIZE_TOPIC, V4_SWAP_TOPIC  # noqa: E402

SPAN = 100
FIRST_CACHED_V4_BLOCK = 102_079_138
RPC = os.environ.get("RPC_HTTP", "https://rpc.monad.xyz")


def reconciliation_blocks(url: str, before: int) -> set[int]:
    with psycopg2.connect(url) as c, c.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT block_number FROM launchpad_trades WHERE venue='reconciliation' AND block_number < %s",
            (before,),
        )
        return {int(r[0]) for r in cur.fetchall()}


def already_done(store, blocks: set[int]) -> set[int]:
    with store.cursor() as cur:
        cur.execute("SELECT number FROM v4_done")
        done = {int(r[0]) for r in cur.fetchall()}
        cur.execute("SELECT win FROM v4_windows")
        windows = {int(r[0]) for r in cur.fetchall()}
    return done | {b for b in blocks if b // SPAN in windows}


def ranges(blocks: list[int]) -> list[tuple[int, int, list[int]]]:
    out: list[tuple[int, int, list[int]]] = []
    i = 0
    while i < len(blocks):
        start = blocks[i]
        j = i
        while j + 1 < len(blocks) and blocks[j + 1] <= start + SPAN - 1:
            j += 1
        out.append((start, blocks[j], blocks[i : j + 1]))
        i = j + 1
    return out


class Limiter:
    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._next = max(self._next, now) + self._interval
            delay = self._next - self._interval - now
        if delay > 0:
            await asyncio.sleep(delay)


async def fetch_range(session, limiter, start: int, end: int, attempts: int = 6) -> list[dict]:
    body = {
        "jsonrpc": "2.0",
        "id": start,
        "method": "eth_getLogs",
        "params": [
            {
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "address": h.UNIV4_POOL_MANAGER_ADDR,
                "topics": [[V4_SWAP_TOPIC, V4_INITIALIZE_TOPIC]],
            }
        ],
    }
    for attempt in range(attempts):
        await limiter.wait()
        try:
            async with session.post(RPC, json=body, timeout=aiohttp.ClientTimeout(total=60)) as r:
                out = await r.json()
            if "error" in out:
                raise RuntimeError(str(out["error"])[:120])
            return out["result"]
        except Exception:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(min(2**attempt, 30))
    return []


def store_batch(store, results: list[tuple[list[int], list[dict]]]) -> tuple[int, int]:
    by_block: dict[int, list[dict]] = {}
    done: list[tuple[int]] = []
    for wanted, logs in results:
        keep = set(wanted)
        done.extend((b,) for b in wanted)
        for log in logs:
            n = int(log["blockNumber"], 16)
            if n in keep:
                by_block.setdefault(n, []).append(log)
    with store.cursor() as cur:
        if by_block:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO v4_logs (number, logs) VALUES %s ON CONFLICT (number) DO UPDATE SET logs = EXCLUDED.logs",
                [(n, json.dumps(logs)) for n, logs in by_block.items()],
                page_size=500,
            )
        psycopg2.extras.execute_values(cur, "INSERT INTO v4_done (number) VALUES %s ON CONFLICT DO NOTHING", done)
    store.commit()
    return len(by_block), sum(len(v) for v in by_block.values())


async def run(work: list[tuple[int, int, list[int]]], store, rps: float, concurrency: int) -> None:
    limiter = Limiter(rps)
    sem = asyncio.Semaphore(concurrency)
    t0 = time.time()
    done_ranges = 0
    blocks_with_logs = 0
    logs_total = 0
    total_blocks = sum(len(w) for _, _, w in work)
    done_blocks = 0

    async def one(session, item):
        start, end, wanted = item
        async with sem:
            return wanted, await fetch_range(session, limiter, start, end)

    async with aiohttp.ClientSession() as session:
        for i in range(0, len(work), 200):
            batch = work[i : i + 200]
            results = await asyncio.gather(*(one(session, item) for item in batch))
            b, n = store_batch(store, results)
            blocks_with_logs += b
            logs_total += n
            done_ranges += len(batch)
            done_blocks += sum(len(w) for _, _, w in batch)
            rate = done_ranges / max(time.time() - t0, 0.001)
            print(
                f"[V4] {done_ranges:,}/{len(work):,} ranges, {done_blocks:,}/{total_blocks:,} blocks "
                f"({rate:.0f} req/s, eta {(len(work) - done_ranges) / max(rate, 0.001) / 60:.0f}m) "
                f"{blocks_with_logs:,} blocks got logs, {logs_total:,} logs",
                flush=True,
            )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--side-db", action="append", default=[], help="side database whose reconciliation legs choose the blocks"
    )
    ap.add_argument("--blocks-file")
    ap.add_argument("--before", type=int, default=FIRST_CACHED_V4_BLOCK)
    ap.add_argument("--rps", type=float, default=40)
    ap.add_argument("--concurrency", type=int, default=24)
    args = ap.parse_args()

    blocks: set[int] = set()
    for url in args.side_db:
        found = reconciliation_blocks(url, args.before)
        print(f"[V4] {len(found):,} reconciliation blocks from {url.rsplit('/', 1)[1].split('?')[0]}", flush=True)
        blocks |= found
    if args.blocks_file:
        blocks |= {int(b) for b in json.load(open(args.blocks_file)) if int(b) < args.before}
    if not blocks:
        raise SystemExit("no blocks selected")

    store_url = os.environ["STORE_URL"]
    if "crystal_v4_logs" not in store_url:
        raise SystemExit("STORE_URL must point at the local v4 log store")
    store = psycopg2.connect(store_url)
    with store.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS v4_logs (number BIGINT PRIMARY KEY, logs JSONB NOT NULL)")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS v4_windows (win BIGINT PRIMARY KEY, fetched_at TIMESTAMPTZ DEFAULT now())"
        )
        cur.execute("CREATE TABLE IF NOT EXISTS v4_done (number BIGINT PRIMARY KEY)")
    store.commit()

    pending = sorted(blocks - already_done(store, blocks))
    work = ranges(pending)
    print(
        f"[V4] {len(blocks):,} blocks, {len(pending):,} still to fetch in {len(work):,} ranges at {args.rps:g} rps",
        flush=True,
    )
    if work:
        asyncio.run(run(work, store, args.rps, args.concurrency))
    print("[V4] done", flush=True)


if __name__ == "__main__":
    main()
