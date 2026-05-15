from __future__ import annotations

import argparse
import time

from replay_dump import replay_dump_range


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark dump replay throughput into Postgres.")
    parser.add_argument("--dump", default="chain-log-dump", help="dump directory containing manifest.json")
    parser.add_argument("--start", type=lambda x: int(x, 0), default=37709836, help="first block to replay")
    parser.add_argument("--end", type=lambda x: int(x, 0), required=True, help="last block to replay")
    parser.add_argument("--reset", action="store_true", help="clear derived state before replay")
    parser.add_argument("--allow-missing-timestamps", action="store_true", help="allow logs without blockTimestamp")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started = time.perf_counter()
    last = replay_dump_range(
        args.dump,
        start=args.start,
        end=args.end,
        reset=args.reset,
        strict_timestamps=not args.allow_missing_timestamps,
    )
    elapsed = time.perf_counter() - started
    blocks = max(0, int(last) - int(args.start) + 1)
    rate = blocks / elapsed if elapsed > 0 else 0.0
    print(
        f"[bench] replayed_blocks={blocks} last_block={last} elapsed_sec={elapsed:.3f} blocks_per_sec={rate:.2f}",
        flush=True,
    )


if __name__ == "__main__":
    main()
