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
FETCH_ATTEMPTS = 6


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
            try:
                with self.conn().cursor() as cur:
                    cur.execute(
                        "SELECT number, logs FROM launchpad_block_logs WHERE number = ANY(%s)",
                        (numbers,),
                    )
                    return {int(n): (json.loads(v) if isinstance(v, str) else v) for n, v in cur.fetchall()}
            except psycopg2.Error as e:
                print(f"[FETCH] attempt {attempt + 1}/{FETCH_ATTEMPTS} failed: {e!r}", flush=True)
                self._drop()
                time.sleep(min(2**attempt, 30))
        raise RuntimeError("prod log fetch kept failing")


def wipe_side() -> None:
    with storage.db_cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = [r[0] for r in cur.fetchall()]
        if tables:
            cur.execute("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
    print(f"[WIPE] truncated {len(tables)} side tables", flush=True)


def seed_from_prod(pc) -> None:
    with pc.cursor() as pcur, storage.db_cursor() as lcur:
        for table in SEED_TABLES:
            try:
                pcur.execute(f"SELECT * FROM {table}")
            except Exception:
                pc.rollback()
                print(f"[SEED] {table}: not on prod, skipped", flush=True)
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


async def replay(addresses: list[str], batch: int, blocks_file: str | None, wipe: bool) -> None:
    src = LogSource()
    if wipe:
        wipe_side()
    seed_from_prod(src.conn())

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
    prefetch = LogSource()
    pool = ThreadPoolExecutor(max_workers=1)
    pending = pool.submit(prefetch.fetch, groups[0])

    done = 0
    t0 = time.time()
    for gi, group in enumerate(groups):
        cached = pending.result()
        if gi + 1 < len(groups):
            pending = pool.submit(prefetch.fetch, groups[gi + 1])
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
                f"last chunk {group[0]}-{group[-1]}: {seen}",
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
    asyncio.run(replay(addresses, args.batch, args.blocks_file, args.wipe))


if __name__ == "__main__":
    main()
