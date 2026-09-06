"""Replay chosen tokens' history into an isolated database, reading the log cache from prod.

The live replay tool layers onto existing rows and skips trades it already has,
which cannot fix a position that sold against an understated basis. This one
starts from EMPTY derived state, so every position folds from scratch in block
order through the current attribution code, and prod is never written.

Only blocks that carry a log for one of the addresses are visited. An empty
block is a no-op for the sequencer, so walking the full block range (which the
live process_chunk does, and which also records every block as processed) is
pure cost here: a sparse token spans tens of millions of blocks for a few
hundred thousand hot ones.

Derived state goes to DATABASE_URL (point it at the side database).
The raw log cache is read from prod through PROD_PG* (host/hostaddr/port/...).

    DATABASE_URL=postgresql://...@localhost/crystal_replay PROD_PGHOSTADDR=127.0.0.1 PROD_PGPORT=15433 \\
      python scripts/replay_side.py --wipe --fresh --address 0x405b... [--address ...] [--batch 500]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from replay_addresses import _filter_logs  # noqa: E402

import backfill  # noqa: E402
import core.chain as h  # noqa: E402
import core.storage as storage  # noqa: E402
from core.sequencer import SEQUENCER, BatchAccumulator  # noqa: E402

SEED_TABLES = (
    "launchpad_tokens",
    "launchpad_pools",
    "univ4_pools",
    "launchpad_meta",
    "nadfun_v2_tokens",
    "crystal_markets",
    "holder_denylist",
)
FETCH_ATTEMPTS = 100000
FETCH_TIMEOUT = 600
FETCH_SLICE = 25


def prod_conn():
    return psycopg2.connect(
        host=os.environ["PROD_PGHOST"],
        hostaddr=os.environ.get("PROD_PGHOSTADDR") or None,
        port=int(os.environ.get("PROD_PGPORT", "5432")),
        user=os.environ["PROD_PGUSER"],
        password=os.environ["PROD_PGPASSWORD"],
        dbname=os.environ["PROD_PGDATABASE"],
        sslmode="require",
        connect_timeout=30,
        keepalives=1,
        keepalives_idle=20,
        keepalives_interval=10,
        keepalives_count=3,
    )


class LogSource:
    def __init__(self) -> None:
        self._pc = None

    def conn(self):
        if self._pc is None or self._pc.closed:
            self._pc = prod_conn()
            self._pc.set_session(readonly=True, autocommit=True)
        return self._pc

    def _drop(self) -> None:
        try:
            if self._pc is not None:
                self._pc.close()
        except Exception:
            pass
        self._pc = None

    def fetch(self, numbers: list[int]) -> dict[int, list[dict]]:
        for attempt in range(FETCH_ATTEMPTS):
            watchdog = None
            try:
                conn = self.conn()
                watchdog = threading.Timer(FETCH_TIMEOUT, self._cancel, args=(conn,))
                watchdog.daemon = True
                watchdog.start()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT number, logs FROM launchpad_block_logs WHERE number = ANY(%s)",
                        (numbers,),
                    )
                    return {int(n): (json.loads(v) if isinstance(v, str) else v) for n, v in cur.fetchall()}
            except psycopg2.Error as e:
                if attempt < 5 or attempt % 20 == 0:
                    print(f"[FETCH] attempt {attempt + 1} failed: {e!r}"[:200], flush=True)
                self._drop()
                time.sleep(min(2**attempt, 30))
            finally:
                if watchdog is not None:
                    watchdog.cancel()
        raise RuntimeError("prod log fetch kept failing")

    @staticmethod
    def _cancel(conn) -> None:
        print(f"[FETCH] no answer in {FETCH_TIMEOUT}s, cancelling the stalled query", flush=True)
        try:
            conn.cancel()
        except Exception:
            pass


class ParallelFetcher:
    def __init__(self, streams: int) -> None:
        self._sources = [LogSource() for _ in range(streams)]
        self._pool = ThreadPoolExecutor(max_workers=streams)

    def fetch(self, numbers: list[int]) -> dict[int, list[dict]]:
        n = len(self._sources)
        slices = [numbers[i : i + FETCH_SLICE] for i in range(0, len(numbers), FETCH_SLICE)]

        def run(k: int) -> dict[int, list[dict]]:
            out: dict[int, list[dict]] = {}
            for part in slices[k::n]:
                out.update(self._sources[k].fetch(part))
            return out

        merged: dict[int, list[dict]] = {}
        for part in self._pool.map(run, range(n)):
            merged.update(part)
        return merged


def wait_for_extra_logs(conn, numbers: list[int], required: set[int]) -> None:
    needed = [b for b in numbers if b in required]
    if not needed:
        return
    waited = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM unnest(%s::bigint[]) b
                WHERE b IN (SELECT number FROM v4_done) OR b / 100 IN (SELECT win FROM v4_windows)
                """,
                (needed,),
            )
            have = cur.fetchone()[0]
        conn.rollback()
        if have >= len(needed):
            return
        if waited % 120 == 0:
            print(f"[EXTRA] waiting for the v4 backfill: {have}/{len(needed)} blocks of this chunk fetched", flush=True)
        time.sleep(10)
        waited += 10


def merge_extra_logs(conn, numbers: list[int], cached: dict[int, list[dict]]) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT number, logs FROM v4_logs WHERE number = ANY(%s)", (numbers,))
        rows = cur.fetchall()
    added = 0
    for number, extra in rows:
        number = int(number)
        logs = cached.get(number)
        if not logs:
            continue
        extra = json.loads(extra) if isinstance(extra, str) else extra
        seen = {((lg.get("transactionHash") or "").lower(), str(lg.get("logIndex"))) for lg in logs}
        stamp = logs[0].get("blockTimestamp")
        for lg in extra:
            key = ((lg.get("transactionHash") or "").lower(), str(lg.get("logIndex")))
            if key in seen:
                continue
            lg = dict(lg)
            if stamp is not None and lg.get("blockTimestamp") is None:
                lg["blockTimestamp"] = stamp
            logs.append(lg)
            seen.add(key)
            added += 1
    return added


def wipe_side() -> None:
    with storage.db_cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = [r[0] for r in cur.fetchall()]
        if tables:
            cur.execute("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
    print(f"[WIPE] truncated {len(tables)} side tables", flush=True)


def seed_from_prod(pc, seed_url: str | None = None) -> None:
    src = psycopg2.connect(seed_url) if seed_url else pc
    if seed_url:
        waited = 0
        while True:
            with src.cursor() as cur:
                cur.execute("SELECT count(*) FROM holder_denylist")
                ready = cur.fetchone()[0] > 0
            src.rollback()
            if ready:
                break
            if waited % 120 == 0:
                print("[SEED] waiting for the local seed snapshot to finish", flush=True)
            time.sleep(15)
            waited += 15
    with src.cursor() as pcur, storage.db_cursor() as lcur:
        for table in SEED_TABLES:
            try:
                pcur.execute(f"SELECT * FROM {table}")
            except Exception:
                src.rollback()
                print(f"[SEED] {table}: not in the seed source, skipped", flush=True)
                continue
            cols = [d[0] for d in pcur.description]
            rows = pcur.fetchall()
            lcur.execute(f"TRUNCATE {table}")
            if rows:
                psycopg2.extras.execute_values(
                    lcur,
                    f"INSERT INTO {table} ({','.join(cols)}) VALUES %s ON CONFLICT DO NOTHING",
                    rows,
                    page_size=2000,
                )
            print(f"[SEED] {table}: {len(rows):,} rows", flush=True)


def hot_blocks(pc, addresses: list[str]) -> list[int]:
    with pc.cursor() as cur:
        cur.execute("SET statement_timeout = '3600s'")
        cur.execute(
            """
            SELECT number FROM launchpad_block_logs b
            WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(b.logs) e WHERE e->>'address' = ANY(%s))
            ORDER BY number
            """,
            (addresses,),
        )
        return [int(r[0]) for r in cur.fetchall()]


def process_hot_blocks(blocks: list[int], logs_by_block: dict[int, list[dict]], cur) -> dict[str, int]:
    batch = BatchAccumulator()
    counts = {tag: 0 for tag in set(h.EVENT_SIGS.values())}
    for blk in blocks:
        SEQUENCER._process_block(
            blk,
            logs_by_block.get(blk, []),
            cur=cur,
            counts_out=counts,
            batch=batch,
            record_processed=False,
        )
        SEQUENCER._logs_by_block.pop(blk, None)
        SEQUENCER._ready_blocks.discard(blk)
        SEQUENCER._block_timestamps.pop(blk, None)
    batch.flush(cur)
    SEQUENCER._state.basis_clear_overlay()
    SEQUENCER._next_block = blocks[-1] + 1
    return counts


async def replay(
    addresses: list[str],
    batch: int,
    blocks_file: str | None,
    wipe: bool,
    streams: int,
    extra_url: str | None,
    required_file: str | None,
    seed_url: str | None,
) -> None:
    src = LogSource()
    if wipe:
        wipe_side()
    seed_from_prod(None if seed_url else src.conn(), seed_url)

    if blocks_file and os.path.exists(blocks_file):
        blocks = json.load(open(blocks_file))
        print(f"[REPLAY] {len(blocks):,} hot blocks loaded from {blocks_file}", flush=True)
    else:
        t0 = time.time()
        blocks = hot_blocks(src.conn(), addresses)
        print(f"[REPLAY] {len(blocks):,} hot blocks found in {time.time() - t0:.0f}s", flush=True)
        if blocks_file:
            json.dump(blocks, open(blocks_file, "w"))
    if not blocks:
        return

    SEQUENCER._state.rebuild_from_db()
    SEQUENCER.reset_pending(blocks[0])

    groups = [blocks[i : i + batch] for i in range(0, len(blocks), batch)]
    prefetch = ParallelFetcher(streams)
    extra_conn = psycopg2.connect(extra_url) if extra_url else None
    required = {int(b) for b in json.load(open(required_file))} if required_file else set()
    extra_total = 0
    pool = ThreadPoolExecutor(max_workers=1)
    pending = pool.submit(prefetch.fetch, groups[0])

    done = 0
    t0 = time.time()
    for gi, group in enumerate(groups):
        cached = pending.result()
        if gi + 1 < len(groups):
            pending = pool.submit(prefetch.fetch, groups[gi + 1])
        if extra_conn is not None:
            wait_for_extra_logs(extra_conn, group, required)
            extra_total += merge_extra_logs(extra_conn, group, cached)
        await backfill.ensure_block_timestamps(cached)
        filtered = _filter_logs(group, cached)
        with storage.db_cursor() as cur:
            counts = process_hot_blocks(group, filtered, cur)
        done += len(group)
        if gi % 10 == 0 or done == len(blocks):
            rate = done / max(time.time() - t0, 0.001)
            eta = (len(blocks) - done) / max(rate, 0.001)
            seen = " ".join(f"{k} {v}" for k, v in sorted(counts.items()) if v)
            print(
                f"[REPLAY] {done:,}/{len(blocks):,} hot blocks ({rate:.0f}/s, eta {eta / 60:.0f}m) "
                f"last chunk {group[0]}-{group[-1]}: {seen}"
                + (f" | extra v4 logs so far {extra_total:,}" if extra_conn else ""),
                flush=True,
            )

    pool.shutdown()
    print(f"[REPLAY] committed to side db after {time.time() - t0:.0f}s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", action="append", default=[])
    ap.add_argument("--addresses-file")
    ap.add_argument("--blocks-file")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--streams", type=int, default=4, help="parallel prod connections per chunk fetch")
    ap.add_argument("--extra-logs-url", help="local store of backfilled PoolManager logs (scripts/backfill_v4_logs.py)")
    ap.add_argument("--extra-required-file", help="blocks that must be in the store before their chunk is folded")
    ap.add_argument("--seed-from", help="copy the seed tables from this database instead of prod")
    ap.add_argument("--wipe", action="store_true", help="truncate every table in the side db before seeding")
    ap.add_argument("--fresh", action="store_true", help="side db starts empty: skip per-trade existence checks")
    args = ap.parse_args()

    if args.fresh:
        import core.storage.launchpad as lp_storage

        lp_storage.trade_exists = lambda *_a, **_k: False
        storage.trade_exists = lambda *_a, **_k: False

    addresses = [a.lower() for a in args.address]
    if args.addresses_file:
        addresses += [a.lower() for a in json.load(open(args.addresses_file))]
    addresses = list(dict.fromkeys(addresses))
    if not addresses:
        raise SystemExit("pass --address or --addresses-file")

    url = os.environ.get("DATABASE_URL", "")
    if "crystal_replay" not in url:
        raise SystemExit(f"DATABASE_URL must point at the side database, got {url[:40]!r}")

    storage.init_pool()
    asyncio.run(
        replay(
            addresses,
            args.batch,
            args.blocks_file,
            args.wipe,
            args.streams,
            args.extra_logs_url,
            args.extra_required_file,
            args.seed_from,
        )
    )


if __name__ == "__main__":
    main()
