"""Replay chosen tokens' history through the position ledger into the side database.

Reads prod's raw log cache (read-only, PROD_PG* falling back to the PG* values from .env
through the tunnel on 127.0.0.1:15433), seeds the side database (DATABASE_URL) with the
reference tables and the MON/USD sample series, then runs LedgerEngine.process_block +
flush per chunk of hot blocks. The old position engine is never invoked.

    DATABASE_URL=postgresql://...@localhost/crystal_ledger \\
      python scripts/ledger_replay.py --token 0x... [--token ...] [--from-block N] [--blocks-file f] [--wipe | --wipe-token]
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
from decimal import Decimal

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["LEDGER_ENABLED"] = "1"

from env_loader import load_env  # noqa: E402

load_env()
for _key in ("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"):
    if os.environ.get(_key):
        os.environ.setdefault("PROD_" + _key, os.environ[_key])
os.environ.setdefault("PROD_PGHOSTADDR", "127.0.0.1")
os.environ.setdefault("PROD_PGPORT", "15433")

from replay_side import FETCH_ATTEMPTS, LogSource, ParallelFetcher  # noqa: E402

import backfill  # noqa: E402
import core.chain as h  # noqa: E402
import core.storage as storage  # noqa: E402
from core.ledger.engine import LedgerEngine, Rates  # noqa: E402
from core.ledger.kinds import AddressKinds  # noqa: E402
from core.ledger.receipts import RECEIPT_LOG_DDL, ReceiptLogs  # noqa: E402
from core.ledger.schema import LEDGER_TABLES, init_ledger_schema  # noqa: E402
from core.ledger.txmeta import RpcClient, TxMetaStore  # noqa: E402
from core.storage import schema  # noqa: E402

SEED_TABLES = (
    "launchpad_tokens",
    "launchpad_pools",
    "univ4_pools",
    "crystal_markets",
    "nadfun_v2_tokens",
    "launchpad_meta",
    "holder_denylist",
)
MON_USD_SAMPLE_TABLE = "ledger_mon_usd_samples"
MON_USD_SAMPLE_RESOLUTION = 60
MON_USD_MIN_TRADE_WEI = 10**16
SIDE_DB_MARKERS = ("crystal_ledger", "crystal_replay")
PROD_QUERY_TIMEOUT = 300
PREFETCH_WORKERS = 4
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PROD_PAGE_ROWS = 2000


def require_side_db() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not any(marker in url for marker in SIDE_DB_MARKERS):
        raise SystemExit(f"DATABASE_URL must point at the side database, got {url[:40]!r}")


def init_side_schema() -> None:
    schema.init_db()
    with storage.db_autocommit_cursor() as cur:
        init_ledger_schema(cur)
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MON_USD_SAMPLE_TABLE}
            (
                bucket_ts    BIGINT PRIMARY KEY,
                block_number BIGINT NOT NULL,
                rate         NUMERIC(50, 18) NOT NULL
            )
            """
        )
        cur.execute(RECEIPT_LOG_DDL)


def wipe_ledger() -> None:
    with storage.db_cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(LEDGER_TABLES))
    print(f"[WIPE] truncated {len(LEDGER_TABLES)} ledger tables", flush=True)


def wipe_tokens(tokens: list[str]) -> tuple[int, int]:
    with storage.db_cursor() as cur:
        cur.execute("DELETE FROM wallet_flows WHERE token = ANY(%s)", (tokens,))
        flows = cur.rowcount
        cur.execute("DELETE FROM positions_v2 WHERE token = ANY(%s)", (tokens,))
        positions = cur.rowcount
    print(f"[WIPE] {len(tokens)} token(s): {flows:,} flows and {positions:,} positions deleted", flush=True)
    return flows, positions


class Watchdog:
    def __init__(self, src: LogSource, conn, timeout: int) -> None:
        self._src = src
        self._conn = conn
        self._timeout = timeout
        self._timer: threading.Timer | None = None

    def arm(self) -> None:
        self.cancel()
        self._timer = threading.Timer(self._timeout, self._src._cancel, args=(self._conn,))
        self._timer.daemon = True
        self._timer.start()

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


def prod_rows(
    src: LogSource, sql: str, params: tuple = (), timeout: int = PROD_QUERY_TIMEOUT, page: int = PROD_PAGE_ROWS
) -> tuple[list, list[str]]:
    for attempt in range(FETCH_ATTEMPTS):
        conn = src.conn()
        watchdog = Watchdog(src, conn, timeout)
        watchdog.arm()
        try:
            with conn.cursor() as plain:
                plain.execute(f"SET statement_timeout = '{timeout}s'")
            cur = conn.cursor(name=f"ledger_replay_{attempt}", withhold=True)
            cur.itersize = page
            cur.execute(sql, params)
            rows: list = []
            while True:
                chunk = cur.fetchmany(page)
                if not chunk:
                    break
                rows.extend(chunk)
                watchdog.arm()
            cols = [d[0] for d in cur.description]
            cur.close()
            return rows, cols
        except psycopg2.Error as e:
            print(f"[PROD] attempt {attempt + 1}/{FETCH_ATTEMPTS} failed: {e!r}"[:200], flush=True)
            src._drop()
            time.sleep(min(2**attempt, 30))
        finally:
            watchdog.cancel()
    raise RuntimeError("prod query kept failing")


def prod_table_exists(src: LogSource, table: str) -> bool:
    rows, _ = prod_rows(src, "SELECT to_regclass(%s)", (table,))
    return bool(rows and rows[0][0])


def copy_table(src: LogSource, lcur, table: str, where: str = "", params: tuple = ()) -> int:
    if not prod_table_exists(src, table):
        print(f"[SEED] {table}: not on prod, skipped", flush=True)
        return 0
    t0 = time.time()
    rows, cols = prod_rows(src, f"SELECT * FROM {table} {where}", params)
    if where:
        lcur.execute(f"DELETE FROM {table} {where}", params)
    else:
        lcur.execute(f"TRUNCATE {table}")
    if rows:
        psycopg2.extras.execute_values(
            lcur,
            f"INSERT INTO {table} ({','.join(cols)}) VALUES %s ON CONFLICT DO NOTHING",
            rows,
            page_size=2000,
        )
    print(f"[SEED] {table}: {len(rows):,} rows in {time.time() - t0:.0f}s", flush=True)
    return len(rows)


def seed_mon_usd_samples(src: LogSource, lcur) -> int:
    t0 = time.time()
    rows, _ = prod_rows(
        src,
        """
        SELECT bucket, block_number, rate FROM (
            SELECT DISTINCT ON (timestamp / %s)
                timestamp / %s * %s AS bucket,
                block_number,
                usd_amount / (native_amount / 1e18) AS rate
            FROM launchpad_trades
            WHERE native_amount >= %s AND usd_amount > 0 AND timestamp > 0
            ORDER BY timestamp / %s, timestamp DESC, block_number DESC, log_index DESC
        ) s
        ORDER BY bucket
        """,
        (
            MON_USD_SAMPLE_RESOLUTION,
            MON_USD_SAMPLE_RESOLUTION,
            MON_USD_SAMPLE_RESOLUTION,
            MON_USD_MIN_TRADE_WEI,
            MON_USD_SAMPLE_RESOLUTION,
        ),
        timeout=1800,
    )
    rows = [(int(b), int(n), Decimal(r)) for b, n, r in rows]
    lcur.execute(f"TRUNCATE {MON_USD_SAMPLE_TABLE}")
    if rows:
        psycopg2.extras.execute_values(
            lcur,
            f"INSERT INTO {MON_USD_SAMPLE_TABLE} (bucket_ts, block_number, rate) VALUES %s",
            rows,
            page_size=5000,
        )
    print(
        f"[SEED] {MON_USD_SAMPLE_TABLE}: {len(rows):,} samples at {MON_USD_SAMPLE_RESOLUTION}s in {time.time() - t0:.0f}s",
        flush=True,
    )
    return len(rows)


def seed_from_prod(src: LogSource, tokens: list[str], reference_tables: bool) -> None:
    t0 = time.time()
    with storage.db_cursor() as lcur:
        if reference_tables:
            for table in SEED_TABLES:
                copy_table(src, lcur, table)
            seed_mon_usd_samples(src, lcur)
        copy_table(src, lcur, "launchpad_positions", "WHERE token = ANY(%s)", (tokens,))
    print(f"[SEED] done in {time.time() - t0:.0f}s", flush=True)


class SideRates:
    def __init__(self) -> None:
        self._cache: dict[int, Rates] = {}
        self._lvmon: Decimal | None = None

    def __call__(self, blk: int, ts: int, cur) -> Rates:
        bucket = int(ts or 0) // MON_USD_SAMPLE_RESOLUTION
        cached = self._cache.get(bucket)
        if cached is not None:
            return cached
        cur.execute(
            f"SELECT rate FROM {MON_USD_SAMPLE_TABLE} WHERE bucket_ts <= %s ORDER BY bucket_ts DESC LIMIT 1",
            (bucket * MON_USD_SAMPLE_RESOLUTION,),
        )
        row = cur.fetchone()
        mon_usd = Decimal(str(row[0])) if row and row[0] is not None else Decimal(0)
        if self._lvmon is None:
            cur.execute("SELECT value FROM launchpad_meta WHERE key = 'lvmon_mon_rate'")
            meta = cur.fetchone()
            self._lvmon = Decimal(str(meta[0])) if meta and meta[0] is not None else Decimal(1)
        rates = Rates(mon_usd=mon_usd, lvmon_rate=self._lvmon, usdc_per_mon=mon_usd)
        if len(self._cache) > 8192:
            self._cache.clear()
        self._cache[bucket] = rates
        return rates


def token_scope(src: LogSource, tokens: list[str]) -> tuple[dict[str, int], dict[str, set[str]]]:
    created: dict[str, int] = {}
    venues: dict[str, set[str]] = {t: set() for t in tokens}
    rows, _ = prod_rows(src, "SELECT token, created_block FROM launchpad_tokens WHERE token = ANY(%s)", (tokens,))
    for token, blk in rows:
        created[token] = int(blk)
    rows, _ = prod_rows(src, "SELECT token_addr, pool FROM launchpad_pools WHERE token_addr = ANY(%s)", (tokens,))
    for token, pool in rows:
        venues[token].add(pool.lower())
    rows, _ = prod_rows(
        src, "SELECT base_address, market FROM crystal_markets WHERE LOWER(base_address) = ANY(%s)", (tokens,)
    )
    for token, market in rows:
        venues[token.lower()].add(market.lower())
    missing = [t for t in tokens if t not in created]
    if missing:
        raise SystemExit(f"not in prod launchpad_tokens: {missing}")
    return created, venues


def hot_blocks(src: LogSource, addresses: list[str], from_block: int, to_block: int | None) -> list[int]:
    rows, _ = prod_rows(
        src,
        """
        SELECT number FROM launchpad_block_logs b
        WHERE number >= %s AND number <= %s
          AND EXISTS (SELECT 1 FROM jsonb_array_elements(b.logs) e WHERE e->>'address' = ANY(%s))
        ORDER BY number
        """,
        (from_block, to_block if to_block is not None else 2**62, addresses),
        timeout=3600,
    )
    return [int(r[0]) for r in rows]


def load_or_find_blocks(
    src: LogSource, addresses: list[str], from_block: int, to_block: int | None, blocks_file: str | None
) -> list[int]:
    if blocks_file and os.path.exists(blocks_file):
        blocks = [int(b) for b in json.load(open(blocks_file))]
        print(f"[BLOCKS] {len(blocks):,} hot blocks loaded from {blocks_file}", flush=True)
    else:
        t0 = time.time()
        blocks = hot_blocks(src, addresses, from_block, to_block)
        print(f"[BLOCKS] {len(blocks):,} hot blocks found in {time.time() - t0:.0f}s", flush=True)
        if blocks_file:
            json.dump(blocks, open(blocks_file, "w"))
            print(f"[BLOCKS] cached to {blocks_file}", flush=True)
    kept = [b for b in blocks if b >= from_block and (to_block is None or b <= to_block)]
    if len(kept) != len(blocks):
        print(f"[BLOCKS] {len(kept):,} within {from_block:,}-{to_block if to_block else 'head'}", flush=True)
    return kept


def block_timestamp(logs: list[dict]) -> int:
    for lg in logs:
        raw = lg.get("blockTimestamp")
        if raw is None:
            continue
        return int(raw, 16) if isinstance(raw, str) else int(raw)
    return 0


def relevant_logs(logs: list[dict], watched: set[str]) -> list[dict]:
    for raw in logs:
        h.register_dynamic_addresses_from_log(raw)
    hot_txs = {(lg.get("transactionHash") or "").lower() for lg in logs if (lg.get("address") or "").lower() in watched}
    if not hot_txs:
        return []
    return [lg for lg in logs if (lg.get("transactionHash") or "").lower() in hot_txs]


async def timestamps_for(blocks: list[int], cached: dict[int, list[dict]]) -> dict[int, int]:
    out = {blk: block_timestamp(cached.get(blk, [])) for blk in blocks}
    for blk, ts in list(out.items()):
        if ts == 0 and cached.get(blk):
            out[blk] = await backfill.get_block_timestamp_http(blk)
    return out


def prefetch_chunk(
    tx_meta: TxMetaStore, kinds: AddressKinds, logs_by_block: dict[int, list[dict]], cur
) -> tuple[int, int]:
    hashes: set[str] = set()
    addrs: set[str] = set()
    for logs in logs_by_block.values():
        for lg in logs:
            hashes.add((lg.get("transactionHash") or "").lower())
            topics = lg.get("topics") or []
            if topics and str(topics[0]).lower() == TRANSFER_TOPIC and len(topics) >= 3:
                addrs.add(h._topic_addr(topics[1]))
                addrs.add(h._topic_addr(topics[2]))
    hashes.discard("")
    addrs.discard("")
    ordered = sorted(hashes)
    parts = [ordered[i::PREFETCH_WORKERS] for i in range(PREFETCH_WORKERS)]
    with ThreadPoolExecutor(max_workers=PREFETCH_WORKERS) as pool:
        list(pool.map(tx_meta.get_many, [p for p in parts if p]))
    kinds.kinds_for(sorted(addrs), cur)
    return len(hashes), len(addrs)


def process_chunk(
    engine: LedgerEngine,
    tx_meta: TxMetaStore,
    kinds: AddressKinds,
    receipts: ReceiptLogs,
    blocks: list[int],
    cached: dict[int, list[dict]],
    timestamps: dict[int, int],
    watched: set[str],
) -> tuple[int, int]:
    relevant = {blk: relevant_logs(cached.get(blk, []), watched) for blk in blocks}
    relevant = {blk: logs for blk, logs in relevant.items() if logs}
    flows = 0
    with storage.db_cursor() as cur:
        engine.stats["receipt_logs"] += receipts.complete(relevant, cur)
        prefetch_chunk(tx_meta, kinds, relevant, cur)
        for blk, logs in relevant.items():
            flows += engine.process_block(blk, timestamps[blk], logs, cur)
        refolded = engine.flush(cur)
    return flows, refolded


def summary(tokens: list[str]) -> None:
    from ledger_check import token_shares

    with storage.db_cursor() as cur:
        cur.execute("SELECT count(*) FROM wallet_flows")
        flows = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM positions_v2")
        positions = cur.fetchone()[0]
        cur.execute(
            "SELECT basis_state, count(*) FROM wallet_flows WHERE token = ANY(%s) GROUP BY basis_state ORDER BY 1",
            (tokens,),
        )
        states = cur.fetchall()
        shares = token_shares(cur, tokens)
    print(f"[SUMMARY] {flows:,} flows, {positions:,} positions", flush=True)
    print(
        "[SUMMARY] flow basis states for the replayed tokens: " + ", ".join(f"{s} {n:,}" for s, n in states), flush=True
    )
    for token in tokens:
        est, unres = shares.get(token, (None, None))
        print(
            f"[SUMMARY] {token}: estimated share {_pct(est)}, unresolved share {_pct(unres)}",
            flush=True,
        )


def _pct(value) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.3f}%"


async def replay(args: argparse.Namespace, tokens: list[str]) -> None:
    src = LogSource()
    init_side_schema()
    if args.wipe:
        wipe_ledger()
    elif args.wipe_token:
        wipe_tokens(tokens)
    seed_from_prod(src, tokens, reference_tables=not args.skip_seed)

    created, venues = token_scope(src, tokens)
    watched = set(tokens)
    for addrs in venues.values():
        watched |= addrs
    from_block = args.from_block if args.from_block is not None else min(created.values())
    for token in tokens:
        print(f"[SCOPE] {token}: created {created[token]:,}, venues {sorted(venues[token])}", flush=True)

    blocks = load_or_find_blocks(src, sorted(watched), from_block, args.to_block, args.blocks_file)
    if args.limit_blocks:
        blocks = blocks[: args.limit_blocks]
        print(f"[BLOCKS] limited to the first {len(blocks):,}", flush=True)
    if not blocks:
        print("[REPLAY] nothing to replay", flush=True)
        return

    tx_meta = TxMetaStore(storage.db_cursor, args.rpc)
    kinds = AddressKinds(storage.db_cursor, args.rpc)
    receipts = ReceiptLogs(RpcClient(args.rpc))
    engine = LedgerEngine(
        storage.db_cursor, rpc_url=args.rpc, enabled=True, tx_meta_store=tx_meta, kinds=kinds, rates_fn=SideRates()
    )
    with storage.db_cursor() as cur:
        engine.refresh_registry(cur)
        kinds.load_known(cur)

    groups = [blocks[i : i + args.batch] for i in range(0, len(blocks), args.batch)]
    fetcher = ParallelFetcher(args.streams)
    pool = ThreadPoolExecutor(max_workers=1)
    pending = pool.submit(fetcher.fetch, groups[0])
    done = 0
    total_flows = 0
    total_refolds = 0
    t0 = time.time()
    for gi, group in enumerate(groups):
        cached = pending.result()
        if gi + 1 < len(groups):
            pending = pool.submit(fetcher.fetch, groups[gi + 1])
        await backfill.ensure_block_timestamps(cached)
        timestamps = await timestamps_for(group, cached)
        flows, refolded = process_chunk(engine, tx_meta, kinds, receipts, group, cached, timestamps, watched)
        total_flows += flows
        total_refolds += refolded
        done += len(group)
        if gi % 10 == 0 or done == len(blocks):
            rate = done / max(time.time() - t0, 0.001)
            eta = (len(blocks) - done) / max(rate, 0.001)
            print(
                f"[REPLAY] {done:,}/{len(blocks):,} hot blocks ({rate:.0f}/s, eta {eta / 60:.0f}m) "
                f"chunk {group[0]}-{group[-1]}: {flows} flows, {refolded} positions refolded; "
                f"total {total_flows:,} flows; engine {dict(engine.stats)}",
                flush=True,
            )
    pool.shutdown()
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO ledger_meta (key, value) VALUES ('replay_head_block', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (str(blocks[-1]),),
        )
    print(f"[REPLAY] {total_flows:,} flows in {time.time() - t0:.0f}s, head block {blocks[-1]:,}", flush=True)
    summary(tokens)


def main() -> None:
    ap = argparse.ArgumentParser(description="replay tokens through the position ledger into the side database")
    ap.add_argument("--token", action="append", default=[])
    ap.add_argument("--from-block", type=int)
    ap.add_argument("--to-block", type=int)
    ap.add_argument("--blocks-file", help="cache of the hot block list; loaded when it exists")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--streams", type=int, default=4, help="parallel prod connections per chunk fetch")
    ap.add_argument("--limit-blocks", type=int, default=0, help="process only the first N hot blocks")
    ap.add_argument("--rpc", default=os.environ.get("RPC_HTTP", "https://rpc.monad.xyz"))
    ap.add_argument("--wipe", action="store_true", help="truncate the ledger tables before replaying")
    ap.add_argument(
        "--wipe-token",
        action="store_true",
        help="delete only the replayed tokens' flows and positions, keeping other tokens' ledger data",
    )
    ap.add_argument(
        "--skip-seed",
        action="store_true",
        help="reference tables and the mon/usd samples are already seeded; the tokens' prod positions are still copied",
    )
    args = ap.parse_args()

    tokens = list(dict.fromkeys(t.lower() for t in args.token))
    if not tokens:
        raise SystemExit("pass --token")
    require_side_db()
    storage.init_pool()
    asyncio.run(replay(args, tokens))


if __name__ == "__main__":
    main()
