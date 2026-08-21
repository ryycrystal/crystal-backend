import argparse
import asyncio
import time

import httpx

import core.storage as storage
from core import chain as h
from state import RPC_HTTP

BUCKET = 10_000_000
DEFAULT_RPS = 10.0
DEFAULT_BATCH = 100
REFILL_CALLS_PER_BATCH = 10
HEAD_LAG_LIMIT = 100


# minimal rate limited json rpc client for checks and refills
class Rpc:
    def __init__(self, url: str, rps: float, timeout: float = 30.0) -> None:
        self.url = url
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self.last_request = 0.0
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def _pace(self) -> None:
        wait = self.min_interval - (time.monotonic() - self.last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        self.last_request = time.monotonic()

    async def call(self, method: str, params: list) -> dict:
        await self._pace()
        resp = await self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data)
        return data

    async def batch(self, calls: list[tuple[str, list]]) -> list[dict]:
        await self._pace()
        payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
        resp = await self.client.post(self.url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise RuntimeError(data)
        by_id = {d.get("id"): d for d in data}
        return [by_id.get(i) or {} for i in range(len(calls))]


# current chain head
async def rpc_head(rpc: Rpc) -> int:
    data = await rpc.call("eth_blockNumber", [])
    return int(data["result"], 16)


# logs for a block range filtered to the indexed topic set
async def rpc_logs(rpc: Rpc, frm: int, to: int) -> list[dict]:
    data = await rpc.call("eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to), "topics": [h.TOPICS]}])
    return data.get("result") or []


# processed and cached table bounds and row counts
def fetch_bounds() -> dict:
    with storage.db_cursor() as cur:
        cur.execute("SELECT MIN(number), MAX(number), COUNT(*) FROM launchpad_blocks")
        p_min, p_max, p_count = cur.fetchone()
        cur.execute("SELECT MIN(number), MAX(number), COUNT(*) FROM launchpad_block_logs")
        c_min, c_max, c_count = cur.fetchone()
    return {"p_min": p_min, "p_max": p_max, "p_count": p_count, "c_min": c_min, "c_max": c_max, "c_count": c_count}


# per bucket processed and cached counts
def fetch_density() -> list[tuple[int, int, int]]:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            SELECT (b.number / %s) * %s AS bucket, COUNT(*) AS processed, COUNT(l.number) AS cached
            FROM launchpad_blocks b
            LEFT JOIN launchpad_block_logs l ON l.number = b.number
            GROUP BY 1
            ORDER BY 1
            """,
            (BUCKET, BUCKET),
        )
        return [(int(b), int(p), int(c)) for b, p, c in cur.fetchall()]


# contiguous gaps in the processed block sequence
def fetch_processed_gaps() -> list[tuple[int, int]]:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            SELECT number + 1, next_number - 1
            FROM (SELECT number, LEAD(number) OVER (ORDER BY number) AS next_number FROM launchpad_blocks) t
            WHERE next_number > number + 1
            ORDER BY 1
            """
        )
        return [(int(a), int(b)) for a, b in cur.fetchall()]


# contiguous ranges of processed blocks that have no cached log row
def fetch_cache_holes(start_block: int | None = None, end_block: int | None = None) -> list[tuple[int, int]]:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            WITH holes AS (
                SELECT b.number
                FROM launchpad_blocks b
                LEFT JOIN launchpad_block_logs l ON l.number = b.number
                WHERE l.number IS NULL
                  AND b.number >= COALESCE(%s, b.number)
                  AND b.number <= COALESCE(%s, b.number)
            ),
            grouped AS (
                SELECT number, number - ROW_NUMBER() OVER (ORDER BY number) AS grp FROM holes
            )
            SELECT MIN(number), MAX(number) FROM grouped GROUP BY grp ORDER BY 1
            """,
            (start_block, end_block),
        )
        return [(int(a), int(b)) for a, b in cur.fetchall()]


# tokens referenced by trades or positions that have no token row
def fetch_orphans() -> tuple[int, int]:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT t.token)
            FROM launchpad_trades t
            LEFT JOIN launchpad_tokens k ON k.token = t.token
            WHERE k.token IS NULL
            """
        )
        trade_orphans = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT COUNT(DISTINCT p.token)
            FROM launchpad_positions p
            LEFT JOIN launchpad_tokens k ON k.token = p.token
            WHERE k.token IS NULL
            """
        )
        pos_orphans = int(cur.fetchone()[0])
    return trade_orphans, pos_orphans


# random cached block numbers per bucket for spot verification
def sample_cached_blocks(per_bucket: int) -> list[int]:
    out: list[int] = []
    with storage.db_cursor() as cur:
        cur.execute("SELECT DISTINCT number / %s FROM launchpad_block_logs", (BUCKET,))
        buckets = [int(r[0]) for r in cur.fetchall()]
        for b in buckets:
            cur.execute(
                "SELECT number FROM launchpad_block_logs WHERE number >= %s AND number < %s ORDER BY random() LIMIT %s",
                (b * BUCKET, (b + 1) * BUCKET, per_bucket),
            )
            out.extend(int(r[0]) for r in cur.fetchall())
    return out


# identity of one log for cache versus chain comparison
def log_key(log: dict) -> tuple:
    idx_raw = log.get("logIndex")
    idx = int(idx_raw, 16) if isinstance(idx_raw, str) else int(idx_raw or 0)
    topic0 = str((log.get("topics") or [""])[0]).lower()
    return (str(log.get("transactionHash") or "").lower(), idx, topic0)


# print the integrity report and count problems
async def run_check(rpc: Rpc, verbose_ranges: int) -> int:
    problems = 0
    b = fetch_bounds()
    if not b["p_count"]:
        print("[DOCTOR] launchpad_blocks is empty: nothing indexed yet", flush=True)
        return 1
    print(f"[DOCTOR] processed: {b['p_min']:,} -> {b['p_max']:,} ({b['p_count']:,} rows)", flush=True)
    print(f"[DOCTOR] cached:    {b['c_min'] or 0:,} -> {b['c_max'] or 0:,} ({b['c_count']:,} rows)", flush=True)

    try:
        head = await rpc_head(rpc)
        lag = head - int(b["p_max"])
        if lag >= HEAD_LAG_LIMIT:
            problems += 1
            print(f"[DOCTOR] chain head: {head:,} (lag {lag:,} blocks) <- INDEXER BEHIND", flush=True)
        else:
            print(f"[DOCTOR] chain head: {head:,} (lag {lag:,} blocks)", flush=True)
    except Exception as e:
        print(f"[DOCTOR] chain head check skipped ({e!r})", flush=True)

    gaps = fetch_processed_gaps()
    gap_total = sum(e - s + 1 for s, e in gaps)
    if gaps:
        problems += 1
        print(f"[DOCTOR] processed gaps: {gap_total:,} blocks in {len(gaps)} ranges <- EVENTS NEVER INDEXED", flush=True)
        for s, e in sorted(gaps, key=lambda r: r[0] - r[1])[:verbose_ranges]:
            print(f"[DOCTOR]   gap {s:,} -> {e:,} ({e - s + 1:,} blocks)", flush=True)
    else:
        print("[DOCTOR] processed gaps: none", flush=True)

    holes = fetch_cache_holes()
    hole_total = sum(e - s + 1 for s, e in holes)
    if holes:
        problems += 1
        print(
            f"[DOCTOR] cache holes: {hole_total:,} blocks in {len(holes)} ranges "
            "<- A CLEAN REINDEX WOULD DROP THEIR EVENTS (run --refill)",
            flush=True,
        )
        for s, e in sorted(holes, key=lambda r: r[0] - r[1])[:verbose_ranges]:
            print(f"[DOCTOR]   hole {s:,} -> {e:,} ({e - s + 1:,} blocks)", flush=True)
    else:
        print("[DOCTOR] cache holes: none", flush=True)

    print("[DOCTOR] cache density per 10m blocks:", flush=True)
    for bucket, processed, cached in fetch_density():
        pct = 100.0 * cached / processed if processed else 0.0
        print(f"[DOCTOR]   {bucket:,}: {cached:,}/{processed:,} cached ({pct:.1f}%)", flush=True)

    trade_orphans, pos_orphans = fetch_orphans()
    if trade_orphans or pos_orphans:
        problems += 1
        print(
            f"[DOCTOR] orphans: {trade_orphans} trade tokens and {pos_orphans} position tokens "
            "missing from launchpad_tokens <- DERIVED STATE LOST THEIR CREATES",
            flush=True,
        )
    else:
        print("[DOCTOR] orphans: none", flush=True)

    return problems


# refill uncached processed ranges from rpc so a reindex stops losing data. the
# chunks ride json-rpc batches, because one wide-topic getLogs takes seconds on
# the public rpc and a chunk at a time makes big holes take days
async def run_refill(rpc: Rpc, batch: int, start_block: int | None = None, end_block: int | None = None) -> None:
    holes = fetch_cache_holes(start_block, end_block)
    if not holes:
        print("[DOCTOR] no cache holes to refill", flush=True)
        return
    total = sum(e - s + 1 for s, e in holes)
    print(f"[DOCTOR] refilling {total:,} blocks across {len(holes)} ranges", flush=True)

    chunks: list[tuple[int, int]] = []
    for r_start, r_end in holes:
        for c_start in range(r_start, r_end + 1, batch):
            chunks.append((c_start, min(c_start + batch - 1, r_end)))

    done = 0
    started = time.monotonic()
    for gi in range(0, len(chunks), REFILL_CALLS_PER_BATCH):
        group = chunks[gi : gi + REFILL_CALLS_PER_BATCH]
        calls = [
            ("eth_getLogs", [{"fromBlock": hex(f), "toBlock": hex(t), "topics": [h.TOPICS]}]) for f, t in group
        ]
        results = None
        for attempt in range(5):
            try:
                results = await rpc.batch(calls)
                for r in results:
                    if "error" in r:
                        raise RuntimeError(f"getLogs error: {r['error']}")
                break
            except Exception as e:
                print(f"[DOCTOR] group at {group[0][0]} failed ({e!r}), retry {attempt + 1}/5", flush=True)
                results = None
                await asyncio.sleep(2.0 * (attempt + 1))
        if results is None:
            raise RuntimeError(f"refill failed for group at {group[0][0]}")

        rows: dict[int, list[dict]] = {}
        for (c_start, c_end), res in zip(group, results):
            by_block: dict[int, list[dict]] = {}
            for log in res.get("result") or []:
                raw = log.get("blockNumber")
                blk = int(raw, 16) if isinstance(raw, str) else int(raw or 0)
                if c_start <= blk <= c_end:
                    by_block.setdefault(blk, []).append(log)
            rows.update({blk: by_block.get(blk, []) for blk in range(c_start, c_end + 1)})
            done += c_end - c_start + 1
        with storage.db_cursor() as cur:
            storage.write_block_logs_batch(rows, cur)

        if gi % (REFILL_CALLS_PER_BATCH * 5) < REFILL_CALLS_PER_BATCH:
            rate = done / max(time.monotonic() - started, 0.001)
            eta = (total - done) / max(rate, 0.001)
            print(f"[DOCTOR] refilled {done:,}/{total:,} blocks ({rate:,.0f}/s, eta {eta / 3600:.1f}h)", flush=True)
    print(f"[DOCTOR] refill complete: {done:,} blocks", flush=True)


# compare sampled cached blocks against fresh rpc logs
async def run_verify(rpc: Rpc, per_bucket: int) -> int:
    blocks = sample_cached_blocks(per_bucket)
    mismatches = 0
    for blk in sorted(blocks):
        cached = storage.get_block_logs(blk) or []
        fresh = await rpc_logs(rpc, blk, blk)
        if {log_key(x) for x in cached} != {log_key(x) for x in fresh}:
            mismatches += 1
            print(f"[DOCTOR] cache mismatch at block {blk}: cached {len(cached)} logs, rpc {len(fresh)}", flush=True)
    print(f"[DOCTOR] verified {len(blocks)} sampled blocks, {mismatches} mismatches", flush=True)
    return mismatches


# entrypoint
async def main() -> None:
    parser = argparse.ArgumentParser(description="detect missing indexer data and refill the raw log cache")
    parser.add_argument("--rpc", default=RPC_HTTP)
    parser.add_argument("--rps", type=float, default=DEFAULT_RPS)
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="blocks per refill eth_getLogs request")
    parser.add_argument("--ranges", type=int, default=10, help="worst ranges to print per finding")
    parser.add_argument("--refill", action="store_true", help="fetch logs for cache holes over rpc and write them")
    parser.add_argument("--verify-rpc", type=int, default=0, help="sampled blocks per 10m bucket to check against rpc")
    parser.add_argument("--start-block", type=lambda x: int(x, 0), default=None, help="restrict refill to holes at or after this block")
    parser.add_argument("--end-block", type=lambda x: int(x, 0), default=None, help="restrict refill to holes at or before this block")
    args = parser.parse_args()

    storage.init_pool()
    rpc = Rpc(args.rpc, args.rps)
    try:
        if args.refill:
            await run_refill(rpc, args.batch, args.start_block, args.end_block)
        problems = await run_check(rpc, args.ranges)
        if args.verify_rpc > 0:
            problems += await run_verify(rpc, args.verify_rpc)
    finally:
        await rpc.close()

    if problems:
        print(f"[DOCTOR] verdict: {problems} problem(s) found", flush=True)
        raise SystemExit(1)
    print("[DOCTOR] verdict: healthy", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
