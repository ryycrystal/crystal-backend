import argparse
import asyncio
import sys
import time

import backfill
import core.storage as storage
from core import chain as h
from core.sequencer import SEQUENCER

SCAN_WINDOW = 5_000_000


def _hot_blocks(start_block: int, end_block: int, addresses: list[str]) -> list[int]:
    found: list[int] = []
    t0 = time.time()
    for window_start in range(start_block, end_block + 1, SCAN_WINDOW):
        window_end = min(window_start + SCAN_WINDOW - 1, end_block)
        found.extend(storage.list_blocks_with_addresses(window_start, window_end, addresses))
        print(
            f"[SCAN] {window_end:,} scanned, {len(found):,} blocks with events ({time.time() - t0:.0f}s)",
            flush=True,
        )
    return found


def _filter_logs(blocks: list[int], cached: dict[int, list[dict]]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for blk in blocks:
        logs_for_blk = cached.get(blk, [])
        for raw in logs_for_blk:
            h.register_dynamic_addresses_from_log(raw)

        keep = []
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
            keep.append(raw)
        out[blk] = keep
    return out


async def replay(start_block: int, end_block: int, addresses: list[str], batch: int, dry_run: bool) -> None:
    min_cached, max_cached = storage.get_cached_block_range()
    if min_cached is None:
        raise RuntimeError("the log cache is empty, nothing to replay")
    start_block = max(start_block, min_cached)
    end_block = min(end_block, max_cached)

    print(f"[REPLAY] scanning {start_block:,}-{end_block:,} for {len(addresses)} addresses", flush=True)
    blocks = _hot_blocks(start_block, end_block, addresses)
    if not blocks:
        print("[REPLAY] no blocks carry those addresses", flush=True)
        return
    print(f"[REPLAY] {len(blocks):,} blocks to replay out of {end_block - start_block + 1:,}", flush=True)

    SEQUENCER._state.rebuild_from_db()
    SEQUENCER.reset_pending(blocks[0])

    done = 0
    t0 = time.time()
    for i in range(0, len(blocks), batch):
        group = blocks[i : i + batch]
        with storage.db_cursor() as cur:
            cached = storage.get_block_logs_for(group, cur=cur)
            await backfill.ensure_block_timestamps(cached)
            filtered = _filter_logs(group, cached)
            for blk in group:
                SEQUENCER.process_chunk(blk, blk, {blk: filtered.get(blk, [])}, cur)
            if dry_run:
                cur.connection.rollback()
        done += len(group)
        if (i // batch) % 20 == 0 or done == len(blocks):
            rate = done / max(time.time() - t0, 0.001)
            eta = (len(blocks) - done) / max(rate, 0.001)
            print(
                f"[REPLAY] {done:,}/{len(blocks):,} blocks ({rate:.0f}/s, eta {eta / 60:.0f}m)",
                flush=True,
            )

    print(f"[REPLAY] {'dry run rolled back' if dry_run else 'committed'} after {time.time() - t0:.0f}s", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="replay only the cached blocks that carry the given addresses")
    parser.add_argument("--start-block", type=lambda x: int(x, 0), default=0)
    parser.add_argument("--end-block", type=lambda x: int(x, 0), default=0)
    parser.add_argument("--address", action="append", default=[])
    parser.add_argument("--nadfun", action="store_true")
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--i-know-the-indexer-is-stopped", action="store_true")
    args = parser.parse_args()

    addresses = [a.lower() for a in args.address]
    if args.nadfun:
        addresses.extend(a.lower() for a in h.NADFUN_ADDRS)
    addresses = list(dict.fromkeys(addresses))
    if not addresses:
        print("pass --address or --nadfun", file=sys.stderr)
        raise SystemExit(2)

    if not args.dry_run and not args.i_know_the_indexer_is_stopped:
        print(
            "refusing to write while the indexer may be live: its in memory state would overwrite "
            "replayed rows. stop the indexer, then pass --i-know-the-indexer-is-stopped",
            file=sys.stderr,
        )
        raise SystemExit(2)

    storage.init_pool()
    end = args.end_block or storage.get_cached_block_range()[1] or 0
    asyncio.run(replay(args.start_block, int(end), addresses, args.batch, args.dry_run))


if __name__ == "__main__":
    main()
