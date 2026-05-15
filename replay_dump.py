from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any

from core import chain as h
from core.sequencer import SEQUENCER
import core.storage as storage
from modules import nadfun


def _read_manifest(dump_dir: Path) -> dict[str, Any]:
    path = dump_dir / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"missing manifest: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_logs(path: Path) -> dict[int, list[dict[str, Any]]]:
    logs_by_block: dict[int, list[dict[str, Any]]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            log = json.loads(line)
            blk_raw = log.get("blockNumber")
            if blk_raw is None:
                continue
            blk = int(blk_raw, 16) if isinstance(blk_raw, str) else int(blk_raw)
            logs_by_block.setdefault(blk, []).append(log)
    return logs_by_block


def _topic_addr(topic: str) -> str:
    if not isinstance(topic, str):
        return ""
    hex_topic = topic[2:] if topic.startswith("0x") else topic
    if len(hex_topic) < 40:
        return ""
    return ("0x" + hex_topic[-40:]).lower()


def _new_tokens_in_block(logs_for_blk: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for raw in logs_for_blk:
        topics = raw.get("topics") or []
        if not topics:
            continue
        tag = h.EVENT_SIGS.get(str(topics[0]).lower())
        addr = str(raw.get("address") or "").lower()
        if tag in {"NFC", "TC"}:
            if len(topics) < 3:
                continue
            if tag == "NFC" and addr != h.CONTRACTS["NADFUN"].lower():
                continue
            if tag == "TC" and addr != h.CONTRACTS.get("ROUTER", "").lower():
                continue
            tok_topic_idx = 2 if tag == "NFC" else 1
            tok = _topic_addr(topics[tok_topic_idx]) if len(topics) > tok_topic_idx else ""
            if tok:
                out.add(tok)
            continue
        if tag == "MC" and addr == h.CONTRACTS.get("ROUTER", "").lower():
            parser = h.PARSERS.get("MC")
            if parser is None:
                continue
            try:
                parsed = parser(addr, topics, str(raw.get("data") or "")[2:])
            except Exception:
                continue
            market_addr = str((parsed or {}).get("market") or "").lower()
            market_type = int((parsed or {}).get("marketType", 0) or 0)
            if market_addr and market_type > 1:
                out.add(market_addr)
    return out


def _filter_logs(logs_by_block: dict[int, list[dict[str, Any]]]) -> dict[int, list[dict[str, Any]]]:
    filtered_by_block: dict[int, list[dict[str, Any]]] = {}

    for blk, logs_for_blk in logs_by_block.items():
        filtered: list[dict[str, Any]] = []
        new_tokens = _new_tokens_in_block(logs_for_blk)

        for raw in logs_for_blk:
            topics = raw.get("topics") or []
            if not topics:
                continue

            tag = h.EVENT_SIGS.get(str(topics[0]).lower())
            if not tag:
                continue

            addr = str(raw.get("address") or "").lower()
            if not h.accepts_log_for_indexing(tag, addr):
                continue

            if tag == "TF":
                mi = SEQUENCER._state.addressToMarket.get(addr)
                is_lp_market = bool(mi is not None and int(getattr(mi, "marketType", 0) or 0) > 1)
                if (
                    addr not in SEQUENCER._state.launchpad_tokens
                    and addr not in SEQUENCER._state.token_to_v3_pool
                    and not is_lp_market
                    and addr not in new_tokens
                ):
                    continue

            filtered.append(raw)

        filtered_by_block[blk] = filtered

    return filtered_by_block


def _selected_chunks(manifest: dict[str, Any], start: int | None, end: int | None) -> list[dict[str, Any]]:
    chunks = sorted(
        manifest.get("chunks") or [],
        key=lambda c: (int(c["from_block"]), int(c["to_block"])),
    )
    out = []
    for chunk in chunks:
        chunk_start = int(chunk["from_block"])
        chunk_end = int(chunk["to_block"])
        if start is not None and chunk_end < start:
            continue
        if end is not None and chunk_start > end:
            continue
        out.append(chunk)
    return out


def _validate_contiguous_chunks(chunks: list[dict[str, Any]], first_block: int, last_block: int) -> None:
    expected = first_block
    for chunk in chunks:
        chunk_start = int(chunk["from_block"])
        chunk_end = int(chunk["to_block"])
        if chunk_end < expected:
            continue
        if chunk_start > expected:
            raise RuntimeError(f"dump gap before block {expected}: next chunk starts at {chunk_start}")
        expected = chunk_end + 1
        if expected > last_block:
            return
    if expected <= last_block:
        raise RuntimeError(f"dump ends before requested block {last_block}; next missing block is {expected}")


def dump_bounds(dump: str | Path) -> tuple[int | None, int | None]:
    dump_dir = Path(dump).resolve()
    manifest = _read_manifest(dump_dir)
    chunks = _selected_chunks(manifest, None, None)
    if not chunks:
        return None, None
    return int(chunks[0]["from_block"]), int(chunks[-1]["to_block"])


def replay_dump_range(
    dump: str | Path,
    *,
    start: int | None = None,
    end: int | None = None,
    reset: bool = False,
    init_storage: bool = True,
    close_storage: bool = True,
    rebuild_state: bool = True,
    strict_timestamps: bool = True,
) -> int:
    dump_dir = Path(dump).resolve()
    manifest = _read_manifest(dump_dir)
    chunks = _selected_chunks(manifest, start, end)
    if not chunks:
        raise RuntimeError("no chunks selected")

    first_block = max(int(chunks[0]["from_block"]), start) if start is not None else int(chunks[0]["from_block"])
    last_block = min(int(chunks[-1]["to_block"]), end) if end is not None else int(chunks[-1]["to_block"])
    _validate_contiguous_chunks(chunks, first_block, last_block)

    if init_storage:
        storage.init_pool()
        storage.init_db()

    if reset:
        with storage.db_cursor() as cur:
            storage.clear_derived_state_from_block(first_block, cur)
        SEQUENCER._state.reset_for_reindex()
    elif rebuild_state:
        SEQUENCER._state.rebuild_from_db()

    SEQUENCER.reset_pending(first_block)
    nadfun._PENDING_SYNC.clear()
    nadfun.METADATA_QUEUE.clear()

    for chunk in chunks:
        chunk_start = int(chunk["from_block"])
        chunk_end = int(chunk["to_block"])
        process_start = max(chunk_start, first_block)
        process_end = min(chunk_end, last_block)
        if process_start > process_end:
            continue

        path = dump_dir / str(chunk["file"])
        logs_by_block = _read_logs(path)
        if strict_timestamps:
            for blk, logs in logs_by_block.items():
                if not logs:
                    continue
                if any(log.get("blockTimestamp") is None for log in logs):
                    raise RuntimeError(f"dump log missing blockTimestamp in {path.name} block {blk}")

        filtered = _filter_logs(logs_by_block)

        with storage.db_cursor() as cur:
            SEQUENCER.process_chunk(process_start, process_end, filtered, cur)

        print(f"[replay] {process_start}-{process_end} from {path.name}", flush=True)

    if close_storage:
        storage.close_pool()

    return last_block


def replay_dump(args: argparse.Namespace) -> None:
    replay_dump_range(
        args.dump,
        start=args.start,
        end=args.end,
        reset=args.reset,
        strict_timestamps=not args.allow_missing_timestamps,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay exported log dump chunks into derived Postgres state without RPC.")
    parser.add_argument("--dump", default="chain-log-dump", help="dump directory containing manifest.json")
    parser.add_argument("--start", type=lambda x: int(x, 0), default=None, help="first block to replay")
    parser.add_argument("--end", type=lambda x: int(x, 0), default=None, help="last block to replay")
    parser.add_argument("--reset", action="store_true", help="clear derived state before replay")
    parser.add_argument("--allow-missing-timestamps", action="store_true", help="allow logs without blockTimestamp")
    return parser.parse_args()


if __name__ == "__main__":
    replay_dump(parse_args())
