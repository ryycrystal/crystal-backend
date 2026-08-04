import asyncio
import argparse
import httpx
import os
import traceback

TIMESTAMP_CONCURRENCY = int(os.getenv("TIMESTAMP_CONCURRENCY", "32"))

from core import chain as h
import core.storage as storage
from core.sequencer import SEQUENCER
from state import RPC_HTTP
from modules import nadfun


async def _process_metadata_batch_for_resync() -> int:
    qlen = len(nadfun.METADATA_QUEUE)
    if qlen == 0:
        return 0

    try:
        count = await nadfun.process_metadata_queue_immediate(storage)
        return count
    except Exception as e:
        print(f"[Resync] Metadata batch error: {e!r}")
        return 0


async def reindex(start_block: int, batch: int) -> int:
    print(f"[Reindex] Starting reindex from block {start_block}")

    min_cached, max_cached = storage.get_cached_block_range()
    if min_cached is None:
        return start_block - 1

    if start_block < min_cached:
        start_block = min_cached

    with storage.db_cursor() as cur:
        storage.clear_derived_state_from_block(start_block, cur)

    SEQUENCER._state.reset_for_reindex()
    SEQUENCER.reset_pending(start_block)
    nadfun._PENDING_SYNC.clear()

    last_processed = start_block - 1
    total_metadata_fetched = 0
    metadata_process_interval = 10

    for batch_idx, chunk_start in enumerate(range(start_block, max_cached + 1, batch)):
        chunk_end = min(chunk_start + batch - 1, max_cached)

        with storage.db_cursor() as cur:
            cached = storage.get_block_logs_range(chunk_start, chunk_end, cur=cur)
            await ensure_block_timestamps(cached)

            filtered_logs: dict[int, list[dict]] = {}
            for blk in range(chunk_start, chunk_end + 1):
                logs_for_blk = cached.get(blk, [])
                for raw in logs_for_blk:
                    h.register_dynamic_addresses_from_log(raw)
                filtered = []
                new_tokens_in_blk = _new_tokens_in_block(logs_for_blk)

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

        if (batch_idx + 1) % metadata_process_interval == 0 or chunk_end >= max_cached:
            qlen = len(nadfun.METADATA_QUEUE)
            if qlen > 0:
                fetched = await _process_metadata_batch_for_resync()
                total_metadata_fetched += fetched
                if fetched > 0:
                    print(f"[Reindex] Fetched {fetched}/{qlen} metadata items (total: {total_metadata_fetched})")

        last_processed = chunk_end
        if chunk_end % 1000 < batch:
            print(f"[Reindex] Processed up to block {chunk_end}")

        await asyncio.sleep(0)

    remaining = len(nadfun.METADATA_QUEUE)
    if remaining > 0:
        print(f"[Reindex] Final metadata drain: {remaining} items...")
        fetched = await _process_metadata_batch_for_resync()
        total_metadata_fetched += fetched

    print(f"[Reindex] Complete, last processed = {last_processed}, total metadata fetched = {total_metadata_fetched}")
    return last_processed



def parse_args():
    parser = argparse.ArgumentParser(description="backfiller")
    parser.add_argument(
        "start_block",
        type=lambda x: int(x, 0),
        help="block to start backfill from (decimal or 0x-prefixed hex)",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=100,
        help="blocks per eth_getLogs query (keep < 100)",
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="reprocess from cached logs only (no RPC), clears derived state from start_block",
    )
    return parser.parse_args()

_HTTP_CLIENT: httpx.AsyncClient | None = None
_BLOCK_TS_CACHE: dict[int, int] = {}

async def _get_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(timeout=30.0)
    return _HTTP_CLIENT


def _topic_addr(topic: str) -> str:
    if not isinstance(topic, str):
        return ""
    hex_topic = topic[2:] if topic.startswith("0x") else topic
    if len(hex_topic) < 40:
        return ""
    return ("0x" + hex_topic[-40:]).lower()


def _new_tokens_in_block(logs_for_blk: list[dict]) -> set[str]:
    out: set[str] = set()
    for raw in logs_for_blk:
        topics = raw.get("topics") or []
        if not topics:
            continue
        tag = h.EVENT_SIGS.get(topics[0].lower())
        addr = raw.get("address", "").lower()
        if tag in {"NFC", "TC"}:
            if len(topics) < 3:
                continue
            if tag == "NFC" and not h.is_nadfun_address(addr):
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
                parsed = parser(addr, topics, (raw.get("data") or "")[2:])
            except Exception:
                continue
            market_addr = str((parsed or {}).get("market") or "").lower()
            market_type = int((parsed or {}).get("marketType", 0) or 0)
            if market_addr and market_type > 1:
                out.add(market_addr)
    return out


async def get_block_timestamp_http(block_number: int) -> int:
    blk = int(block_number)
    cached = _BLOCK_TS_CACHE.get(blk)
    if cached is not None:
        return cached

    data = await http_jsonrpc("eth_getBlockByNumber", [hex(blk), False])
    result = data.get("result") or {}
    ts_raw = result.get("timestamp")
    if ts_raw is None:
        raise RuntimeError(f"missing timestamp for block {blk}")
    ts = int(ts_raw, 16) if isinstance(ts_raw, str) else int(ts_raw)
    _BLOCK_TS_CACHE[blk] = ts
    return ts


async def ensure_block_timestamps(logs_by_block: dict[int, list[dict]]) -> None:
    missing: list[int] = []
    for blk, logs in logs_by_block.items():
        if not logs:
            continue
        if any(log.get("blockTimestamp") is None for log in logs):
            missing.append(int(blk))

    if not missing:
        return

    # one rpc round trip per block, so fetch them concurrently; awaiting each in
    # turn caps replay at 1/latency blocks per second regardless of the rps budget
    sem = asyncio.Semaphore(TIMESTAMP_CONCURRENCY)

    async def _fetch(blk: int) -> tuple[int, str]:
        async with sem:
            return blk, hex(await get_block_timestamp_http(blk))

    for blk, ts_hex in await asyncio.gather(*(_fetch(b) for b in missing)):
        for log in logs_by_block.get(blk, []):
            if log.get("blockTimestamp") is None:
                log["blockTimestamp"] = ts_hex


async def get_head_http() -> int:
    data = await http_jsonrpc("eth_blockNumber", [])
    return int(data["result"], 16)

async def http_jsonrpc(method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    client = await _get_client()
    await h.rate_gate()
    resp = await client.post(RPC_HTTP, json=payload)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data)
    return data


async def fetch_logs_http(frm: int, to: int) -> list[dict]:
    data = await http_jsonrpc(
        "eth_getLogs",
        [{
            "fromBlock": hex(frm),
            "toBlock": hex(to),
            "topics": [h.TOPICS],
        }],
    )
    return data["result"]





async def backfill(start_block: int, batch: int) -> int:
    while True:
        try:
            head_snapshot = await get_head_http()
            print(f"[Backfill] CH @ Init = {head_snapshot}")

            last_processed = start_block - 1
            total_metadata_fetched = 0
            metadata_process_interval = 10

            for batch_idx, chunk_start in enumerate(range(start_block, head_snapshot + 1, batch)):
                chunk_end = min(chunk_start + batch - 1, head_snapshot)

                with storage.db_cursor() as cur:
                    cached = storage.get_block_logs_range(chunk_start, chunk_end, cur=cur)

                    missing_ranges = []
                    cur_start = None

                    for blk in range(chunk_start, chunk_end + 1):
                        if blk in cached:
                            if cur_start is not None:
                                missing_ranges.append((cur_start, blk - 1))
                                cur_start = None
                        else:
                            if cur_start is None:
                                cur_start = blk
                    if cur_start is not None:
                        missing_ranges.append((cur_start, chunk_end))

                    fetched_blocks: set[int] = set()
                    for mr_start, mr_end in missing_ranges:
                        logs = await fetch_logs_http(mr_start, mr_end)

                        by_block: dict[int, list[dict]] = {}
                        for raw in logs:
                            blk_hex = raw.get("blockNumber")
                            blk = int(blk_hex, 16) if isinstance(blk_hex, str) else int(blk_hex)
                            if blk < mr_start or blk > mr_end:
                                continue
                            by_block.setdefault(blk, []).append(raw)

                        for blk in range(mr_start, mr_end + 1):
                            cached[blk] = by_block.get(blk, [])
                            fetched_blocks.add(blk)

                    await ensure_block_timestamps(cached)

                    filtered_logs: dict[int, list[dict]] = {}
                    for blk in range(chunk_start, chunk_end + 1):
                        logs_for_blk = cached.get(blk, [])
                        for raw in logs_for_blk:
                            h.register_dynamic_addresses_from_log(raw)
                        filtered = []
                        new_tokens_in_blk = _new_tokens_in_block(logs_for_blk)

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

                    if fetched_blocks:
                        to_write = {blk: cached.get(blk, []) for blk in fetched_blocks}
                        storage.write_block_logs_batch(to_write, cur)

                    SEQUENCER.process_chunk(chunk_start, chunk_end, filtered_logs, cur)

                if (batch_idx + 1) % metadata_process_interval == 0 or chunk_end >= head_snapshot:
                    qlen = len(nadfun.METADATA_QUEUE)
                    if qlen > 0:
                        fetched = await _process_metadata_batch_for_resync()
                        total_metadata_fetched += fetched

                last_processed = chunk_end

            remaining = len(nadfun.METADATA_QUEUE)
            if remaining > 0:
                fetched = await _process_metadata_batch_for_resync()
                total_metadata_fetched += fetched

            print(f"[Backfill] Complete, LP = {last_processed}, metadata fetched = {total_metadata_fetched}")
            return last_processed

        except Exception as e:
            print(f"[Backfill] Fatal Error {e!r}", flush=True)
            traceback.print_exc()
            try:
                db_last = storage.get_last_processed_block()
                resume_from = max(start_block, (int(db_last) + 1) if db_last is not None else start_block)
                print(f"[Backfill] Rebuilding in-memory state from DB, resume at {resume_from}", flush=True)
                SEQUENCER._state.rebuild_from_db()
                SEQUENCER.reset_pending(resume_from)
                nadfun._PENDING_SYNC.clear()
                start_block = resume_from
            except Exception as recover_err:
                print(f"[Backfill] Recovery failed {recover_err!r}", flush=True)
            await asyncio.sleep(5.0)
