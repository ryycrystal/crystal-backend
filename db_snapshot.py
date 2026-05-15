from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path

import core.storage as storage
from core.storage.base import _build_database_url


def _database_url() -> str:
    url = os.getenv("DATABASE_URL") or _build_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL or PGHOST/PGUSER/PGPASSWORD/PGDATABASE is required for snapshot operations")
    return url


def _last_processed_block() -> int:
    storage.init_pool()
    try:
        last = storage.get_last_processed_block()
        return int(last) if last is not None else 0
    finally:
        storage.close_pool()


def create_snapshot(args: argparse.Namespace) -> None:
    database_url = _database_url()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    block = _last_processed_block()
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    label = f"_{args.label}" if args.label else ""
    path = out_dir / f"derived_state_block_{block:012d}_{ts}{label}.dump"

    cmd = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--file",
        str(path),
        database_url,
    ]
    subprocess.run(cmd, check=True)

    meta = path.with_suffix(".txt")
    meta.write_text(
        f"block={block}\ncreated_utc={ts}\nformat=pg_dump_custom\n",
        encoding="utf-8",
    )
    print(f"[snapshot] wrote {path}", flush=True)


def restore_snapshot(args: argparse.Namespace) -> None:
    database_url = _database_url()
    path = Path(args.file).resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    cmd = [
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-acl",
        "--dbname",
        database_url,
        str(path),
    ]
    if args.jobs > 1:
        cmd.insert(1, f"--jobs={args.jobs}")

    subprocess.run(cmd, check=True)
    print(f"[snapshot] restored {path}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or restore derived Postgres state snapshots.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create", help="create a compressed pg_dump snapshot")
    create.add_argument("--out", default="snapshots", help="snapshot output directory")
    create.add_argument("--label", default="", help="optional filename label")
    create.set_defaults(func=create_snapshot)

    restore = sub.add_parser("restore", help="restore a pg_dump snapshot")
    restore.add_argument("file", help="snapshot .dump file")
    restore.add_argument("--jobs", type=int, default=1, help="parallel pg_restore jobs")
    restore.set_defaults(func=restore_snapshot)

    return parser.parse_args()


if __name__ == "__main__":
    parsed = parse_args()
    parsed.func(parsed)
