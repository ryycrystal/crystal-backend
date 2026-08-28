import argparse
import asyncio
import sys

import backfill
import core.storage as storage
from core import chain as h
from core.sequencer import SEQUENCER

COUNTED_TABLES = (
    "launchpad_trades",
    "launchpad_ohlcv",
    "launchpad_positions",
    "launchpad_users",
    "crystal_market_trades",
    "crystal_orderbook_events",
    "crystal_orderbook_orders",
    "crystal_orderbook_fills",
)


def _snapshot(cur) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in COUNTED_TABLES:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        out[t] = int(cur.fetchone()[0])
    return out


def _affected_candles(cur, start_block: int, end_block: int) -> list[tuple]:
    cur.execute(
        """
        SELECT o.token, o.resolution_sec, o.bucket_start, o.close_price
        FROM launchpad_ohlcv o
        WHERE (o.token, o.resolution_sec, o.bucket_start) IN (
            SELECT t.token, r.res, (t.timestamp / r.res) * r.res
            FROM launchpad_trades t
            CROSS JOIN (SELECT UNNEST(ARRAY[60, 300, 900, 3600, 14400, 86400]) AS res) r
            WHERE t.block_number BETWEEN %s AND %s
        )
        ORDER BY o.token, o.resolution_sec, o.bucket_start
        """,
        (start_block, end_block),
    )
    return cur.fetchall()


async def repair(start_block: int, end_block: int, batch: int, dry_run: bool) -> None:
    min_cached, max_cached = storage.get_cached_block_range()
    if min_cached is None or start_block < min_cached or end_block > max_cached:
        raise RuntimeError(f"range {start_block}-{end_block} is outside the cache {min_cached}-{max_cached}")

    missing = storage.count_uncached_processed_blocks(start_block, end_block)
    if missing:
        raise RuntimeError(f"{missing} blocks in {start_block}-{end_block} have no cached logs, run doctor.py --refill")

    print(f"[REPAIR] rebuilding state from db before replaying {start_block}-{end_block}", flush=True)
    SEQUENCER._state.rebuild_from_db()
    SEQUENCER.reset_pending(start_block)

    with storage.db_cursor() as cur:
        before = _snapshot(cur)
        candles_before = _affected_candles(cur, start_block, end_block)

        for chunk_start in range(start_block, end_block + 1, batch):
            chunk_end = min(chunk_start + batch - 1, end_block)
            cached = storage.get_block_logs_range(chunk_start, chunk_end, cur=cur)
            await backfill.ensure_block_timestamps(cached)

            filtered_logs: dict[int, list[dict]] = {}
            for blk in range(chunk_start, chunk_end + 1):
                logs_for_blk = cached.get(blk, [])
                for raw in logs_for_blk:
                    h.register_dynamic_addresses_from_log(raw)

                filtered = []
                new_tokens_in_blk = backfill._new_tokens_in_block(logs_for_blk)
                for raw in logs_for_blk:
                    topics = raw.get("topics") or []
                    if not topics:
                        continue
                    tag = h.EVENT_SIGS.get(topics[0].lower())
                    if not tag:
                        continue
                    addr = raw.get("address", "").lower()
                    if not h.accepts_log_for_indexing(tag, addr):
                        continue
                    if tag == "TF":
                        mi = SEQUENCER._state.addressToMarket.get(addr)
                        is_lp_market = bool(mi is not None and int(getattr(mi, "marketType", 0) or 0) > 1)
                        if (
                            addr not in SEQUENCER._state.launchpad_tokens
                            and addr not in SEQUENCER._state.token_to_v3_pool
                            and not is_lp_market
                            and addr not in new_tokens_in_blk
                        ):
                            continue
                    filtered.append(raw)
                filtered_logs[blk] = filtered

            SEQUENCER.process_chunk(chunk_start, chunk_end, filtered_logs, cur)

        after = _snapshot(cur)
        candles_after = _affected_candles(cur, start_block, end_block)

        print("[REPAIR] row deltas:", flush=True)
        for t in COUNTED_TABLES:
            delta = after[t] - before[t]
            print(f"  {t:<28} {before[t]:>10} -> {after[t]:>10}  ({delta:+})", flush=True)

        moved = [
            (b[0], b[1], b[2], b[3], a[3])
            for b, a in zip(candles_before, candles_after)
            if b[:3] == a[:3] and b[3] != a[3]
        ]
        print(f"[REPAIR] candles with a rewritten close price: {len(moved)}", flush=True)
        for token, res, bucket, old, new in moved[:20]:
            print(f"  {token} res={res} bucket={bucket} close {old} -> {new}", flush=True)

        if dry_run:
            cur.connection.rollback()
            print("[REPAIR] dry run, rolled back", flush=True)
            return

    print(f"[REPAIR] committed replay of {start_block}-{end_block}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="replay one cached block range into derived state")
    parser.add_argument("--start-block", type=lambda x: int(x, 0), required=True)
    parser.add_argument("--end-block", type=lambda x: int(x, 0), required=True)
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--i-know-the-indexer-is-stopped", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and not args.i_know_the_indexer_is_stopped:
        print(
            "refusing to write while the indexer may be live: its in memory state would overwrite "
            "repaired positions on the next flush. stop the indexer, then pass "
            "--i-know-the-indexer-is-stopped",
            file=sys.stderr,
        )
        raise SystemExit(2)

    storage.init_pool()
    storage.init_db()
    asyncio.run(repair(args.start_block, args.end_block, args.batch, args.dry_run))


if __name__ == "__main__":
    main()
