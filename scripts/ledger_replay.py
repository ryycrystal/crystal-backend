"""Replay chosen tokens' history through the position ledger into the side database.

Reads prod's raw log cache (read-only, PROD_PG* falling back to the PG* values from .env
through the tunnel on 127.0.0.1:15433), seeds the side database (DATABASE_URL) with the
reference tables and the MON/USD sample series, then runs LedgerEngine.process_block +
flush per chunk of hot blocks. The old position engine is never invoked.

    DATABASE_URL=postgresql://...@localhost/crystal_ledger \\
      python scripts/ledger_replay.py --token 0x... [--token ...] [--from-block N] [--blocks-file f] [--wipe | --wipe-token] [--reset-discovered] [--chain-logs-file f]
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
from core.ledger.engine import LedgerEngine  # noqa: E402
from core.ledger.kinds import AddressKinds  # noqa: E402
from core.ledger.rates import SAMPLE_TABLE, SEED_SAMPLES, RateBook, from_samples  # noqa: E402
from core.ledger.receipts import RECEIPT_LOG_DDL, ReceiptLogs  # noqa: E402
from core.ledger.schema import LEDGER_TABLES, init_ledger_schema  # noqa: E402
from core.ledger.txmeta import RpcClient, RpcError, TxMetaStore  # noqa: E402
from core.ledger.types import QUOTE_ASSETS  # noqa: E402
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
MON_USD_SAMPLE_TABLE = SAMPLE_TABLE
SIDE_DB_MARKERS = ("crystal_ledger", "crystal_replay")
PROD_QUERY_TIMEOUT = 300
PREFETCH_WORKERS = 4
RPC_BATCH_CALLS = 25
CHUNK_ATTEMPTS = 4
CHUNK_RETRY_SECONDS = 20
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
    """Forget everything derived for these tokens, including what a previous run claimed to have folded."""
    with storage.db_cursor() as cur:
        cur.execute("DELETE FROM wallet_flows WHERE token = ANY(%s)", (tokens,))
        flows = cur.rowcount
        cur.execute("DELETE FROM positions_v2 WHERE token = ANY(%s)", (tokens,))
        positions = cur.rowcount
        cur.execute("DELETE FROM parked_entitlements WHERE token = ANY(%s)", (tokens,))
        cur.execute("DELETE FROM token_coverage WHERE token = ANY(%s)", (tokens,))
        cur.execute("DELETE FROM token_fold_state WHERE token = ANY(%s)", (tokens,))
    print(f"[WIPE] {len(tokens)} token(s): {flows:,} flows and {positions:,} positions deleted", flush=True)
    return flows, positions


def reset_discovered() -> tuple[int, int]:
    """Forget venues the classifier discovered on its own so the replay re-derives them under the current rules."""
    with storage.db_cursor() as cur:
        cur.execute("DELETE FROM venues WHERE discovered")
        venues = cur.rowcount
        cur.execute("DELETE FROM address_kinds WHERE source IN ('heuristic', 'venue_event', 'pair_probe')")
        kinds = cur.rowcount
    print(f"[RESET] {venues:,} discovered venues and {kinds:,} derived kinds forgotten", flush=True)
    return venues, kinds


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
    """Copy prod's rate series in exactly the buckets the shared rate book will ask for."""
    t0 = time.time()
    rows, _ = prod_rows(src, SEED_SAMPLES, timeout=1800)
    rows = [(int(b), int(n), Decimal(r)) for b, n, r in rows]
    lcur.execute(f"TRUNCATE {MON_USD_SAMPLE_TABLE}")
    if rows:
        psycopg2.extras.execute_values(
            lcur,
            f"INSERT INTO {MON_USD_SAMPLE_TABLE} (bucket_ts, block_number, rate) VALUES %s",
            rows,
            page_size=5000,
        )
    print(f"[SEED] {MON_USD_SAMPLE_TABLE}: {len(rows):,} samples in {time.time() - t0:.0f}s", flush=True)
    return len(rows)


def seed_from_prod(src: LogSource, tokens: list[str], reference_tables: bool, everything: bool = False) -> None:
    t0 = time.time()
    with storage.db_cursor() as lcur:
        if reference_tables:
            for table in SEED_TABLES:
                copy_table(src, lcur, table)
            seed_mon_usd_samples(src, lcur)
        if everything and reference_tables:
            copy_table(src, lcur, "launchpad_positions")
        elif not everything:
            copy_table(src, lcur, "launchpad_positions", "WHERE token = ANY(%s)", (tokens,))
    print(f"[SEED] done in {time.time() - t0:.0f}s", flush=True)


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
        with storage.db_cursor() as cur:
            cur.execute("SELECT token, created_block FROM launchpad_tokens WHERE token = ANY(%s)", (missing,))
            for token, blk in cur.fetchall():
                if blk:
                    created[token] = int(blk)
                    print(
                        f"[SCOPE] {token}: prod no longer lists it, using the side database's created block {int(blk):,}",
                        flush=True,
                    )
            cur.execute("SELECT token_addr, pool FROM launchpad_pools WHERE token_addr = ANY(%s)", (missing,))
            for token, pool in cur.fetchall():
                venues[token].add(pool.lower())
            cur.execute(
                "SELECT base_address, market FROM crystal_markets WHERE LOWER(base_address) = ANY(%s)", (missing,)
            )
            for token, market in cur.fetchall():
                venues[token.lower()].add(market.lower())
        missing = [t for t in tokens if t not in created]
    if missing:
        raise SystemExit(f"not in prod launchpad_tokens nor the side database: {missing}")
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


def cached_block_logs(blocks: list[int]) -> dict[int, list[dict]]:
    """Blocks whose logs the side database already holds, so a second replay of a token reads nothing remote.

    Almost all of a replay's time through the tunnel is fetching logs; the netting is CPU and every lookup
    it makes is already cached. Prod's log cache for a past block never changes, so a copy taken once is
    good forever, and a change to the netting can be re-run against a token in minutes instead of hours.
    """
    if not blocks:
        return {}
    with storage.db_cursor() as cur:
        cur.execute("SELECT number, logs FROM launchpad_block_logs WHERE number = ANY(%s)", (blocks,))
        return {int(n): (json.loads(v) if isinstance(v, str) else v) for n, v in cur.fetchall()}


def remember_block_logs(rows: dict[int, list[dict]]) -> None:
    if not rows:
        return
    with storage.db_cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO launchpad_block_logs (number, logs) VALUES %s ON CONFLICT (number) DO NOTHING",
            [(int(n), psycopg2.extras.Json(v)) for n, v in rows.items()],
            page_size=200,
        )


def load_chain_logs(path: str | None) -> dict[int, list[dict]]:
    """Logs fetched straight from the chain by scripts/ledger_chain_logs.py, keyed by block."""
    if not path:
        return {}
    raw = json.load(open(path))
    out = {int(blk): logs for blk, logs in raw.items() if logs}
    total = sum(len(v) for v in out.values())
    print(f"[CHAIN] {total:,} chain-fetched logs in {len(out):,} blocks from {path}", flush=True)
    return out


def merge_chain_logs(cached: dict[int, list[dict]], chain: dict[int, list[dict]], blocks: list[int]) -> None:
    """Add chain-fetched logs to the cached rows of these blocks, skipping logs the cache already has."""
    for blk in blocks:
        extra = chain.get(blk)
        if not extra:
            continue
        have = cached.setdefault(blk, [])
        seen = {(str(lg.get("transactionHash")).lower(), str(lg.get("logIndex")).lower()) for lg in have}
        for lg in extra:
            key = (str(lg.get("transactionHash")).lower(), str(lg.get("logIndex")).lower())
            if key not in seen:
                seen.add(key)
                have.append(lg)


def load_or_find_blocks(
    src: LogSource, addresses: list[str], from_block: int, to_block: int | None, blocks_file: str | None
) -> tuple[list[int], int]:
    """The hot blocks, and the block from which the scan can honestly claim to have seen everything.

    A scan we ran ourselves covers the range we asked for. A list loaded from a file only demonstrates the
    blocks it contains, so it certifies nothing before its own first block: a file built for a later window
    would otherwise let a replay claim a history it never read.
    """
    if blocks_file and os.path.exists(blocks_file):
        blocks = [int(b) for b in json.load(open(blocks_file))]
        print(f"[BLOCKS] {len(blocks):,} hot blocks loaded from {blocks_file}", flush=True)
        scanned_from = min(blocks) if blocks else from_block
        if scanned_from > from_block:
            print(
                f"[BLOCKS] the cached list starts at {scanned_from:,}, so coverage is claimed only from there, "
                f"not from {from_block:,}",
                flush=True,
            )
    else:
        t0 = time.time()
        blocks = hot_blocks(src, addresses, from_block, to_block)
        print(f"[BLOCKS] {len(blocks):,} hot blocks found in {time.time() - t0:.0f}s", flush=True)
        scanned_from = from_block
        if blocks_file:
            json.dump(blocks, open(blocks_file, "w"))
            print(f"[BLOCKS] cached to {blocks_file}", flush=True)
    kept = [b for b in blocks if b >= from_block and (to_block is None or b <= to_block)]
    if len(kept) != len(blocks):
        print(f"[BLOCKS] {len(kept):,} within {from_block:,}-{to_block if to_block else 'head'}", flush=True)
    return kept, max(scanned_from, from_block)


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


def prepare_chunk(
    receipts: ReceiptLogs, tx_meta: TxMetaStore, blocks: list[int], cached: dict[int, list[dict]], watched: set[str]
) -> tuple[dict[int, list[dict]], int]:
    relevant = {blk: relevant_logs(cached.get(blk, []), watched) for blk in blocks}
    relevant = {blk: logs for blk, logs in relevant.items() if logs}
    ordered_blocks = sorted(relevant)
    hashes = sorted({(lg.get("transactionHash") or "").lower() for logs in relevant.values() for lg in logs} - {""})

    def complete_receipts(part: list[int]) -> int:
        with storage.db_cursor() as cur:
            return receipts.complete({blk: relevant[blk] for blk in part}, cur)

    with ThreadPoolExecutor(max_workers=2 * PREFETCH_WORKERS) as pool:
        receipt_parts = [ordered_blocks[i::PREFETCH_WORKERS] for i in range(PREFETCH_WORKERS)]
        receipt_futures = [pool.submit(complete_receipts, part) for part in receipt_parts if part]
        meta_parts = [hashes[i::PREFETCH_WORKERS] for i in range(PREFETCH_WORKERS)]
        meta_futures = [pool.submit(tx_meta.get_many, part) for part in meta_parts if part]
        added = sum(f.result() for f in receipt_futures)
        for f in meta_futures:
            f.result()
    return relevant, added


def prefetch_kinds(kinds: AddressKinds, logs_by_block: dict[int, list[dict]], cur, tokens: set[str]) -> int:
    addrs: set[str] = set()
    for logs in logs_by_block.values():
        for lg in logs:
            if (lg.get("address") or "").lower() not in tokens:
                continue
            topics = lg.get("topics") or []
            if topics and str(topics[0]).lower() == TRANSFER_TOPIC and len(topics) >= 3:
                addrs.add(h._topic_addr(topics[1]))
                addrs.add(h._topic_addr(topics[2]))
    addrs.discard("")
    kinds.kinds_for(sorted(addrs), cur)
    return len(addrs)


def process_chunk(
    engine: LedgerEngine,
    kinds: AddressKinds,
    relevant: dict[int, list[dict]],
    timestamps: dict[int, int],
    receipt_logs_added: int,
    span: tuple[list[str] | None, int, int] | None = None,
    fold: bool = True,
) -> tuple[int, int]:
    """Fold one chunk, recording its coverage in the same transaction as the flows it produced.

    The span is the whole hot-block range scanned so far, not the blocks that happened to carry a flow: a
    block the scan skipped is one where the token provably did not move, which is exactly what coverage
    means. Committing it separately would let a crash leave flows that claim more coverage than they have.
    """
    flows = 0
    with storage.db_cursor() as cur:
        engine.stats["receipt_logs"] += receipt_logs_added
        prefetch_kinds(kinds, relevant, cur, set(engine.registry(cur)) | QUOTE_ASSETS)
        engine.prefetch_tx_meta(relevant, cur)
        for blk, logs in relevant.items():
            flows += engine.process_block(blk, timestamps[blk], logs, cur)
        if span is not None:
            engine.cover(cur, span[0], span[1], span[2])
        refolded = engine.flush(cur) if fold else 0
    return flows, refolded


def process_chunk_with_retries(
    engine: LedgerEngine,
    kinds: AddressKinds,
    relevant: dict[int, list[dict]],
    timestamps: dict[int, int],
    receipt_logs_added: int,
    span: tuple[list[str] | None, int, int] | None = None,
    fold: bool = True,
) -> tuple[int, int]:
    for attempt in range(CHUNK_ATTEMPTS):
        try:
            return process_chunk(engine, kinds, relevant, timestamps, receipt_logs_added, span, fold)
        except (RuntimeError, RpcError, psycopg2.OperationalError, psycopg2.errors.DeadlockDetected) as exc:
            if attempt + 1 == CHUNK_ATTEMPTS:
                raise
            print(f"[CHUNK] attempt {attempt + 1}/{CHUNK_ATTEMPTS} failed: {exc!r}"[:200], flush=True)
            time.sleep(CHUNK_RETRY_SECONDS * (attempt + 1))
    raise RuntimeError("unreachable")


def summary(tokens: list[str], per_token: bool = True) -> None:
    from ledger_check import token_shares

    with storage.db_cursor() as cur:
        cur.execute("SELECT count(*) FROM wallet_flows")
        flows = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM positions_v2")
        positions = cur.fetchone()[0]
        if per_token:
            cur.execute(
                "SELECT basis_state, count(*) FROM wallet_flows WHERE token = ANY(%s) GROUP BY basis_state ORDER BY 1",
                (tokens,),
            )
        else:
            cur.execute("SELECT basis_state, count(*) FROM wallet_flows GROUP BY basis_state ORDER BY 1")
        states = cur.fetchall()
        shares = token_shares(cur, tokens) if per_token else {}
    print(f"[SUMMARY] {flows:,} flows, {positions:,} positions", flush=True)
    print(
        "[SUMMARY] flow basis states for the replayed tokens: " + ", ".join(f"{s} {n:,}" for s, n in states), flush=True
    )
    for token in tokens if per_token else []:
        est, unres, unpriced = shares.get(token, (None, None, None))
        print(
            f"[SUMMARY] {token}: estimated share {_pct(est)}, inflow still without a cost {_pct(unres)}, "
            f"inflow arriving with no price of its own {_pct(unpriced)}",
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
    if args.reset_discovered:
        reset_discovered()
    seed_from_prod(src, tokens, reference_tables=not args.skip_seed, everything=args.all)
    if args.all:
        with storage.db_cursor() as cur:
            cur.execute("SELECT token FROM launchpad_tokens WHERE token IS NOT NULL ORDER BY token")
            tokens = [row[0].lower() for row in cur.fetchall()]
        print(f"[SCOPE] every registered token: {len(tokens):,}", flush=True)

    created, venues = token_scope(src, tokens)
    watched = set(tokens)
    for addrs in venues.values():
        watched |= addrs
    from_block = args.from_block if args.from_block is not None else min(created.values())
    if args.resume:
        with storage.db_cursor() as cur:
            cur.execute("SELECT MAX(block_number) FROM wallet_flows WHERE token = ANY(%s)", (tokens,))
            last = cur.fetchone()[0]
        if last:
            from_block = max(from_block, int(last))
            print(f"[RESUME] continuing from block {from_block:,} (last ledger block for these tokens)", flush=True)
    for token in tokens if not args.all else []:
        print(f"[SCOPE] {token}: created {created[token]:,}, venues {sorted(venues[token])}", flush=True)

    blocks, covers_from = load_or_find_blocks(src, sorted(watched), from_block, args.to_block, args.blocks_file)
    chain_logs = load_chain_logs(args.chain_logs_file)
    if chain_logs:
        blocks = sorted(set(blocks) | set(chain_logs))
        covers_from = min(covers_from, min(chain_logs))
        earliest = min(chain_logs)
        with storage.db_cursor() as cur:
            cur.execute(
                "UPDATE launchpad_tokens SET created_block = LEAST(created_block, %s) WHERE token = ANY(%s)",
                (earliest, tokens),
            )
        print(f"[CHAIN] hot blocks now {len(blocks):,}; registration lowered to {earliest:,} where later", flush=True)
    if args.limit_blocks:
        blocks = blocks[: args.limit_blocks]
        print(f"[BLOCKS] limited to the first {len(blocks):,}", flush=True)
    if not blocks:
        print("[REPLAY] nothing to replay", flush=True)
        return

    tx_meta = TxMetaStore(storage.db_cursor, args.rpc, rpc=RpcClient(args.rpc, batch_size=args.rpc_batch))
    kinds = AddressKinds(storage.db_cursor, args.rpc)
    engine = LedgerEngine(
        storage.db_cursor,
        rpc_url=args.rpc,
        enabled=True,
        tx_meta_store=tx_meta,
        kinds=kinds,
        rates_fn=RateBook(from_samples),
    )
    engine.scope = None if args.all else frozenset(tokens)
    with storage.db_cursor() as cur:
        registry = engine.refresh_registry(cur)
        kinds.load_known(cur)
    receipts = ReceiptLogs(RpcClient(args.rpc, batch_size=args.rpc_batch), tokens=set(registry))

    groups = [blocks[i : i + args.batch] for i in range(0, len(blocks), args.batch)]
    fetcher = ParallelFetcher(args.streams)
    fetch_pool = ThreadPoolExecutor(max_workers=1)
    prepare_pool = ThreadPoolExecutor(max_workers=1)

    def fetch_group(group: list[int]) -> dict[int, list[dict]]:
        rows = cached_block_logs(group)
        missing = [blk for blk in group if blk not in rows]
        if missing:
            fetched = fetcher.fetch(missing)
            remember_block_logs(fetched)
            rows.update(fetched)
        merge_chain_logs(rows, chain_logs, group)
        return rows

    fetched = {gi: fetch_pool.submit(fetch_group, groups[gi]) for gi in range(min(2, len(groups)))}
    cached_logs: dict[int, dict[int, list[dict]]] = {}

    def prepared_for(gi: int):
        cached_logs[gi] = fetched.pop(gi).result()
        return prepare_pool.submit(prepare_chunk, receipts, tx_meta, groups[gi], cached_logs[gi], watched)

    prepared = {0: prepared_for(0)}
    done = 0
    total_flows = 0
    total_refolds = 0
    t0 = time.time()
    for gi, group in enumerate(groups):
        relevant, receipt_logs_added = prepared.pop(gi).result()
        cached = cached_logs.pop(gi)
        if gi + 2 < len(groups):
            fetched[gi + 2] = fetch_pool.submit(fetch_group, groups[gi + 2])
        if gi + 1 < len(groups):
            prepared[gi + 1] = prepared_for(gi + 1)
        await backfill.ensure_block_timestamps(cached)
        timestamps = await timestamps_for(group, cached)
        span = (None if args.all else tokens, covers_from, group[-1])
        flows, refolded = process_chunk_with_retries(
            engine, kinds, relevant, timestamps, receipt_logs_added, span, fold=not args.no_fold
        )
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
    fetch_pool.shutdown()
    prepare_pool.shutdown()
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO ledger_meta (key, value) VALUES ('replay_head_block', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (str(blocks[-1]),),
        )
    print(f"[REPLAY] {total_flows:,} flows in {time.time() - t0:.0f}s, head block {blocks[-1]:,}", flush=True)
    summary(tokens, per_token=not args.all)


def refold_only(tokens: list[str], per_token: bool = True) -> None:
    """Recompute positions from stored flows, for a fold change that alters no flow's identity or evidence."""
    from core.ledger import fold, store
    from core.ledger.schema import init_ledger_schema

    t0 = time.time()
    with storage.db_cursor() as cur:
        init_ledger_schema(cur)
        covered = store.coverage_from_creation(cur, tokens)
    skipped = [token for token in tokens if token not in covered]
    if skipped:
        print(f"[REFOLD] no coverage from creation, positions left alone: {skipped}", flush=True)
    written = 0
    folded = [token for token in tokens if token in covered]
    for i, token in enumerate(folded, 1):
        with storage.db_cursor() as cur:
            written += store.refold_tokens(cur, {token: 0}, fold.fold_token)
        if per_token or i % 500 == 0 or i == len(folded):
            print(f"[REFOLD] {i:,}/{len(folded):,} tokens, {written:,} positions, {time.time() - t0:.0f}s", flush=True)
    print(f"[REFOLD] {written:,} positions in {time.time() - t0:.0f}s", flush=True)
    summary(tokens, per_token=per_token)


def backfill_coverage(tokens: list[str]) -> None:
    """Record coverage for a database replayed before coverage existed, from what its flows already prove.

    Only claims what the earlier run's own scope claimed: the token's registration block through its last
    flow. A token whose first flow is later than its registration gets the range it actually has, which is
    what then keeps it out of positions.
    """
    from core.ledger import store
    from core.ledger.schema import init_ledger_schema

    with storage.db_cursor() as cur:
        init_ledger_schema(cur)
        for token in tokens:
            cur.execute("SELECT MIN(block_number), MAX(block_number) FROM wallet_flows WHERE token = %s", (token,))
            lo, hi = cur.fetchone()
            if lo is None:
                continue
            cur.execute("SELECT registered_block FROM token_registry WHERE token = %s", (token,))
            row = cur.fetchone()
            start = int(row[0]) if row and row[0] is not None and int(row[0]) <= int(lo) else int(lo)
            store.extend_coverage(cur, token, start, int(hi))
            print(f"[COVER] {token}: {start:,}-{int(hi):,}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="replay tokens through the position ledger into the side database")
    ap.add_argument("--token", action="append", default=[])
    ap.add_argument("--from-block", type=int)
    ap.add_argument("--to-block", type=int)
    ap.add_argument("--blocks-file", help="cache of the hot block list; loaded when it exists")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument(
        "--rpc-batch",
        type=int,
        default=RPC_BATCH_CALLS,
        help="json-rpc calls per http request for transaction metadata and receipts; small batches avoid the per-second cap",
    )
    ap.add_argument("--streams", type=int, default=4, help="parallel prod connections per chunk fetch")
    ap.add_argument("--limit-blocks", type=int, default=0, help="process only the first N hot blocks")
    ap.add_argument("--rpc", default=os.environ.get("RPC_HTTP", "https://rpc.monad.xyz"))
    ap.add_argument("--wipe", action="store_true", help="truncate the ledger tables before replaying")
    ap.add_argument(
        "--resume",
        action="store_true",
        help="continue from the highest block already in wallet_flows for these tokens; implies no wipe",
    )
    ap.add_argument(
        "--wipe-token",
        action="store_true",
        help="delete only the replayed tokens' flows and positions, keeping other tokens' ledger data",
    )
    ap.add_argument(
        "--chain-logs-file",
        help="JSON of {block: [logs]} from scripts/ledger_chain_logs.py; merged into the replay for history prod's cache lacks",
    )
    ap.add_argument(
        "--reset-discovered",
        action="store_true",
        help="forget venues the classifier discovered (pool-event and pool-shape rules) so this replay re-derives them",
    )
    ap.add_argument(
        "--skip-seed",
        action="store_true",
        help="reference tables and the mon/usd samples are already seeded; the tokens' prod positions are still copied",
    )
    ap.add_argument(
        "--refold",
        action="store_true",
        help="fold only: recompute these tokens' positions and fold columns from the flows already stored",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="every registered token, unscoped: a block-range partition of the whole history, coverage recorded for all tokens",
    )
    ap.add_argument(
        "--no-fold",
        action="store_true",
        help="net flows only; the fold runs once over the merged partitions with --refold",
    )
    ap.add_argument(
        "--backfill-coverage",
        action="store_true",
        help="record coverage for tokens replayed before coverage existed, from the span their flows already prove",
    )
    args = ap.parse_args()

    tokens = list(dict.fromkeys(t.lower() for t in args.token))
    if not tokens and not args.all:
        raise SystemExit("pass --token or --all")
    require_side_db()
    storage.init_pool()
    if args.backfill_coverage:
        backfill_coverage(tokens)
    if args.refold:
        if args.all:
            with storage.db_cursor() as cur:
                cur.execute("SELECT DISTINCT token FROM wallet_flows ORDER BY token")
                tokens = [row[0] for row in cur.fetchall()]
            print(f"[REFOLD] every token with flows: {len(tokens):,}", flush=True)
        refold_only(tokens, per_token=not args.all)
        return
    asyncio.run(replay(args, tokens))


if __name__ == "__main__":
    main()
