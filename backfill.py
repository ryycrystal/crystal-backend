import asyncio
import argparse
import httpx
import traceback

from core import chain as h
import core.storage as storage
from core.sequencer import SEQUENCER
from state import RPC_HTTP
from modules import nadfun

async def _process_metadata_background():
    try:
        results = await nadfun.process_metadata_queue()
        if results:
            storage.update_token_metadata_batch(results)
    except Exception as e:
        print(f"[Backfill] Metadata background error: {e!r}")


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

    last_processed = start_block - 1

    for chunk_start in range(start_block, max_cached + 1, batch):
        chunk_end = min(chunk_start + batch - 1, max_cached)

        with storage.db_cursor() as cur:
            cached = storage.get_block_logs_range(chunk_start, chunk_end, cur=cur)

            filtered_logs: dict[int, list[dict]] = {}
            for blk in range(chunk_start, chunk_end + 1):
                logs_for_blk = cached.get(blk, [])
                filtered = []

                for raw in logs_for_blk:
                    topics = raw.get("topics") or []
                    if not topics:
                        continue

                    tag = h.EVENT_SIGS.get(topics[0].lower())
                    if not tag:
                        continue

                    addr = raw.get("address", "").lower()

                    if tag in ("NFC", "NFB", "NFS", "NFSYNC", "NFT", "MG"):
                        if addr != h.CONTRACTS["NADFUN"].lower():
                            continue
                    elif tag == "V3SWAP":
                        pass
                    elif tag == "TF":
                        if addr not in SEQUENCER._state.launchpad_tokens and addr not in SEQUENCER._state.token_to_v3_pool:
                            continue
                    else:
                        continue

                    filtered.append(raw)

                filtered_logs[blk] = filtered

            SEQUENCER.process_chunk(chunk_start, chunk_end, filtered_logs, cur)

        asyncio.create_task(_process_metadata_background())

        last_processed = chunk_end
        if chunk_end % 1000 < batch:
            print(f"[Reindex] Processed up to block {chunk_end}")

        await asyncio.sleep(0)

    print(f"[Reindex] Complete, last processed = {last_processed}")
    return last_processed

# parse cli arguments for the backfiller process
# returns an argparse namespace with start_block and batch size
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

async def _get_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(timeout=30.0)
    return _HTTP_CLIENT

# gets current chainhead block number
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

# fetch logs for a block range [frm, to] using eth_getLogs
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

# main backfill loop
# walks from start_block up to current head in batches
# seeds headers and replaying logs into the sequencer in order
# returns the last processed block
async def backfill(start_block: int, batch: int) -> int:
    while True:
        try:
            head_snapshot = await get_head_http()
            print(f"[Backfill] CH @ Init = {head_snapshot}")

            last_processed = start_block - 1

            for chunk_start in range(start_block, head_snapshot + 1, batch):
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

                    filtered_logs: dict[int, list[dict]] = {}
                    for blk in range(chunk_start, chunk_end + 1):
                        logs_for_blk = cached.get(blk, [])
                        filtered = []

                        for raw in logs_for_blk:
                            topics = raw.get("topics") or []
                            if not topics:
                                continue

                            tag = h.EVENT_SIGS.get(topics[0].lower())
                            if not tag:
                                continue

                            addr = raw.get("address", "").lower()

                            if tag in ("NFC", "NFB", "NFS", "NFSYNC", "NFT", "MG"):
                                if addr != h.CONTRACTS["NADFUN"].lower():
                                    continue
                            elif tag == "V3SWAP":
                                pass
                            elif tag == "TF":
                                if addr not in SEQUENCER._state.launchpad_tokens and addr not in SEQUENCER._state.token_to_v3_pool:
                                    continue
                            else:
                                continue

                            filtered.append(raw)

                        filtered_logs[blk] = filtered

                    if fetched_blocks:
                        to_write = {blk: cached.get(blk, []) for blk in fetched_blocks}
                        storage.write_block_logs_batch(to_write, cur)

                    SEQUENCER.process_chunk(chunk_start, chunk_end, filtered_logs, cur)

                asyncio.create_task(_process_metadata_background())

                last_processed = chunk_end

            print(f"[Backfill] Complete, LP = {last_processed}")
            return last_processed

        except Exception as e:
            print(f"[Backfill] Fatal Error {e!r}", flush=True)
            traceback.print_exc()
            await asyncio.sleep(5.0)