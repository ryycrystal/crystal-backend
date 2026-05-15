from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from core import chain as h
from state import RPC_HTTP as DEFAULT_RPC_HTTP


DEFAULT_BATCH_BLOCKS = 100


class RpcClient:
    def __init__(self, url: str, rps: float, timeout: float) -> None:
        self.url = url
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self.last_request = 0.0
        self.lock = asyncio.Lock()
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, params: list[Any]) -> dict[str, Any]:
        async with self.lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self.last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_request = time.monotonic()

        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        resp = await self.client.post(self.url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data)
        return data

    async def call_batch(self, calls: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:
        if not calls:
            return []

        async with self.lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self.last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_request = time.monotonic()

        payload = [
            {"jsonrpc": "2.0", "id": idx, "method": method, "params": params}
            for idx, (method, params) in enumerate(calls)
        ]
        resp = await self.client.post(self.url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise RuntimeError(data)

        by_id = {item.get("id"): item for item in data}
        ordered: list[dict[str, Any]] = []
        for idx in range(len(calls)):
            item = by_id.get(idx)
            if item is None or "error" in item:
                raise RuntimeError(item or {"error": f"missing batch response id {idx}"})
            ordered.append(item)
        return ordered


def _chunk_path(out_dir: Path, start: int, end: int) -> Path:
    return out_dir / f"logs_{start:012d}_{end:012d}.jsonl.gz"


def _manifest_path(out_dir: Path) -> Path:
    return out_dir / "manifest.json"


def _read_manifest(out_dir: Path) -> dict[str, Any]:
    path = _manifest_path(out_dir)
    if not path.exists():
        return {"chunks": []}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_manifest(out_dir: Path, manifest: dict[str, Any]) -> None:
    path = _manifest_path(out_dir)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, separators=(",", ":"), sort_keys=True)
    tmp.replace(path)


def _write_logs(path: Path, logs: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        for log in logs:
            f.write(json.dumps(log, separators=(",", ":"), sort_keys=True))
            f.write("\n")
    tmp.replace(path)


async def _head(client: RpcClient) -> int:
    data = await client.call("eth_blockNumber", [])
    return int(data["result"], 16)


async def _fetch_logs(
    client: RpcClient,
    start: int,
    end: int,
    topics: list[str] | None,
) -> list[dict[str, Any]]:
    flt: dict[str, Any] = {
        "fromBlock": hex(start),
        "toBlock": hex(end),
    }
    if topics is not None:
        flt["topics"] = [topics]
    data = await client.call("eth_getLogs", [flt])
    return data.get("result") or []


async def _fetch_block_timestamps(
    client: RpcClient,
    blocks: list[int],
    batch_size: int,
) -> dict[int, int]:
    out: dict[int, int] = {}
    for i in range(0, len(blocks), batch_size):
        chunk = blocks[i:i + batch_size]
        calls = [("eth_getBlockByNumber", [hex(blk), False]) for blk in chunk]
        responses = await client.call_batch(calls)
        for blk, data in zip(chunk, responses):
            result = data.get("result") or {}
            ts_raw = result.get("timestamp")
            if ts_raw is None:
                raise RuntimeError(f"missing timestamp for block {blk}")
            out[blk] = int(ts_raw, 16) if isinstance(ts_raw, str) else int(ts_raw)
    return out


async def _stamp_missing_timestamps(
    client: RpcClient,
    logs: list[dict[str, Any]],
    timestamp_batch_size: int,
) -> dict[int, int]:
    block_ts: dict[int, int] = {}
    missing: set[int] = set()

    for log in logs:
        blk_raw = log.get("blockNumber")
        if blk_raw is None:
            continue
        blk = int(blk_raw, 16) if isinstance(blk_raw, str) else int(blk_raw)
        ts_raw = log.get("blockTimestamp")
        if ts_raw is None:
            missing.add(blk)
            continue
        block_ts[blk] = int(ts_raw, 16) if isinstance(ts_raw, str) else int(ts_raw)

    missing_blocks = [blk for blk in sorted(missing) if blk not in block_ts]
    if missing_blocks:
        block_ts.update(await _fetch_block_timestamps(client, missing_blocks, timestamp_batch_size))

    for log in logs:
        if log.get("blockTimestamp") is not None:
            continue
        blk_raw = log.get("blockNumber")
        if blk_raw is None:
            continue
        blk = int(blk_raw, 16) if isinstance(blk_raw, str) else int(blk_raw)
        ts = block_ts.get(blk)
        if ts is not None:
            log["blockTimestamp"] = hex(ts)

    return block_ts


async def export_logs(args: argparse.Namespace) -> None:
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    topics = h.TOPICS if args.indexed_topics else None
    client = RpcClient(args.rpc_url, args.rps, args.timeout)

    try:
        while True:
            manifest = _read_manifest(out_dir)
            last_complete = manifest.get("last_complete_block")
            start_block = args.start
            if args.resume and last_complete is not None:
                start_block = max(start_block, int(last_complete) + 1)

            end_block = args.end if args.end is not None else await _head(client)
            if start_block > end_block:
                if args.follow:
                    await asyncio.sleep(args.follow_interval)
                    continue
                raise ValueError(f"start block {start_block} is after end block {end_block}")

            manifest.update(
                {
                    "rpc_url": args.rpc_url,
                    "start_block": int(manifest.get("start_block") or start_block),
                    "end_block": end_block,
                    "batch_blocks": args.batch,
                    "mode": "indexed_topics" if args.indexed_topics else "all_logs",
                    "format": "gzip_jsonl_one_log_per_line",
                }
            )
            chunks = manifest.setdefault("chunks", [])
            known_files = {str(c.get("file")) for c in chunks}

            total_logs = int(manifest.get("total_logs") or 0)
            for chunk_start in range(start_block, end_block + 1, args.batch):
                chunk_end = min(chunk_start + args.batch - 1, end_block)
                path = _chunk_path(out_dir, chunk_start, chunk_end)

                if path.exists() and not args.overwrite:
                    print(f"[skip] {chunk_start}-{chunk_end} exists: {path.name}", flush=True)
                    manifest["last_complete_block"] = max(int(manifest.get("last_complete_block") or 0), chunk_end)
                    if path.name not in known_files:
                        chunks.append(
                            {
                                "from_block": chunk_start,
                                "to_block": chunk_end,
                                "file": path.name,
                                "logs": None,
                                "timestamped_blocks": None,
                            }
                        )
                        known_files.add(path.name)
                    _write_manifest(out_dir, manifest)
                    continue

                for attempt in range(1, args.retries + 1):
                    try:
                        logs = await _fetch_logs(client, chunk_start, chunk_end, topics)
                        block_timestamps = {}
                        if args.include_timestamps:
                            block_timestamps = await _stamp_missing_timestamps(
                                client,
                                logs,
                                args.timestamp_batch,
                            )
                        _write_logs(path, logs)
                        chunks.append(
                            {
                                "from_block": chunk_start,
                                "to_block": chunk_end,
                                "file": path.name,
                                "logs": len(logs),
                                "timestamped_blocks": len(block_timestamps),
                            }
                        )
                        known_files.add(path.name)
                        total_logs += len(logs)
                        manifest["total_logs"] = total_logs
                        manifest["last_complete_block"] = chunk_end
                        _write_manifest(out_dir, manifest)
                        print(
                            f"[ok] {chunk_start}-{chunk_end}: {len(logs)} logs -> {path.name}",
                            flush=True,
                        )
                        break
                    except Exception as e:
                        if attempt >= args.retries:
                            raise
                        wait = min(30.0, 2.0 * attempt)
                        print(
                            f"[retry] {chunk_start}-{chunk_end} attempt {attempt}/{args.retries}: {e!r}",
                            flush=True,
                        )
                        await asyncio.sleep(wait)

            if not args.follow:
                return
            await asyncio.sleep(args.follow_interval)
    finally:
        await client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export EVM logs to resumable gzip JSONL chunk files."
    )
    parser.add_argument("start", type=lambda x: int(x, 0), help="first block to export")
    parser.add_argument("--end", type=lambda x: int(x, 0), default=None, help="last block; defaults to chain head")
    parser.add_argument("--out", default="chain-log-dump", help="output directory")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH_BLOCKS, help="blocks per eth_getLogs request")
    parser.add_argument("--rps", type=float, default=float(os.getenv("RPC_EXPORT_RPS", "10")), help="max RPC requests per second")
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds")
    parser.add_argument("--retries", type=int, default=5, help="retries per chunk")
    parser.add_argument("--rpc-url", default=os.getenv("RPC_HTTP", DEFAULT_RPC_HTTP), help="HTTP JSON-RPC endpoint")
    parser.add_argument("--indexed-topics", action="store_true", help="export only topics this backend indexes")
    parser.add_argument("--no-timestamps", dest="include_timestamps", action="store_false", help="do not fetch missing block timestamps")
    parser.add_argument("--timestamp-batch", type=int, default=100, help="eth_getBlockByNumber calls per JSON-RPC batch")
    parser.add_argument("--follow", action="store_true", help="keep exporting new head ranges forever")
    parser.add_argument("--follow-interval", type=float, default=2.0, help="seconds between follow-mode head checks")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="do not continue from manifest last_complete_block")
    parser.add_argument("--overwrite", action="store_true", help="overwrite existing chunk files")
    parser.set_defaults(include_timestamps=True)
    parser.set_defaults(resume=True)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(export_logs(parse_args()))
