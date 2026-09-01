import argparse
import os
import time

import httpx
from psycopg2.extras import Json, execute_values

import core.storage as storage
from core import chain as h
from modules.orderbook import FILL_TOPIC, ORDERS_UPDATED_TOPIC

CHUNK = 100
CALLS_PER_BATCH = 10
GAP_TOLERANCE = 20


def main() -> None:
    parser = argparse.ArgumentParser(description="re-complete cache rows the sweep created before the refill")
    parser.add_argument("--rpc", default=os.environ.get("REPAIR_RPC", "https://rpc.monad.xyz"))
    parser.add_argument("--rps", type=float, default=4.0)
    parser.add_argument("--start-block", type=lambda x: int(x, 0), required=True)
    parser.add_argument("--end-block", type=lambda x: int(x, 0), required=True)
    parser.add_argument("--all-blocks", action="store_true")
    args = parser.parse_args()

    storage.init_pool()

    if args.all_blocks:
        targets = list(range(args.start_block, args.end_block + 1))
        _repair(args, targets)
        return

    with storage.db_cursor() as cur:
        cur.execute(
            """
            SELECT number FROM launchpad_block_logs
            WHERE number BETWEEN %s AND %s
              AND (logs @> %s::jsonb OR logs @> %s::jsonb)
            ORDER BY number
            """,
            (
                args.start_block,
                args.end_block,
                f'[{{"topics": ["{ORDERS_UPDATED_TOPIC}"]}}]',
                f'[{{"topics": ["{FILL_TOPIC}"]}}]',
            ),
        )
        targets = [int(r[0]) for r in cur.fetchall()]
    _repair(args, targets)


def _repair(args, targets: list[int]) -> None:
    if not targets:
        print("[REPAIR] nothing to repair", flush=True)
        return

    ranges: list[tuple[int, int]] = []
    lo = hi = targets[0]
    for n in targets[1:]:
        if n - hi <= GAP_TOLERANCE:
            hi = n
        else:
            ranges.append((lo, hi))
            lo = hi = n
    ranges.append((lo, hi))
    total = sum(e - s + 1 for s, e in ranges)
    print(f"[REPAIR] {len(targets)} target blocks -> {len(ranges)} ranges, {total:,} blocks to refetch", flush=True)

    chunks: list[tuple[int, int]] = []
    for r_start, r_end in ranges:
        for c_start in range(r_start, r_end + 1, CHUNK):
            chunks.append((c_start, min(c_start + CHUNK - 1, r_end)))

    client = httpx.Client(timeout=60.0)
    min_interval = 1.0 / args.rps
    last = 0.0
    done = 0
    merged = 0
    started = time.monotonic()
    for gi in range(0, len(chunks), CALLS_PER_BATCH):
        group = chunks[gi : gi + CALLS_PER_BATCH]
        payload = [
            {
                "jsonrpc": "2.0",
                "id": i,
                "method": "eth_getLogs",
                "params": [{"fromBlock": hex(f), "toBlock": hex(t), "topics": [h.TOPICS]}],
            }
            for i, (f, t) in enumerate(group)
        ]

        results = None
        for attempt in range(5):
            wait = min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.monotonic()
            try:
                resp = client.post(args.rpc, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, list) or any("error" in d for d in data):
                    raise RuntimeError("batch error")
                results = {d["id"]: d.get("result") or [] for d in data}
                break
            except Exception as e:
                print(f"[REPAIR] group at {group[0][0]} attempt {attempt + 1}/5 failed ({e!r})", flush=True)
                time.sleep(2.0 * (attempt + 1))
        if results is None:
            raise RuntimeError(f"repair failed for group at {group[0][0]}")

        by_block: dict[int, list[dict]] = {}
        for i in range(len(group)):
            for log in results.get(i) or []:
                raw = log.get("blockNumber")
                blk = int(raw, 16) if isinstance(raw, str) else int(raw or 0)
                by_block.setdefault(blk, []).append(log)

        if by_block:
            with storage.db_cursor() as cur:
                cur.execute(
                    "SELECT number, logs FROM launchpad_block_logs WHERE number IN %s",
                    (tuple(by_block.keys()),),
                )
                existing = {int(n): logs or [] for n, logs in cur.fetchall()}
                merge_rows = []
                for blk, logs in by_block.items():
                    seen = {
                        ((lg.get("transactionHash") or "").lower(), str(lg.get("logIndex") or ""))
                        for lg in existing.get(blk, [])
                    }
                    fresh = [
                        lg
                        for lg in logs
                        if ((lg.get("transactionHash") or "").lower(), str(lg.get("logIndex") or "")) not in seen
                    ]
                    if fresh:
                        merge_rows.append((int(blk), Json(fresh)))
                if merge_rows:
                    execute_values(
                        cur,
                        """
                        INSERT INTO launchpad_block_logs (number, logs) VALUES %s
                        ON CONFLICT (number) DO UPDATE SET logs = launchpad_block_logs.logs || EXCLUDED.logs
                        """,
                        merge_rows,
                    )
                    merged += len(merge_rows)

        done += sum(t - f + 1 for f, t in group)
        if gi % (CALLS_PER_BATCH * 5) < CALLS_PER_BATCH:
            rate = done / max(time.monotonic() - started, 0.001)
            eta = (total - done) / max(rate, 0.001)
            print(f"[REPAIR] {done:,}/{total:,} blocks, {merged:,} rows gained logs ({rate:,.0f}/s, eta {eta / 60:.0f}m)", flush=True)

    print(f"[REPAIR] complete: {merged:,} cache rows regained missing logs", flush=True)


if __name__ == "__main__":
    main()
