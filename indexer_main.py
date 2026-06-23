from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import backfill
import export_logs
from core.stream import stream_logs, vault_sampler
from core.sequencer import SEQUENCER
import core.storage as storage
from modules import nadfun
from replay_dump import dump_bounds, replay_dump_range


DEFAULT_START_BLOCK = 37709836


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crystal indexer startup coordinator")
    parser.add_argument(
        "--mode",
        choices=("resume", "bootstrap", "rebuild", "replay-only", "live"),
        default="resume",
        help=(
            "resume: DB + dump catchup + live; bootstrap: first start from dump or RPC; "
            "rebuild: clear derived DB and replay dump; replay-only: replay dump and exit; "
            "live: DB state then RPC/live only"
        ),
    )
    parser.add_argument("--start-block", type=lambda x: int(x, 0), default=DEFAULT_START_BLOCK)
    parser.add_argument("--dump-dir", default=None, help="directory created by export_logs.py")
    parser.add_argument("--dump-end", type=lambda x: int(x, 0), default=None, help="last dump block to replay")
    parser.add_argument("--allow-missing-timestamps", action="store_true", help="allow dump logs without blockTimestamp")
    parser.add_argument("--live-dump-dir", default=None, help="continuously append full-chain logs to this dump directory")
    parser.add_argument("--live-dump-batch", type=int, default=100, help="blocks per live dump eth_getLogs request")
    parser.add_argument("--live-dump-rps", type=float, default=5.0, help="max RPC requests per second for live dump follower")
    parser.add_argument("--live-dump-indexed-only", action="store_true", help="live dump only backend-indexed topics instead of all logs")
    parser.add_argument("--no-indexer-lock", action="store_true", help="disable Postgres advisory lock guard")
    parser.add_argument(
        "--from-current-block",
        action="store_true",
        help="in live mode, load DB state but start streaming from the current RPC head",
    )
    return parser.parse_args()


def _load_state_from_db() -> int:
    print("[IDX] Loading derived state from DB...", flush=True)
    SEQUENCER._state.rebuild_from_db()
    print(
        f"[IDX] Loaded {len(SEQUENCER._state.launchpad_tokens)} tokens, "
        f"{len(SEQUENCER._state.v3_pools)} pools, "
        f"{len(getattr(SEQUENCER._state, 'addressToMarket', {}))} markets, "
        f"{len(getattr(SEQUENCER._state, 'vaults', {}))} vaults",
        flush=True,
    )

    last_blk = storage.get_last_processed_block()
    return int(last_blk) if last_blk is not None else 0


def _dump_exists(dump_dir: str | None) -> bool:
    if not dump_dir:
        return False
    return (Path(dump_dir) / "manifest.json").exists()


def _replay_dump(
    *,
    dump_dir: str,
    start: int,
    end: int | None,
    reset: bool,
    rebuild_state: bool,
    allow_missing_timestamps: bool,
) -> int:
    dump_start, dump_end = dump_bounds(dump_dir)
    if dump_start is None or dump_end is None:
        raise RuntimeError(f"dump has no chunks: {dump_dir}")
    if dump_start > start:
        raise RuntimeError(
            f"dump starts at {dump_start}, but replay was requested from {start}; "
            "refusing to create a derived-state gap"
        )

    replay_start = max(start, dump_start)
    replay_end = min(end, dump_end) if end is not None else dump_end
    if replay_start > replay_end:
        print(
            f"[IDX] Dump has nothing to replay for requested range {start}-{end or dump_end}",
            flush=True,
        )
        return start - 1

    print(
        f"[IDX] Replaying dump {dump_dir} blocks {replay_start}-{replay_end} "
        f"reset={reset}",
        flush=True,
    )
    return replay_dump_range(
        dump_dir,
        start=replay_start,
        end=replay_end,
        reset=reset,
        init_storage=False,
        close_storage=False,
        rebuild_state=rebuild_state,
        strict_timestamps=not allow_missing_timestamps,
    )


async def _run_live_dump(args: argparse.Namespace, start_block: int) -> None:
    dump_args = argparse.Namespace(
        start=start_block,
        end=None,
        out=args.live_dump_dir,
        batch=args.live_dump_batch,
        rps=args.live_dump_rps,
        timeout=60.0,
        retries=5,
        rpc_url=None,
        indexed_topics=args.live_dump_indexed_only,
        include_timestamps=True,
        timestamp_batch=100,
        overwrite=False,
        follow=True,
        follow_interval=2.0,
        resume=True,
    )
    dump_args.rpc_url = os.getenv("RPC_HTTP", export_logs.DEFAULT_RPC_HTTP)

    while True:
        try:
            await export_logs.export_logs(dump_args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[DUMP] live dump follower failed: {e!r}; restarting in 5s", flush=True)
            await asyncio.sleep(5.0)


async def _start_live(args: argparse.Namespace, start_block: int) -> None:
    print(f"[IDX] Starting live stream from block {start_block}", flush=True)
    await nadfun.start_metadata_worker(storage)
    SEQUENCER.set_next_block(start_block)

    tasks = [
        asyncio.create_task(stream_logs(start_block)),
        asyncio.create_task(vault_sampler(SEQUENCER._state)),
    ]
    if args.live_dump_dir:
        print(
            f"[DUMP] Starting live full-log dump follower at {args.live_dump_dir} "
            f"from block {start_block}",
            flush=True,
        )
        tasks.append(asyncio.create_task(_run_live_dump(args, start_block)))

    await asyncio.gather(*tasks)


async def _live_start_block(args: argparse.Namespace, last_db: int) -> int:
    if not args.from_current_block:
        return (last_db + 1) if last_db else args.start_block

    head = await backfill.get_head_http()
    print(
        f"[IDX] --from-current-block set: DB last processed block is {last_db or 'none'}; "
        f"RPC head is {head}",
        flush=True,
    )
    return head


async def main() -> None:
    args = parse_args()

    storage.init_pool()
    lock_conn = None

    try:
        if not args.no_indexer_lock:
            lock_conn = storage.acquire_indexer_lock()
            print("[IDX] Acquired Postgres advisory indexer lock", flush=True)

        storage.init_db()

        if args.mode == "bootstrap":
            if _dump_exists(args.dump_dir):
                last = _replay_dump(
                    dump_dir=args.dump_dir,
                    start=args.start_block,
                    end=args.dump_end,
                    reset=True,
                    rebuild_state=False,
                    allow_missing_timestamps=args.allow_missing_timestamps,
                )
                await _start_live(args, last + 1)
            else:
                print(
                    f"[IDX] Bootstrap without dump: starting RPC/live backfill at {args.start_block}",
                    flush=True,
                )
                SEQUENCER._state.reset_for_reindex()
                await _start_live(args, args.start_block)
            return

        if args.mode == "resume":
            last_db = _load_state_from_db()
            start = (last_db + 1) if last_db else args.start_block
            if _dump_exists(args.dump_dir):
                last = _replay_dump(
                    dump_dir=args.dump_dir,
                    start=start,
                    end=args.dump_end,
                    reset=False,
                    rebuild_state=False,
                    allow_missing_timestamps=args.allow_missing_timestamps,
                )
                if last >= start:
                    start = last + 1
            else:
                print("[IDX] Resume without dump: using DB state then RPC/live catchup", flush=True)
            await _start_live(args, start)
            return

        if args.mode == "rebuild":
            if not _dump_exists(args.dump_dir):
                raise RuntimeError("--mode rebuild requires --dump-dir")
            last = _replay_dump(
                dump_dir=args.dump_dir,
                start=args.start_block,
                end=args.dump_end,
                reset=True,
                rebuild_state=False,
                allow_missing_timestamps=args.allow_missing_timestamps,
            )
            await _start_live(args, last + 1)
            return

        if args.mode == "replay-only":
            if not _dump_exists(args.dump_dir):
                raise RuntimeError("--mode replay-only requires --dump-dir")
            _replay_dump(
                dump_dir=args.dump_dir,
                start=args.start_block,
                end=args.dump_end,
                reset=True,
                rebuild_state=False,
                allow_missing_timestamps=args.allow_missing_timestamps,
            )
            print("[IDX] Replay-only complete; exiting without live stream", flush=True)
            return

        if args.mode == "live":
            last_db = _load_state_from_db()
            start = await _live_start_block(args, last_db)
            await _start_live(args, start)
            return

        raise RuntimeError(f"unknown mode: {args.mode}")
    finally:
        storage.release_indexer_lock(lock_conn)
        if args.mode == "replay-only":
            storage.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
